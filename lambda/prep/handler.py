"""Prep Lambda: TTS + S3 image staging + Remotion inputProps assembly.

Mirrors renderer-service openmontage_backend.render steps 1-2 and the
edit_decisions construction of step 3, adapted for renderMediaOnLambda:

  - images: fetched (http(s) or /media filename) then copied into the assets
    bucket with a presigned GET (hotlinked scene 1 images must not expire or
    be blocked at render time)
  - narration: identical ffmpeg apad/concat semantics as _build_narration,
    but uploaded to S3 and referenced as audio.narration.src instead of
    post-muxing (renderMediaOnLambda cannot post-mux)
  - playbooks: unavailable on Lambda; themeConfig comes from the metadata
    fallback only (same branch as _build_theme_from_playbook without a
    playbook), omitted entirely when there is nothing to derive
  - captions: root key of inputProps (Explainer.tsx consumes props.captions;
    the schema strip in _validate was validation-only)

Event  = v1 RenderRequest JSON (job_id, script, scenes, images, voice,
         music_path, captions)
Return = {job_id, inputProps, duration_sec, scene_count}

Errors raise PrepError with the message prefixed "STATUS: " so dispatch can
map to HTTP (400 validation/image, 502 TTS, 500 anything else).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

# ffmpeg/ffprobe from the static layer land in /opt (layer zip root).
os.environ["PATH"] = "/opt:" + os.environ.get("PATH", "")

logger = logging.getLogger()
logger.setLevel(logging.INFO)

JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_SCENE_SECONDS = 60.0
MIN_SCENE_SECONDS = 1.0
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_SCENES = 30
MAX_TEXT = 2000
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}
DEFAULT_VOICE = os.environ.get("PREP_VOICE", "hi-IN-SwaraNeural")
PUBLIC_ORIGIN = os.environ.get("PUBLIC_ORIGIN", "").rstrip("/")
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "")
SIGNED_TTL = int(os.environ.get("SIGNED_TTL", "86400"))
MUSIC_VOLUME = 0.12  # v1 composer.mix_music default (README contract)


class PrepError(Exception):
    """Failure carrying an HTTP-mappable status code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status


# --------------------------------------------------------------------------
# request validation (SceneIn / RenderRequest semantics from main.py)
# --------------------------------------------------------------------------

def _split_script(script: str) -> list[dict[str, Any]]:
    parts = [p.strip() for p in re.split(r"(?<=[।!?])\s+|\n+", script) if p.strip()]
    scenes: list[dict[str, Any]] = []
    for i, part in enumerate(parts, 1):
        words = len(part.split())
        dur = min(max(2.5, words * 0.55), 12.0)
        scenes.append({"scene": i, "duration": round(dur, 1), "text": part})
    return scenes


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PrepError(400, "body must be a JSON object")

    raw_scenes = payload.get("scenes")
    if raw_scenes:
        if not isinstance(raw_scenes, list) or not raw_scenes:
            raise PrepError(400, "scenes must be a non-empty array")
    else:
        script = payload.get("script")
        if not isinstance(script, str) or not script.strip():
            raise PrepError(400, "need scenes[] or a non-empty script")
        raw_scenes = _split_script(script)

    if len(raw_scenes) > MAX_SCENES:
        raise PrepError(400, f"too many scenes (max {MAX_SCENES})")

    scenes: list[dict[str, Any]] = []
    for i, sc in enumerate(raw_scenes):
        if not isinstance(sc, dict):
            raise PrepError(400, f"scene {i + 1}: must be an object")
        text = str(sc.get("text") or "").strip()
        if not text:
            raise PrepError(400, f"scene {i + 1}: text is required")
        if len(text) > MAX_TEXT:
            raise PrepError(400, f"scene {i + 1}: text exceeds {MAX_TEXT} chars")
        try:
            duration = float(sc.get("duration", 4.0))
        except (TypeError, ValueError):
            raise PrepError(400, f"scene {i + 1}: duration must be a number") from None
        if duration < MIN_SCENE_SECONDS or duration > MAX_SCENE_SECONDS:
            raise PrepError(
                400,
                f"scene {i + 1}: duration must be {MIN_SCENE_SECONDS:g}-"
                f"{MAX_SCENE_SECONDS:g} seconds",
            )
        scenes.append(
            {
                "index": i,
                "text": text,
                "duration": duration,
                "image_path": sc.get("image_path"),
                "image_url": sc.get("image_url"),
            }
        )

    job_id = payload.get("job_id")
    if job_id is not None and not (isinstance(job_id, str) and JOB_RE.match(job_id)):
        raise PrepError(400, "job_id must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
    images = payload.get("images")
    if images is not None and not isinstance(images, list):
        raise PrepError(400, "images must be an array")

    return {
        "job_id": job_id or uuid.uuid4().hex[:16],
        "scenes": scenes,
        "images": images if isinstance(images, list) else [],
        "voice": str(payload.get("voice") or DEFAULT_VOICE),
        "music_path": payload.get("music_path"),
        "captions": bool(payload.get("captions", True)),
    }


# --------------------------------------------------------------------------
# image + music acquisition (v1 _resolve_image / _fetch_image semantics)
# --------------------------------------------------------------------------

def _media_url(src: str) -> str:
    """Map a local shared-volume reference to the public /media URL."""
    if src.startswith(("http://", "https://")):
        return src
    if not PUBLIC_ORIGIN:
        raise PrepError(400, "PUBLIC_ORIGIN not configured for local image_path")
    name = src.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if not name:
        raise PrepError(400, f"invalid image reference: {src}")
    return f"{PUBLIC_ORIGIN}/media/{urllib.parse.quote(name)}"


def _fetch(url: str, limit: int = MAX_IMAGE_BYTES) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "video-renderer-lambda/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(limit + 1)
    except Exception as exc:  # noqa: BLE001 - surfaced as 400 like v1 httpx path
        raise PrepError(400, f"image download failed: {exc}") from exc
    if not data:
        raise PrepError(400, "downloaded image is empty")
    if len(data) > limit:
        raise PrepError(400, "image exceeds 25 MB")
    return data


def _scene_image_url(scene: dict[str, Any], images: list[str]) -> str:
    src = scene["image_path"] or scene["image_url"]
    if not src and scene["index"] < len(images) and images[scene["index"]]:
        src = images[scene["index"]]
    if not src:
        idx = scene["index"]
        raise PrepError(400, f"scene {idx + 1}: no image (need image_path, image_url or images[{idx}])")
    return _media_url(str(src))


def _extension_for(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.lower()
    for ext in IMAGE_EXTENSIONS:
        if path.endswith(ext):
            return ext
    return ".png"


def _s3():
    import boto3  # bundled in the python3.11 runtime

    return boto3.client("s3", region_name=os.environ.get("AWS_REGION"))


def _stage_asset(s3, key: str, body: bytes, content_type: str) -> str:
    s3.put_object(Bucket=ASSETS_BUCKET, Key=key, Body=body, ContentType=content_type)
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": ASSETS_BUCKET, "Key": key},
        ExpiresIn=SIGNED_TTL,
    )


_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".webp": "image/webp",
}


