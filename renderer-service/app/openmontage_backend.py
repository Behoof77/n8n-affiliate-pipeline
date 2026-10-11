"""OpenMontage (Remotion) render engine behind the same HTTP contract.

Job payload -> edge-tts narration + word timings (v1 tts.py, Hindi WordBoundary)
-> OpenMontage artifacts (edit_decisions + asset_manifest, jsonschema-validated)
-> VideoCompose().execute(operation="render", render_runtime="remotion",
   profile="instagram_reels") => silent 1080x1920 video with Ken-Burns scenes,
   fades, and TikTok-style word-level captions
-> post-mux narration track -> optional music duck-mix (v1 composer) -> mp4.

Audio is deliberately post-muxed instead of using Explainer's audio prop so the
tTS timeline and music mix behave exactly like the v1 ffmpeg path.  On any
OpenMontage-side failure we raise OmRenderError and main.py may fall back to
the v1 ffmpeg+ASS engine — never silently: the response carries "engine".
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import composer
from .tts import DEFAULT_VOICE, tts_scene


class OmRenderError(Exception):
    """OpenMontage-side failure (safe to fall back to the ffmpeg engine)."""


OM_ROOT = Path("/opt/openmontage")
PROFILE = "instagram_reels"  # 1080x1920 via lib/media_profiles.get_profile
REMOTION_TIMEOUT_MS = 120_000  # headless browser setup on a slow VM


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _ff(cmd: list[str], timeout: int = 300) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-20:])
        raise OmRenderError(f"{cmd[0]} exited {proc.returncode}: {tail}")


def _build_narration(audios: list[Path], durs: list[float], out_wav: Path) -> None:
    """Pad each scene's TTS audio to its scene duration and concat."""
    segs: list[Path] = []
    for i, (audio, dur) in enumerate(zip(audios, durs)):
        seg = out_wav.parent / f"narr_seg_{i}.wav"
        _ff([
            "ffmpeg", "-y", "-i", str(audio),
            "-af", f"apad=whole_dur={dur:.3f}",
            "-t", f"{dur:.3f}",
            "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le",
            str(seg),
        ])
        segs.append(seg)
    if len(segs) == 1:
        shutil.move(str(segs[0]), str(out_wav))
        return
    lst = out_wav.parent / "narr_list.txt"
    lst.write_text("\n".join(f"file '{s}'" for s in segs), encoding="utf-8")
    _ff(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c", "copy", str(out_wav)])


def _mux_narration(video: Path, narration: Path, out: Path) -> None:
    """Attach narration; video length wins (apad + -shortest => silent tail OK)."""
    _ff([
        "ffmpeg", "-y",
        "-i", str(video), "-i", str(narration),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-af", "apad", "-shortest", "-movflags", "+faststart",
        str(out),
    ])


def _validate(ed: dict[str, Any], am: dict[str, Any]) -> None:
    """Validate artifacts against OpenMontage's JSON Schemas when available."""
    try:
        import jsonschema  # ships with OpenMontage requirements
    except ImportError:
        return
    base = OM_ROOT / "schemas" / "artifacts"
    for doc, name in (
        (ed, "edit_decisions.schema.json"),
        (am, "asset_manifest.schema.json"),
    ):
        schema_file = base / name
        if not schema_file.is_file():
            continue
        try:
            jsonschema.validate(doc, json.loads(schema_file.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001 - schema violations are all fatal
            raise OmRenderError(f"artifact {name} invalid: {exc}") from exc


# --------------------------------------------------------------------------
# engine entry
# --------------------------------------------------------------------------

async def render(
    req: Any,
    scenes: list[Any],
    job_id: str,
    tmp: Path,
    *,
    resolve_image: Callable[..., Awaitable[Path]],
    resolve_local: Callable[[str, str], Path],
) -> Path:
    """Render the job with OpenMontage; return the final mp4 inside `tmp`.

    Raises RuntimeError (TTS) and HTTPException (image/music resolution) so
    main.py keeps its existing 502/400 semantics; OmRenderError marks failures
    that main.py may hand to the ffmpeg fallback engine.
    """
    voice = req.voice or DEFAULT_VOICE

    # 1) images + per-scene TTS (word timings are scene-local, in seconds)
    image_paths: list[Path] = []
    audio_paths: list[Path] = []
    scene_durs: list[float] = []
    captions: list[dict[str, Any]] = []
    offset = 0.0
    for idx, sc in enumerate(scenes):
        image = await resolve_image(sc, idx, req.images, tmp)
        audio = tmp / f"om_scene_{idx}.mp3"
        words, dur = await tts_scene(sc.text, str(audio), voice)
        scene_dur = max(float(sc.duration), dur + 0.2)
        image_paths.append(image)
        audio_paths.append(audio)
        scene_durs.append(scene_dur)
        if req.captions:
            for w in words:
                captions.append({
                    "text": w.text,
                    "startMs": int(round((offset + w.start) * 1000)),
                    "endMs": int(round((offset + w.end) * 1000)),
                })
        offset += scene_dur

    # 2) single narration timeline aligned to the cut boundaries
    narration = tmp / "narration.wav"
    await _run_blocking(_build_narration, audio_paths, scene_durs, narration)

    # 3) OpenMontage artifacts (schema: version const "1.0", cut transform only
    #    — top-level cut.animation is rejected by additionalProperties:false)
    cuts: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    t = 0.0
    for i, image in enumerate(image_paths):
        end = t + scene_durs[i]
        sid = str(i + 1)
        cuts.append({
            "id": f"scene-{sid}",
            "source": str(image.resolve()),
            "in_seconds": round(t, 3),
            "out_seconds": round(end, 3),
            "transition_in": "fade",
            "transform": {"animation": "ken-burns"},
        })
        assets.append({
            "id": f"asset-scene-{sid}",
            "type": "image",
            "path": str(image.resolve()),
            "source_tool": "renderer-service",
            "scene_id": sid,
        })
        t = end

    edit_decisions: dict[str, Any] = {
        "version": "1.0",
        "cuts": cuts,
        "render_runtime": "remotion",
        "renderer_family": "explainer-data",
        "metadata": {"job_id": job_id, "compose_target": "instagram_reels"},
    }
    asset_manifest: dict[str, Any] = {"version": "1.0", "assets": assets}
    if captions:
        edit_decisions["captions"] = captions

    _validate(edit_decisions, asset_manifest)

    # 4) Remotion render (blocking; runs in a worker thread under RENDER_LOCK)
    raw = tmp / "om_raw.mp4"
    result = await _run_blocking(_execute_compose, edit_decisions, asset_manifest, raw)
    if not getattr(result, "success", False):
        raise OmRenderError(getattr(result, "error", "unknown VideoCompose failure"))
    if not raw.is_file():
        raise OmRenderError(f"remotion produced no output at {raw}")

    # 5) narration mux + optional music duck-mix (identical to v1 semantics)
    with_audio = tmp / "om_with_narration.mp4"
    await _run_blocking(_mux_narration, raw, narration, with_audio)

    final = tmp / "final.mp4"
    if req.music_path:
        music = resolve_local(req.music_path, "music_path")
        await _run_blocking(composer.mix_music, str(with_audio), str(music), str(final))
    else:
        await _run_blocking(composer.faststart, str(with_audio), str(final))
    if not final.is_file():
        raise OmRenderError("post-processing produced no final.mp4")
    return final


def _execute_compose(
    edit_decisions: dict[str, Any], asset_manifest: dict[str, Any], raw: Path
) -> Any:
    from tools.video.video_compose import VideoCompose  # PYTHONPATH=/opt/openmontage

    return VideoCompose().execute({
        "operation": "render",
        "output_path": str(raw),
        "edit_decisions": edit_decisions,
        "asset_manifest": asset_manifest,
        "profile": PROFILE,
        "remotion_timeout_ms": REMOTION_TIMEOUT_MS,
    })


async def _run_blocking(fn: Callable[..., Any], *args: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(fn, *args)
