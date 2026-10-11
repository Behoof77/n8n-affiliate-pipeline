# Remotion Lambda Migration — Architecture & Runbook

Task: offload video rendering from the 956 MB Oracle VM to Remotion Lambda (AWS,
ap-south-1), then decommission the local `video-renderer` container. Motivation:
the validated local OM render of an 8s reel takes ~668 s and 1.3-1.7 GB swap — a
standing OOM risk to n8n.

Decisions (user-locked):
- Audio architecture A: Lambda-native audio. edge-tts pre-render Lambda produces
  narration.mp3 + word timings; Explainer consumes them via its built-in
  `audio` prop; the returned MP4 already contains audio. Zero local render stack.
- AWS IAM user (programmatic) created by user; keys delivered out-of-band; stored
  only in env vars / gitignored .env. Never committed.
- Sheet ordering is binding: Lambda verified first -> n8n Stage-4/5 update
  (export/backup workflow JSON first) -> container removal. Push authorized.

## Composition facts (verified against OM_SHA 9327439d ref clone)

- `Explainer.tsx` already supports `audio?: {narration?: {src, volume?}, music?:
  {src, volume?, fadeInSeconds?, fadeOutSeconds?, offsetSeconds?, loop?}}`
  (interfaces 287-311, rendering 887-902) and `captions?: WordCaption[]`.
- `src/lib/resolveAsset.ts` passes `http://`, `https://`, `data:` URLs through
  unmodified -> S3-hosted media works with ZERO composition changes.
- Production validates fps=15 (local ref clone is fps=30; Dockerfile seds
  Root.tsx lines 128/132/142). The Lambda site build MUST apply the same patch.
- Composition id `Explainer`, profile instagram_reels = 1080x1920, cuts use
  absolute seconds, captions are absolute-ms WordCaption {text,startMs,endMs}.

## Target architecture

```
n8n Stage 4 (unchanged Assemble, image_path -> absolute URL)
  -> HTTP Request "Render Video" -> API Gateway (HTTP API, POST /render, API-key)
  -> dispatch Lambda (Node 20, @remotion/lambda)
       1. invoke prep Lambda (async request/response)
       2. renderMediaOnLambda(serveUrl, "Explainer", inputProps, codec h264,
          privacy private)
       3. poll getRenderProgress until done
       4. presign GET for the S3 MP4 (1 h) -> JSON response
  <- same response shape as today's renderer (see contract)
  -> new nodes: Download Rendered Video (HTTP, binary) + Write Video File
     (/data/shared/<job_id>.mp4)  [stage 5 Read Video File keeps working]
  -> Stage 5 unchanged (YouTube upload reads local path)

prep Lambda (Python 3.11 + edge-tts):
  input  {job_id, scenes[{duration,text,image_url}], voice, captions}
  - downloads scene images (https) -> S3 renders/<job>/s<n>.png
  - edge-tts narration.mp3 (hi-IN default) + WordBoundary events ->
    captions WordCaption[] (absolute ms)
  - builds edit_decisions + asset_manifest exactly like
    renderer-service/app/openmontage_backend.py, but source/audio = S3 HTTPS URLs
  - output inputProps {cuts, captions, audio:{narration:{src}}, ...} + S3 refs
```

Why this split: `@remotion/lambda` is Node-only; edge-tts is Python-only. The
Python prep Lambda owns ALL validated business logic (TTS timing, cut math,
caption conversion); the Node dispatch Lambda is a thin render API wrapper.

## Contracts

### Render request (n8n -> API GW -> dispatch) — compatible superset of v1
`{job_id, scenes:[{scene, duration, text, image_url}], voice, captions:true}`
Stage-4 Assemble change: replace `image_path` with
`image_url = cfg.public_origin + '/media/' + image_path` (one-line edit in
scripts/build_workflow.py assemble_js). Requires nginx to serve /data/shared
at /media AFTER the renderer container is removed (see nginx section) — the
renderer's own /media must stay available until then.

### Render response (dispatch -> Build Video Email, UNCHANGED consumer)
`{status:"ok", job_id, video_path:"/data/shared/<job_id>.mp4",
  download_url:"<presigned S3 GET, 1h>", duration_sec, size_bytes,
  engine:"remotion-lambda"}`
- `status` is checked; `download_url` drives the email <video>/<a>;
- `video_path` drives Stage-5 Read Video File after the new download+write
  nodes materialize that exact path;
- error path: `{status:"error", error:"..."}` (Build Video Email throws).