# --------------------------------------------------------------------------
# TTS (renderer-service tts.py: edge WordBoundary, no whisper on Lambda)
# --------------------------------------------------------------------------

async def _tts_words(text: str, out_mp3: Path, voice: str) -> list[dict[str, float | str]]:
    import edge_tts

    words: list[dict[str, float | str]] = []
    try:
        communicate = edge_tts.Communicate(text.strip(), voice)
        with open(out_mp3, "wb") as fh:
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    fh.write(chunk["data"])
                elif chunk.get("type") == "WordBoundary":
                    start = float(chunk["offset"]) / 1e7
                    dur = float(chunk["duration"]) / 1e7
                    words.append({"start": start, "end": start + dur, "text": chunk["text"]})
    except PrepError:
        raise
    except Exception as exc:  # noqa: BLE001 - v1 surfaces edge-tts failures as 502
        raise PrepError(502, f"edge-tts failed: {exc}") from exc
    return words


def _audio_duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def _proportional_words(text: str, duration: float) -> list[dict[str, float | str]]:
    tokens = text.split()
    if not tokens or duration <= 0:
        return []
    step = duration / len(tokens)
    return [
        {"start": i * step, "end": (i + 1) * step, "text": t}
        for i, t in enumerate(tokens)
    ]


def _tts_scene(text: str, out_mp3: Path, voice: str) -> tuple[list[dict[str, float | str]], float]:
    words = asyncio.run(_tts_words(text, out_mp3, voice))
    dur = _audio_duration(out_mp3)
    if dur <= 0:
        raise PrepError(502, "edge-tts produced empty audio")
    if not words:
        words = _proportional_words(text, dur)
    return words, dur


# --------------------------------------------------------------------------
# narration build (openmontage_backend._build_narration, byte-for-byte same
# ffmpeg semantics; /tmp paths instead of the shared volume)
# --------------------------------------------------------------------------

def _ff(cmd: list[str], timeout: int = 300) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-20:])
        raise PrepError(500, f"{cmd[0]} exited {proc.returncode}: {tail}")


def _build_narration(audios: list[Path], durs: list[float], out_wav: Path) -> None:
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


# --------------------------------------------------------------------------
# theme (metadata fallback branch of video_compose._build_theme_from_playbook;
# no playbooks ship with the Lambda)
# --------------------------------------------------------------------------

