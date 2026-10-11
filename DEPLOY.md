# Deployment Guide

Deploys n8n + video-renderer + meesho-scraper on the Oracle VM behind HTTPS at
`https://agenticflow1.duckdns.org`, without touching the live `openalgo`,
`kronos`, or `skopaq` services.

## What deploy.sh does

| Step | Action | Safety |
|---|---|---|
| 1 | Removes `~/200` and `~/404` junk dirs only | Never touches `/etc/nginx` sites for skopaq, `openalgo.service`, `kronos.service` |
| 2 | Creates 2G swapfile (VM has 956 MiB RAM) | Idempotent, skipped if swap already active |
| 3 | Installs `docker.io`, `nginx`, `certbot`; resolves compose command | Adds packages only |
| 4 | DuckDNS A-record update + 6-hour refresh cron | Uses token from `.env` |
| 5 | Bootstraps HTTP-only nginx site for `agenticflow1.duckdns.org` | Separate site file; skopaq vhost untouched |
| 6 | `certbot certonly --webroot` for the new domain | Webroot, no standalone port conflicts |
| 7 | Installs final `nginx/agenticflow1.conf` (80→443, `/`→5678, `/media/`→8010) | Reload, not restart |
| 8 | `docker compose up -d --build` | Loopback ports only (5678/8010/5001) |
| 9 | Imports `workflows/meesho-reels-pipeline.json` (substitutes `__PUBLIC_ORIGIN__`) | Workflow imported inactive (`active: false`) |

## Prerequisites

- `.env` filled in (see `.env.example`). Real token lives only in `.env` (gitignored).
- Repo cloned to the VM, e.g. `~/n8n-affiliate-pipeline`.
- Ports 80/443 open (already proven by skopaq).

## Run

```bash
cd ~/n8n-affiliate-pipeline
chmod +x deploy.sh
./deploy.sh
```

Re-running is safe/idempotent.

## After first deploy (manual, once)

1. Open `https://agenticflow1.duckdns.org/` and create the n8n **owner account**.
2. Edit `.env` → `NOTIFY_EMAIL` = your real mailbox (pipeline approval emails), then
   `docker compose up -d` to re-create n8n with it (or set it inside the Config node).
3. In n8n, open the imported **Meesho Reels Pipeline** workflow and attach credentials
   to every node marked `REPLACE_AFTER_IMPORT`:

   | Credential type | Used by | How |
   |---|---|---|
   | Gmail OAuth2 | all `Email:*` nodes | n8n → Credentials → Gmail |
   | Header Auth (`X-OpenAI-Key` = `Bearer <key>`) | `Write Reel Script`, `Generate Scene Image`, `Generate Promo Copy` | Header name `Authorization`, value `Bearer sk-...` |
   | YouTube OAuth2 | `Upload YouTube`, `Set YouTube Public` | Google Cloud project + YouTube Data API v3 |
   | Header Auth | `Create IG Reel Container`, `Publish IG Container`, `Post First Comment` | Header `Authorization`, value `Bearer <meta-access-token>`; set `Config.ig_user_id` |

4. Set `Config.notify_email`, `Config.ig_user_id`, and `Config.yt_privacy` in the
   workflow's **Config** node.
5. Run once manually (click **Execute workflow**), verify the Wait-resume links work,
   then activate.

## Hard verification gates

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://agenticflow1.duckdns.org/   # expect 200/302
curl -s -o /dev/null -w "%{http_code}\n" https://skopaq.duckdns.org/         # expect 200 (must not regress)
docker ps                                                                      # 3 containers Up (healthy)
docker exec n8n curl -s http://localhost:5678/healthz                          # ok
curl -s http://127.0.0.1:8010/health                                           # renderer
curl -s http://127.0.0.1:5001/health                                           # scraper
```

### Wait-resume query-param shape (UNVERIFIED until first live run)

The workflow's `Parse Decision:*` nodes deep-scan `$json` for `action`/`stage`/`job_id`
at any depth and default to **reject**, so an unexpected payload shape fails safe.
To confirm on first run: approve from the email, check the execution output of
`Parse Decision: Product` — if `action` is not found there, adjust the code node to
the actual shape n8n returns for `resume: webhook` + `httpMethod: GET`.

## Rollback

```bash
docker compose down                # stops only pipeline containers
rm /etc/nginx/sites-enabled/agenticflow1.conf && systemctl reload nginx
# skopaq/openalgo/kronos are never modified by deploy.sh
```
