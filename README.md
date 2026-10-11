# n8n-affiliate-pipeline

Meesho trending products → approved reel script → AI scene images → rendered
Hindi short (TTS + karaoke captions + Ken-Burns 9:16) → human-approved publish
to YouTube + Instagram Reels. Every stage is gated by an email approve/reject
link (human-in-the-loop).

## Architecture

```
schedule (daily 09:30 IST)
  └─ meesho-scraper ──► product review email ──► [approve]
       └─ GPT script ──► script review email ──► [approve]
            └─ DALL-E 3 scene images ──► image review email ──► [approve]
                 └─ video-renderer (edge-tts + ffmpeg) ──► video review email ──► [approve]
                      └─ promo copy ──► publish review email ──► [approve]
                           ├─ YouTube upload (unlisted/private first)
                           └─ Instagram Reels container → publish → first comment
```

All services run in one Docker Compose network (`pipeline`) on the Oracle VM and
are exposed only through nginx behind HTTPS at `https://agenticflow1.duckdns.org`.

| Service | Container | Host port (loopback) | Role |
|---|---|---|---|
| n8n | `n8n` | 127.0.0.1:5678 | Orchestration + HITL wait/resume |
| video-renderer | `video-renderer` | 127.0.0.1:8010 → 8000 | FastAPI: TTS, ASS captions, ffmpeg Ken-Burns, 9:16 MP4 |
| meesho-scraper | `meesho-scraper` | 127.0.0.1:5001 → 5000 | FastAPI + Playwright: trending products, return-risk filter |
| nginx (host) | — | 80/443 | HTTPS, `/` → n8n, `/media/` → renderer |

Shared media (scene images, rendered MP4s) lives in `./shared` → `/data/shared`
in every container; renderer serves previews at `/media/*` which nginx exposes
publicly (used by the IG `video_url`).

## Repo layout

```
workflows/meesho-reels-pipeline.json   # 49-node 5-stage HITL workflow (importable)
workflows/../scripts/build_workflow.py # regenerates the JSON (no hand-escaped JSON)
scripts/validate_workflow.py           # structural validation (refs, IF wiring)
meesho-scraper/                        # FastAPI scraper service + Dockerfile
renderer-service/                      # FastAPI video renderer + Dockerfile
docker-compose.yml                     # 3 services, loopback ports, healthchecks
nginx/agenticflow1.conf                # 80→443, n8n, /media/
deploy.sh                              # idempotent VM deployment (see DEPLOY.md)
.env.example                           # config template (real .env is gitignored)
```

## Quick start

```bash
cp .env.example .env      # fill N8N_DOMAIN, N8N_ENCRYPTION_KEY, DUCKDNS_*, NOTIFY_EMAIL
./deploy.sh               # full VM deployment + workflow import
```

Then follow **DEPLOY.md** for the owner account, credentials, and verification gates.

## Workflow notes

- Wait nodes use `resume: webhook` + `GET`; approve/reject links are
  `$execution.resumeUrl?action=approve|reject&stage=<product|script|images|video|publish>&job_id=...`
- `Parse Decision:*` nodes deep-scan the resumed payload for `action`/`stage`/`job_id`
  and **default to reject** on anything unrecognized (fail-safe).
- Scene 1 uses the product's own image when available; remaining scenes are DALL-E 3
  (`1024x1792`, 9:16). Renderer mixes in background music at 0.12 volume when provided.
- `Set YouTube Public` and YouTube upload are best-effort (`onError: continueRegularOutput`);
  the publish email tells you the manual fallback. Instagram requires `Config.ig_user_id`
  and a Meta Graph header-auth token.

## Security

- `.env` (DuckDNS token, encryption key) is gitignored — never commit it.
- All containers bind to 127.0.0.1; only nginx is reachable from the internet.
- n8n credentials are encrypted at rest with `N8N_ENCRYPTION_KEY`.