def _theme_from_metadata(edit_decisions: dict[str, Any]) -> dict[str, Any] | None:
    meta = edit_decisions.get("metadata") or {}
    if not meta.get("primary_color"):
        return None
    return {
        "primaryColor": meta["primary_color"],
        "accentColor": meta.get("accent_color", "#F59E0B"),
        "backgroundColor": meta.get("background_color", "#FFFFFF"),
        "surfaceColor": meta.get("surface_color", "#F9FAFB"),
        "textColor": meta.get("text_color", "#1F2937"),
        "mutedTextColor": "#6B7280",
        "headingFont": meta.get("heading_font", "Inter"),
        "bodyFont": meta.get("body_font", "Inter"),
        "monoFont": "JetBrains Mono",
        "chartColors": meta.get("chart_colors", ["#2563EB", "#F59E0B", "#10B981"]),
        "springConfig": {"damping": 20, "stiffness": 120, "mass": 1},
        "transitionDuration": 0.4,
        "captionHighlightColor": meta["primary_color"],
        "captionBackgroundColor": "rgba(255, 255, 255, 0.85)",
    }


# --------------------------------------------------------------------------
# handler
# --------------------------------------------------------------------------

def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    payload = event
    if isinstance(event.get("body"), str):
        try:
            payload = json.loads(event["body"])
        except json.JSONDecodeError as exc:
            raise PrepError(400, f"invalid JSON body: {exc}") from exc

    req = _normalize_payload(payload)
    job_id = req["job_id"]
    tmp = Path("/tmp")
    prefix = f"prep/{job_id}"
    s3 = _s3()

    logger.info("prep job=%s scenes=%d voice=%s", job_id, len(req["scenes"]), req["voice"])

    # 1) per-scene: stage image into S3, synthesise TTS, collect captions
    image_urls: list[str] = []
    audio_paths: list[Path] = []
    scene_durs: list[float] = []
    captions: list[dict[str, Any]] = []
    offset = 0.0
    for sc in req["scenes"]:
        idx = sc["index"]
        img_url = _scene_image_url(sc, req["images"])
        ext = _extension_for(img_url)
        key = f"{prefix}/scene_{idx}{ext}"
        image_urls.append(
            _stage_asset(s3, key, _fetch(img_url), _CONTENT_TYPES.get(ext, "application/octet-stream"))
        )

        mp3 = tmp / f"om_scene_{idx}.mp3"
        words, dur = _tts_scene(sc["text"], mp3, req["voice"])
        scene_dur = max(sc["duration"], dur + 0.2)
        audio_paths.append(mp3)
        scene_durs.append(scene_dur)

        if req["captions"]:
            for w in words:
                captions.append(
                    {
                        "text": w["text"],
                        "startMs": int(round((offset + float(w["start"])) * 1000)),
                        "endMs": int(round((offset + float(w["end"])) * 1000)),
                    }
                )
        offset += scene_dur

    # 2) narration timeline aligned to the cut boundaries, then presign
    narration = tmp / "narration.wav"
    _build_narration(audio_paths, scene_durs, narration)
    narration_key = f"{prefix}/narration.wav"
    narration_url = _stage_asset(s3, narration_key, narration.read_bytes(), "audio/wav")
    for p in [*audio_paths, narration, *tmp.glob("narr_seg_*.wav")]:
        p.unlink(missing_ok=True)

    # 3) edit_decisions (openmontage_backend steps: cuts/assets/version shape)
    cuts: list[dict[str, Any]] = []
    t = 0.0
    for i, source in enumerate(image_urls):
        end = t + scene_durs[i]
        sid = str(i + 1)
        cuts.append(
            {
                "id": f"scene-{sid}",
                "source": source,
                "in_seconds": round(t, 3),
                "out_seconds": round(end, 3),
                "transition_in": "fade",
                "transform": {"animation": "ken-burns"},
            }
        )
        t = end

    input_props: dict[str, Any] = {
        "version": "1.0",
        "cuts": cuts,
        "render_runtime": "remotion",
        "renderer_family": "explainer-data",
        "metadata": {"job_id": job_id, "compose_target": "instagram_reels"},
    }
    if captions:
        input_props["captions"] = captions
    theme = _theme_from_metadata(input_props)
    if theme:
        input_props["themeConfig"] = theme
    input_props["audio"] = {"narration": {"src": narration_url, "volume": 1.0}}

    # 4) optional music (v1 mixes at 0.12; Explainer gets a flat layer instead)
    if req["music_path"]:
        music_url_raw = _media_url(str(req["music_path"]))
        path_lower = urllib.parse.urlsplit(music_url_raw).path.lower()
        music_ext = next(
            (e for e in (".mp3", ".m4a", ".wav", ".ogg", ".aac") if path_lower.endswith(e)),
            ".mp3",
        )
        music_url = _stage_asset(
            s3,
            f"{prefix}/music{music_ext}",
            _fetch(music_url_raw, limit=100 * 1024 * 1024),
            "application/octet-stream",
        )
        input_props["audio"]["music"] = {
            "src": music_url,
            "volume": MUSIC_VOLUME,
            "loop": True,
            "fadeInSeconds": 2,
            "fadeOutSeconds": 3,
        }

    duration_sec = round(t + 1.0, 2)  # calculateMetadata pads +1s for final fade
    logger.info("prep job=%s duration=%.2fs cuts=%d", job_id, duration_sec, len(cuts))
    return {"job_id": job_id, "inputProps": input_props, "duration_sec": duration_sec, "scene_count": len(cuts)}
