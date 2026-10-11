"""video-renderer: FastAPI service turning script+images into a 9:16 reel.

POST /render-video  -> synthesises per-scene TTS (edge-tts word timings),
burns karaoke captions, Ken-Burns-zooms the images, joins + mixes music,
and drops the final MP4 in the shared volume (served at /media/...).

One render at a time — the host has ~1 GB RAM, so a second concurrent
render is rejected with 409 instead of queued into an OOM.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import captions, composer, openmontage_backend as om_backend
from .tts import DEFAULT_VOICE, STT_MODE, tts_scene

SHARED = Path(os.getenv("SHARED_DIR", "/data/shared")).resolve()
PUBLIC_ORIGIN = os.getenv("PUBLIC_ORIGIN", "").rstrip("/")

# Render engine: openmontage (Remotion via OpenMontage; 9:16, word captions)
# or ffmpeg (v1 zoompan+ASS).  OM_FALLBACK=1 falls back to ffmpeg on
# OpenMontage-side failures — the response "engine" field always reports
# which engine produced the video (openmontage | ffmpeg | ffmpeg-fallback).
ENGINE = os.getenv("RENDER_ENGINE", "openmontage").strip().lower()
OM_FALLBACK = os.getenv("OM_FALLBACK", "1") == "1"

MAX_SCENES = 20
MAX_SCENE_SECONDS = 60
MAX_IMAGE_BYTES = 25 * 1024 * 1024
JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

app = FastAPI(title="video-renderer", version="1.0.0")
RENDER_LOCK = asyncio.Lock()


class SceneIn(BaseModel):
    scene: int | None = None
    duration: float = Field(default=4.0, ge=1.0, le=MAX_SCENE_SECONDS)
    text: str = Field(min_length=1, max_length=2000)
    image_path: str | None = None  # inside /data/shared (absolute or relative)
    image_url: str | None = None


class RenderRequest(BaseModel):
    job_id: str | None = None
    script: str | None = None          # fallback: sentence-split when no scenes[]
    scenes: list[SceneIn] | None = None
    images: list[str] | None = None    # parallel fallback list for scenes
    voice: str | None = None
    music_path: str | None = None      # inside /data/shared
    captions: bool = True


def _split_script(script: str) -> list[SceneIn]:
    parts = [p.strip() for p in re.split(r"(?<=[।!?])\s+|\n+", script) if p.strip()]
    scenes: list[SceneIn] = []
    for i, part in enumerate(parts, 1):
        words = len(part.split())
        dur = min(max(2.5, words * 0.55), 12.0)
        scenes.append(SceneIn(scene=i, duration=round(dur, 1), text=part))
    return scenes


def _inside_shared(path: Path) -> bool:
    try:
        path.relative_to(SHARED)
        return True
    except ValueError:
        return False


def _resolve_local(src: str, what: str) -> Path:
    p = Path(src)
    if not p.is_absolute():
        p = SHARED / src
    p = p.resolve()
    if not _inside_shared(p):
        raise HTTPException(status_code=400, detail=f"{what} outside shared volume: {src}")
    if not p.is_file():
        raise HTTPException(status_code=400, detail=f"{what} not found: {src}")
    return p


async def _fetch_image(url: str, dest: Path) -> None:
    try:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True,
            headers={"User-Agent": "video-renderer/1.0"},
        ) as client:
            resp = await client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=f"image download failed: {exc}") from exc
    if not resp.content:
        raise HTTPException(status_code=400, detail="downloaded image is empty")
    if len(resp.content) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="image exceeds 25 MB")
    dest.write_bytes(resp.content)


async def _resolve_image(
    sc: SceneIn, idx: int, images: list[str] | None, tmp: Path
) -> Path:
    if sc.image_path:
        return _resolve_local(sc.image_path, f"scene {idx + 1} image_path")
    if sc.image_url:
        dest = tmp / f"src_{idx}.img"
        await _fetch_image(sc.image_url, dest)
        return dest
    if images and idx < len(images) and images[idx]:
        src = images[idx]
        if src.startswith(("http://", "https://")):
            dest = tmp / f"src_{idx}.img"
            await _fetch_image(src, dest)
            return dest
        return _resolve_local(src, f"scene {idx + 1} image")
    raise HTTPException(
        status_code=400,
        detail=f"scene {idx + 1}: no image (need image_path, image_url or images[{idx}])",
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "video-renderer",
            "voice": DEFAULT_VOICE, "stt": STT_MODE, "shared": str(SHARED)}


@app.post("/render-video")
async def render_video(req: RenderRequest) -> dict:
    job_id = req.job_id or f"job_{uuid.uuid4().hex[:16]}"
    if not JOB_RE.match(job_id):
        raise HTTPException(status_code=422, detail="invalid job_id (alnum, -, _ only)")

    scenes = req.scenes or (_split_script(req.script) if req.script else None)
    if not scenes:
        raise HTTPException(status_code=422, detail="provide scenes[] or script")
    if len(scenes) > MAX_SCENES:
        raise HTTPException(status_code=422, detail=f"too many scenes (max {MAX_SCENES})")

    if RENDER_LOCK.locked():
        raise HTTPException(status_code=409, detail="another render is in progress")
    async with RENDER_LOCK:
        tmp = Path(tempfile.mkdtemp(prefix=f"{job_id}_", dir="/tmp"))
        try:
            voice = req.voice or DEFAULT_VOICE
            engine_used = "ffmpeg"
            final_tmp = tmp / "final.mp4"

            if ENGINE in ("openmontage", "om", "remotion"):
                try:
                    final_tmp = await om_backend.render(
                        req, scenes, job_id, tmp,
                        resolve_image=_resolve_image,
                        resolve_local=_resolve_local,
                    )
                    engine_used = "openmontage"
                except om_backend.OmRenderError as exc:
                    if not OM_FALLBACK:
                        raise HTTPException(
                            status_code=500, detail=f"openmontage failed: {exc}"
                        ) from exc
                    print(
                        f"[renderer] openmontage failed, falling back to ffmpeg: {exc}",
                        flush=True,
                    )
                    final_tmp = tmp / "final.mp4"
                    engine_used = "ffmpeg-fallback"

            if engine_used != "openmontage":
                rendered: list[str] = []

                for idx, sc in enumerate(scenes):
                    image = await _resolve_image(sc, idx, req.images, tmp)
                    audio = tmp / f"scene_{idx}.mp3"
                    words, dur = await tts_scene(sc.text, str(audio), voice)
                    scene_dur = max(float(sc.duration), dur + 0.2)

                    ass: str | None = None
                    if req.captions and words:
                        ass_path = tmp / f"scene_{idx}.ass"
                        captions.build_ass(words, str(ass_path))
                        ass = str(ass_path)

                    out = tmp / f"scene_{idx}.mp4"
                    await asyncio.to_thread(
                        composer.render_scene,
                        str(image), str(audio), ass, scene_dur, str(out),
                        zoom_in=(idx % 2 == 0),
                    )
                    rendered.append(str(out))

                joined = tmp / "joined.mp4"
                await asyncio.to_thread(composer.concat_scenes, rendered, str(joined))

                if req.music_path:
                    music = _resolve_local(req.music_path, "music_path")
                    await asyncio.to_thread(
                        composer.mix_music, str(joined), str(music), str(final_tmp)
                    )
                else:
                    await asyncio.to_thread(composer.faststart, str(joined), str(final_tmp))

            SHARED.mkdir(parents=True, exist_ok=True)
            final = SHARED / f"{job_id}.mp4"
            shutil.move(str(final_tmp), str(final))  # cross-device safe

            total = composer.probe_duration(str(final))
            return {
                "status": "ok",
                "job_id": job_id,
                "video_path": str(final),
                "download_url": f"{PUBLIC_ORIGIN}/media/{final.name}" if PUBLIC_ORIGIN else None,
                "duration_sec": round(total, 2),
                "size_bytes": final.stat().st_size,
                "engine": engine_used,
            }
        except HTTPException:
            raise
        except composer.FfmpegError as exc:
            raise HTTPException(status_code=500, detail=f"ffmpeg failed: {exc}") from exc
        except RuntimeError as exc:  # edge-tts / empty audio
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"render failed: {exc}") from exc
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@app.get("/media/{file_path:path}")
async def media(file_path: str) -> FileResponse:
    target = (SHARED / file_path).resolve()
    if not _inside_shared(target) or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target)
