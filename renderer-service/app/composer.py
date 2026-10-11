"""FFmpeg layer: Ken-Burns scene render, scene concat, background-music mix.

Design notes
------------
- Every scene is encoded with IDENTICAL parameters (x264 veryfast crf23,
  yuv420p, 1080x1920@30, aac 44.1k stereo 128k) so the concat demuxer can
  join them with ``-c copy`` — the video stream is never re-encoded when the
  copy path succeeds (pass 2 is nearly instant on a 1-vCPU box).
- Ken Burns: the still is cover-scaled to 1620x2880 (1.5x the output, avoids
  zoompan jitter), then zoompan renders the motion down to 1080x1920.
  Zoom step is computed per scene so the motion completes exactly over the
  clip instead of saturating early.
- zoom-out seeds itself from the ``zoom`` state (``lt(zoom,1.01)``) instead
  of ``on``-based first-frame checks, and floors at 1.05 so the seed can
  never re-trigger mid-scene.
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

# Identical encode params everywhere -> concat -c copy is safe.
V_CODEC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
A_CODEC = ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]

OUT_W, OUT_H = 1080, 1920
PRESCALE_W, PRESCALE_H = 1620, 2880  # 1.5x output for clean zoompan sampling
ZOOM_MAX = 1.15
ZOOM_OUT_FLOOR = 1.05


class FfmpegError(RuntimeError):
    """Raised when an ffmpeg/ffprobe invocation fails."""


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(f"ffmpeg timed out after {timeout}s: {' '.join(cmd[:6])}...") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1500:]
        raise FfmpegError(f"ffmpeg exited {proc.returncode}: {tail}")
    return proc


def render_scene(
    image: str,
    audio: str,
    ass: str | None,
    duration: float,
    out_path: str,
    zoom_in: bool = True,
    fps: int = 30,
) -> None:
    """Pass 1: still image + scene audio + ASS captions -> one scene clip."""
    frames = max(int(math.ceil(duration * fps)), 15)
    n = max(frames - 1, 1)
    if zoom_in:
        step = (ZOOM_MAX - 1.0) / n
        zexpr = f"min(zoom+{step:.6f},{ZOOM_MAX})"
    else:
        step = (ZOOM_MAX - ZOOM_OUT_FLOOR) / n
        zexpr = f"if(lt(zoom,1.01),{ZOOM_MAX},max(zoom-{step:.6f},{ZOOM_OUT_FLOOR}))"

    chain = (
        f"scale={PRESCALE_W}:{PRESCALE_H}:force_original_aspect_ratio=increase,"
        f"crop={PRESCALE_W}:{PRESCALE_H},"
        f"zoompan=z='{zexpr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s={OUT_W}x{OUT_H}:fps={fps}"
    )
    if ass:
        chain += f",ass={ass}"

    cmd = [
        "ffmpeg", "-y",
        "-i", image,
        "-i", audio,
        "-filter_complex", f"[0:v]{chain}[v];[1:a]apad[a]",
        "-map", "[v]", "-map", "[a]",
        "-t", f"{duration:.3f}",
        *V_CODEC, *A_CODEC,
        "-r", str(fps),
        "-movflags", "+faststart",
        out_path,
    ]
    _run(cmd, timeout=max(180, int(duration * 30)))


def concat_scenes(scene_paths: list[str], out_path: str) -> None:
    """Pass 2a: join scene clips. Copy first, re-encode only if copy fails."""
    if not scene_paths:
        raise FfmpegError("no scenes to concat")
    if len(scene_paths) == 1:
        # plain remux to normalise faststart
        _run(["ffmpeg", "-y", "-i", scene_paths[0], "-c", "copy",
              "-movflags", "+faststart", out_path], timeout=300)
        return

    list_file = str(Path(out_path).with_suffix(".concat.txt"))
    Path(list_file).write_text(
        "".join(f"file '{p}'\n" for p in scene_paths), encoding="utf-8"
    )
    try:
        _run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
             "-c", "copy", "-movflags", "+faststart", out_path],
            timeout=300,
        )
        return
    except FfmpegError:
        # params *should* match — if the stream copy refuses anyway, fall back
        # to a single concat-filter re-encode (still one pass).
        pass

    inputs: list[str] = []
    for p in scene_paths:
        inputs += ["-i", p]
    segs = "".join(f"[{i}:v][{i}:a]" for i in range(len(scene_paths)))
    fc = f"{segs}concat=n={len(scene_paths)}:v=1:a=1[v][a]"
    _run(
        ["ffmpeg", "-y", *inputs, "-filter_complex", fc,
         "-map", "[v]", "-map", "[a]", *V_CODEC, *A_CODEC,
         "-movflags", "+faststart", out_path],
        timeout=900,
    )


def mix_music(video: str, music: str, out_path: str, volume: float = 0.12) -> None:
    """Mix low-volume background music under the narration. Video stream copied."""
    fc = (
        f"[1:a]volume={volume}[bg];"
        "[0:a][bg]amix=inputs=2:duration=first:normalize=0[a]"
    )
    _run(
        ["ffmpeg", "-y", "-i", video, "-i", music,
         "-filter_complex", fc,
         "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", *A_CODEC,
         "-movflags", "+faststart", out_path],
        timeout=300,
    )


def faststart(src: str, out_path: str) -> None:
    """Remux without re-encoding so the moov atom lands at the front."""
    _run(["ffmpeg", "-y", "-i", src, "-c", "copy",
          "-movflags", "+faststart", out_path], timeout=300)


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0