## n8n workflow changes (LIVE 49-node workflow — export JSON backup FIRST)

1. Backup: docker exec n8n ... export via REST / GET workflow JSON, store
   dated copy in repo backups/ + on VM.
2. Assemble Render Payload: image_path -> full /media URL (line ~503).
3. Render Video node: url -> API GW invoke URL; keep httpHeaderAuth credential
   (x-api-key); jsonBody unchanged; timeout 900000 kept (Lambda path should
   finish in ~1-4 min).
4. INSERT after "Build Video Email" (before Stage-5 "Read Video File"):
   - "Download Rendered Video" (httpRequest, GET download_url,
     response.response.fileProperty -> binary "data", timeout 120000)
   - "Write Video File" (readWriteFile write,
     fileName=/data/shared/{{ $('Render Video').first().json.job_id }}.mp4)
   "Read Video File" already reads
   `$('Render Video').first().json.video_path` -> stays valid.
5. Re-import or apply via n8n UI; re-link credentials.

## nginx (agenticflow1) — serve /media from shared volume

Stage-3 "Build Image Email" embeds public_origin+/media/img_*.png; prep Lambda
also fetches those URLs. After renderer removal, add to the agenticflow1 vhost:
`location /media/ { alias /data/shared/; }` and mount the shared volume into the
nginx container (compose edit). Verify: GET https://agenticflow1.duckdns.org/media/smoke1.png
= 200 BEFORE removing the renderer. Exposure identical to today (renderer already
served these files publicly).

## VM cleanup (LAST, after Lambda + n8n + nginx verified)

1. docker-compose.yml: delete video-renderer service block.
2. docker stop video-renderer && docker rm video-renderer
3. docker rmi <image id>; docker builder prune -f
4. Verify target state: only n8n (1.123.84), meesho-scraper, nginx containers;
   openalgo.service + kronos.service + skopaq vhost untouched; RAM/swap free;
   gates: agenticflow1=200, /setup=200, skopaq=200, /media png=200, stage-5
   video file lands on VM during first real run.

## AWS bootstrap (when keys arrive; from local machine, Node v24 present)

- Env: REMOTION_AWS_ACCESS_KEY_ID / REMOTION_AWS_SECRET_ACCESS_KEY,
  REMOTION_AWS_REGION=ap-south-1 (never commit).
- IAM user needs (bootstrap-grade; remotion CLI auto-provisions CF+roles):
  AmazonS3FullAccess, AWSLambda_FullAccess, CloudFormationFullAccess,
  AmazonAPIGatewayAdministrator, IAM Full (CreateRole/PassRole for the Lambda
  execution role remotion provisions). Can be tightened post-bootstrap.
- Site build script (scripts/lambda_deploy.sh, mirrors Dockerfile pattern —
  no vendoring of OM source): clone OpenMontage@OM_SHA -> apply fps-15 sed
  (Root.tsx 128/132/142 + grep guards) -> npm ci -> npx remotion lambda sites
  create (capture serveUrl) -> npx remotion lambda functions deploy
  (arm64, memory 2048, timeout ~900) -> package + deploy prep (Python) and
  dispatch (Node) Lambdas -> create HTTP API + API key + /render route.
- Functions layout (new, in repo):
  lambda/prep/     (Python 3.11; handler.py ported from openmontage_backend
                    cut/caption/TTS logic; requirements: edge-tts, boto3)
  lambda/dispatch/ (Node; index.mjs: invoke prep, renderMediaOnLambda,
                    getRenderProgress poll, presign; package.json @remotion/lambda)
  scripts/lambda_deploy.sh (site build + aws lambda deploy + API GW wiring)
- E2E verify from local machine BEFORE touching n8n: POST sample payload with
  /media image URLs -> expect presigned MP4 (h264 1080x1920, audio track from
  edge-tts, word captions rendered).

## Risks / notes

- API GW HTTP API supports long integrations (<=30 min); REST API's 29 s cap
  must NOT be used. n8n keeps its 900 s node timeout.
- Remotion Lambda cost: Lambda+S3+CloudWatch per render (cents per reel);
  concurrency default is modest — first run may queue.
- scene 1 uses the scraper's original product.images[0] (public URL, no /media)
  — prep Lambda must tolerate hotlinking by copying to S3 during prep.
- music_path (v1 contract field): not used by current stage-4 payload; prep
  accepts music S3 URL when provided later (Explainer supports fades/loop).
- Container removal frees ~330 MB+ during render and eliminates the swap storm
  that starved skopaq (observed 19.5 s response during render).
