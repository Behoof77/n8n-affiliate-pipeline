# video-renderer

FastAPI service that turns a script + images into a vertical (9:16) reel:
per-scene edge-tts narration with exact word timings, karaoke captions
(active word highlighted yellow, dead-centre), Ken-Burns zoom on every image,
optional low-volume background music, 1080x1920 @ 30 fps H.264 MP4 out.

## Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/render-video` | Render a reel (see body below). Returns JSON with `video_path` + `download_url`. |
| GET | `/media/{path}` | Serve a file from the shared volume (final MP4s, scene images). |
| GET | `/health` | Liveness. |

### `POST /render-video`

```json
{
  "job_id": "job_1728600000000",
  "scenes": [
    {"scene": 1, "duration": 4, "text": "क्या आप भी...", "image_path": "images/job_x/scene_0.png"},
    {"scene": 2, "duration": 5, "text": "...", "image_url": "https://..."}
  ],
  "voice": "hi-IN-SwaraNeural",
  "music_path": "music/bgm.mp3",
  "captions": true
}
```

- `scenes[].image_path` is resolved **inside** `/data/shared` (relative or
  absolute); `image_url` is downloaded (http/https). `images: []` is a
  parallel fallback list when scenes only carry `text`.
- No `scenes`? Pass `script` — it is sentence-split automatically.
- Response: `{"status":"ok","job_id","video_path","download_url","duration_sec","size_bytes"}`
  `download_url` is `{PUBLIC_ORIGIN}/media/{job_id}.mp4`.
- Errors: **409** another render running (only one at a time — small host),
  **502** TTS failure, **500** ffmpeg failure, **400/422** bad input.

## Pipeline

1. `app/tts.py` — edge-tts streams `WordBoundary` events (exact timings, free).
   `RENDERER_STT=whisper` switches to faster-whisper tiny/int8 word timestamps;
   proportional spread is the last resort.
2. `app/captions.py` — ASS karaoke file per scene (yellow active / white
   pending, Noto Sans for Devanagari, size 76, centred, cards of ≤4 words).
3. `app/composer.py` — per scene: cover-scale → zoompan (alternating
   zoom-in / zoom-out per scene, motion completes exactly over the clip) + ASS
   burn + padded scene audio, all encoded with identical params → concat
   `-c copy` (single re-encode fallback) → optional music mix at volume 0.12
   (video stream copied) → faststart MP4 into `/data/shared`.

## Env

| Var | Default | Meaning |
|---|---|---|
| `RENDERER_VOICE` | `hi-IN-SwaraNeural` | edge-tts voice (`hi-IN-MadhurNeural` alt) |
| `RENDERER_STT` | `edge` | `edge` \| `whisper` word-timing source |
| `SHARED_DIR` | `/data/shared` | shared volume root (outputs + media) |
| `PUBLIC_ORIGIN` | empty | e.g. `https://agenticflow1.duckdns.org` for download URLs |

## Run (container)

Built by the root `docker-compose.yml`; manual:

```bash
docker build -t video-renderer ./renderer-service
docker run --rm -p 8010:8000 -v n8n_shared_media:/data/shared video-renderer
```

## Known constraints

- One render at a time (host has ~1 GB RAM).
- Images are cover-cropped to 9:16 (Meesho product photos are centre-framed;
  DALL-E 1024x1792 output is nearly full-bleed already).
- Meesho/n8n job ids are predictable by design (n8n writes scene images itself),
  so `/media/` paths are unguessable only per-job — treat the origin as
  semi-public; final reels get published anyway.
