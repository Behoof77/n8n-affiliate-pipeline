"""Build workflows/meesho-reels-pipeline.json programmatically.

Avoids hand-written JSON escaping errors. Run: python scripts/build_workflow.py
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "workflows", "meesho-reels-pipeline.json")

NODES = []
CONN = {}
_IDX = [0]


def nid():
    _IDX[0] += 1
    return "11111111-1111-4111-8111-%012d" % (111111111100 + _IDX[0])


def node(name, ntype, tv, params, x, y, **kw):
    n = {
        "id": nid(),
        "name": name,
        "type": ntype,
        "typeVersion": tv,
        "position": [x, y],
        "parameters": params,
    }
    n.update(kw)
    NODES.append(n)
    return n


def link(src, dst, out=0):
    c = CONN.setdefault(src, {})
    m = c.setdefault("main", [])
    while len(m) <= out:
        m.append([])
    m[out].append({"node": dst, "type": "main", "index": 0})


def chain(names):
    for a, b in zip(names, names[1:]):
        link(a, b)


def x_at(i):
    return -720 + 220 * i


def code(name, js, x, y, **kw):
    return node(name, "n8n-nodes-base.code", 2, {"jsCode": js}, x, y, **kw)


def http(name, params, x, y, creds=None, **kw):
    if creds:
        kw["credentials"] = creds
    return node(name, "n8n-nodes-base.httpRequest", 4.2, params, x, y, **kw)


def gmail(name, subject, html_expr, x, y):
    params = {
        "resource": "message",
        "operation": "send",
        "sendTo": "={{ $('Config').first().json.notify_email }}",
        "subject": subject,
        "message": html_expr,
        "emailType": "html",
        "options": {},
    }
    return node(
        name,
        "n8n-nodes-base.gmail",
        2.1,
        params,
        x,
        y,
        credentials={"gmailOAuth2": {"id": "REPLACE_AFTER_IMPORT", "name": "Gmail account"}},
    )


def wait_hook(name, webhook_id, x, y):
    return node(
        name,
        "n8n-nodes-base.wait",
        1.1,
        {"resume": "webhook", "httpMethod": "GET", "options": {}},
        x,
        y,
        webhookId=webhook_id,
    )


PARSE_DECISION_JS = r"""
const raw = $input.first().json || {};
const hits = {};
const walk = (o) => {
  if (!o || typeof o !== 'object') return;
  for (const k of Object.keys(o)) {
    const v = o[k];
    const key = String(k).toLowerCase();
    if (['action', 'stage', 'job_id', 'jobid'].includes(key) &&
        (typeof v === 'string' || typeof v === 'number') && !(key in hits)) {
      hits[key] = v;
    }
    if (v && typeof v === 'object') walk(v);
  }
};
walk(raw);
let action = String(hits.action || 'reject').toLowerCase();
if (['approve', 'approved', 'yes', 'ok', 'accept', 'true'].includes(action)) {
  action = 'approve';
} else {
  action = 'reject';
}
const stage = String(hits.stage || 'unknown');
const job_id = hits.job_id || hits.jobid || $('Init Job').first().json.job_id;
return [{ json: { action: action, stage: stage, job_id: job_id } }];
"""


def parse_decision(name, x, y):
    return code(name, PARSE_DECISION_JS.strip(), x, y)


def if_node(name, x, y):
    params = {
        "conditions": {
            "options": {
                "caseSensitive": True,
                "leftValue": "",
                "typeValidation": "strict",
                "version": 2,
            },
            "conditions": [
                {
                    "id": "cond-approve",
                    "leftValue": "={{ $json.action }}",
                    "rightValue": "approve",
                    "operator": {"type": "string", "operation": "equals"},
                }
            ],
            "combinator": "and",
        },
        "options": {},
    }
    return node(name, "n8n-nodes-base.if", 2.2, params, x, y)


def resume_links(stage):
    return (
        "<p><a href='${base}?action=approve&stage=" + stage
        + "&job_id=${job_id}'>APPROVE</a> | "
        "<a href='${base}?action=reject&stage=" + stage
        + "&job_id=${job_id}'>REJECT</a></p>"
    )


# --------------------------------------------------------------------------
# Stage 1: product review (row y=480)
# --------------------------------------------------------------------------
Y1 = 480
ROW1 = [
    "Schedule Trigger",
    "Config",
    "Init Job",
    "Fetch Trending Products",
    "Build Product Email",
    "Email: Product Review",
    "Wait: Product",
    "Parse Decision: Product",
    "IF: Product Approved",
]


def stage1():
    node(
        "Schedule Trigger",
        "n8n-nodes-base.scheduleTrigger",
        1.2,
        {"rule": {"interval": [{"field": "days", "triggerAtHour": 9, "triggerAtMinute": 30}]}},
        x_at(0), Y1,
    )
    node(
        "Config",
        "n8n-nodes-base.set",
        3.4,
        {
            "assignments": {
                "assignments": [
                    {"id": "a1", "name": "notify_email", "value": "you@example.com", "type": "string"},
                    {"id": "a2", "name": "voice", "value": "hi-IN-SwaraNeural", "type": "string"},
                    {"id": "a3", "name": "yt_privacy", "value": "private", "type": "string"},
                    {"id": "a4", "name": "ig_user_id", "value": "", "type": "string"},
                    {"id": "a5", "name": "openai_model", "value": "gpt-4o-mini", "type": "string"},
                    {"id": "a6", "name": "scraper_limit", "value": 5, "type": "number"},
                    {"id": "a7", "name": "public_origin", "value": "__PUBLIC_ORIGIN__", "type": "string"},
                ]
            },
            "options": {},
        },
        x_at(1), Y1,
    )
    code(
        "Init Job",
        "return [{ json: { job_id: 'job_' + Date.now() } }];",
        x_at(2), Y1,
    )
    http(
        "Fetch Trending Products",
        {
            "url": "=http://meesho-scraper:5000/trending?limit={{ $('Config').first().json.scraper_limit }}",
            "options": {"timeout": 300000},
        },
        x_at(3), Y1,
    )
    build_product_js = r"""
const cfg = $('Config').first().json;
const job_id = $('Init Job').first().json.job_id;
const payload = $input.first().json || {};
let list = [];
if (Array.isArray(payload)) list = payload;
else if (Array.isArray(payload.products)) list = payload.products;
else if (Array.isArray(payload.items)) list = payload.items;
if (!list.length) throw new Error('Scraper returned no products');
const base = $execution.resumeUrl;
const cards = list.map((it, i) => {
  const name = it.title || it.name || ('Product ' + (i + 1));
  const price = it.price || '';
  const rating = it.rating || '';
  const reviews = it.reviews != null ? it.reviews : (it.review_count != null ? it.review_count : '');
  const img = (Array.isArray(it.images) && it.images[0]) || it.image || '';
  const url = it.url || it.link || '#';
  return '<div style="border:1px solid #ddd;border-radius:8px;padding:12px;margin:8px 0;font-family:sans-serif">' +
    '<b>#' + (i + 1) + ' ' + name + '</b><br/>Price: ' + price + ' | Rating: ' + rating + ' | Reviews: ' + reviews +
    '<br/><a href="' + url + '">Open product</a>' +
    (img ? '<br/><img src="' + img + '" width="120"/>' : '') + '</div>';
}).join('');
const html = '<p>Trending Meesho products for today. The FIRST product in the list will be used for the reel.</p>' +
  cards +
  "<p><a href='" + base + "?action=approve&stage=product&job_id=" + job_id + "'>APPROVE</a> | " +
  "<a href='" + base + "?action=reject&stage=product&job_id=" + job_id + "'>REJECT</a></p>";
return [{ json: { job_id: job_id, products: list, product: list[0], html: html } }];
"""
    code("Build Product Email", build_product_js.strip(), x_at(4), Y1)
    gmail(
        "Email: Product Review",
        "Review products for today's reel",
        "={{ $('Build Product Email').first().json.html }}",
        x_at(5), Y1,
    )
    wait_hook("Wait: Product", "wait-product", x_at(6), Y1)
    parse_decision("Parse Decision: Product", x_at(7), Y1)
    if_node("IF: Product Approved", x_at(8), Y1)
    chain(ROW1)


# --------------------------------------------------------------------------
# Stage 2: script review (row y=340)
# --------------------------------------------------------------------------
Y2 = 340
ROW2 = [
    "Write Reel Script",
    "Parse Script",
    "Build Script Email",
    "Email: Script Review",
    "Wait: Script",
    "Parse Decision: Script",
    "IF: Script Approved",
]


def stage2():
    system_msg = (
        "You are a short-form video scriptwriter. "
        "Write a 30-45 second Hindi reel script for the given product. "
        "Return ONLY valid JSON with keys: hook (string, first 3 seconds attention grabber), "
        "scenes (array of exactly 6 objects, each with scene (number), duration (seconds, 3-6), "
        "text (Hindi narration for that scene), image_prompt (English visual description)), "
        "cta (string, closing call to action)."
    )
    json_body = (
        "={{ JSON.stringify({"
        " model: $('Config').first().json.openai_model,"
        " temperature: 0.8,"
        " response_format: { type: 'json_object' },"
        " messages: ["
        "  { role: 'system', content: " + json.dumps(system_msg) + " },"
        "  { role: 'user', content: 'Product data: ' + JSON.stringify($('Build Product Email').first().json.product) + ' Write the reel script JSON now.' }"
        " ]"
        "}) }}"
    )
    http(
        "Write Reel Script",
        {
            "method": "POST",
            "url": "https://api.openai.com/v1/chat/completions",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": json_body,
            "options": {"timeout": 120000},
        },
        x_at(0), Y2,
        creds={"httpHeaderAuth": {"id": "REPLACE_AFTER_IMPORT", "name": "OpenAI API"}},
    )
    parse_script_js = r"""
const resp = $input.first().json;
let parsed;
try {
  parsed = JSON.parse(resp.choices[0].message.content);
} catch (e) {
  throw new Error('Script JSON parse failed: ' + e.message);
}
if (!parsed.hook || !parsed.cta || !Array.isArray(parsed.scenes) || parsed.scenes.length < 2) {
  throw new Error('Script missing hook/cta/scenes (need at least 2 scenes)');
}
const scenes = parsed.scenes.map((s, i) => ({
  scene: i + 1,
  duration: Math.min(60, Math.max(1, Number(s.duration) || 4)),
  text: String(s.text || ''),
  image_prompt: String(s.image_prompt || 'clean product advertising photo'),
}));
return [{ json: {
  job_id: $('Init Job').first().json.job_id,
  script: { hook: String(parsed.hook), scenes: scenes, cta: String(parsed.cta) },
  product: $('Build Product Email').first().json.product,
} }];
"""
    code("Parse Script", parse_script_js.strip(), x_at(1), Y2)
    build_script_email_js = r"""
const { job_id, script, product } = $input.first().json;
const base = $execution.resumeUrl;
const rows = script.scenes.map((s) =>
  '<tr><td>' + s.scene + '</td><td>' + s.duration + 's</td><td>' + s.text + '</td><td>' + s.image_prompt + '</td></tr>'
).join('');
const html = '<p><b>Product:</b> ' + (product.title || product.name || '') + '</p>' +
  '<p><b>Hook:</b> ' + script.hook + '</p>' +
  '<table border="1" cellpadding="6" style="border-collapse:collapse;font-family:sans-serif">' +
  '<tr><th>#</th><th>Dur</th><th>Text</th><th>Image prompt</th></tr>' + rows + '</table>' +
  '<p><b>CTA:</b> ' + script.cta + '</p>' +
  "<p><a href='" + base + "?action=approve&stage=script&job_id=" + job_id + "'>APPROVE</a> | " +
  "<a href='" + base + "?action=reject&stage=script&job_id=" + job_id + "'>REJECT</a></p>";
return [{ json: { job_id: job_id, html: html } }];
"""
    code("Build Script Email", build_script_email_js.strip(), x_at(2), Y2)
    gmail(
        "Email: Script Review",
        "Review reel script",
        "={{ $('Build Script Email').first().json.html }}",
        x_at(3), Y2,
    )
    wait_hook("Wait: Script", "wait-script", x_at(4), Y2)
    parse_decision("Parse Decision: Script", x_at(5), Y2)
    if_node("IF: Script Approved", x_at(6), Y2)
    chain(ROW2)


# --------------------------------------------------------------------------
# Stage 3: image review (row y=200)
# --------------------------------------------------------------------------
Y3 = 200
ROW3 = [
    "Prepare Scene Images",
    "Generate Scene Image",
    "Convert Images to Binary",
    "Write Scene Files",
    "Build Image Email",
    "Email: Image Review",
    "Wait: Images",
    "Parse Decision: Images",
    "IF: Images Approved",
]


def stage3():
    prepare_js = r"""
const { job_id, script, product } = $input.first().json;
const imgs = (product && Array.isArray(product.images)) ? product.images : [];
const hasProductImg = imgs.length > 0;
const items = [];
for (const s of script.scenes) {
  if (hasProductImg && s.scene === 1) continue;
  items.push({ json: { job_id: job_id, scene: s.scene, prompt: s.image_prompt, has_product_image: hasProductImg } });
}
return items;
"""
    code("Prepare Scene Images", prepare_js.strip(), x_at(0), Y3)
    http(
        "Generate Scene Image",
        {
            "method": "POST",
            "url": "https://api.openai.com/v1/images/generations",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                "={{ JSON.stringify({ model: 'dall-e-3', n: 1, size: '1024x1792',"
                " response_format: 'b64_json',"
                " prompt: $json.prompt + ' -- vertical 9:16 advertising background,"
                " bright even lighting, no text, no watermark, clean composition' }) }}"
            ),
            "options": {"timeout": 180000},
        },
        x_at(1), Y3,
        creds={"httpHeaderAuth": {"id": "REPLACE_AFTER_IMPORT", "name": "OpenAI API"}},
    )
    convert_js = r"""
const prep = $('Prepare Scene Images').all();
const inputs = $input.all();
const out = [];
for (let i = 0; i < inputs.length; i++) {
  const src = inputs[i].json || {};
  const meta = (prep[i] && prep[i].json) || {};
  const b64 = src.data && src.data[0] && src.data[0].b64_json;
  if (!b64) throw new Error('Missing b64 image for scene ' + meta.scene);
  const fileName = 'img_' + meta.job_id + '_s' + meta.scene + '.png';
  let binary;
  try {
    binary = await this.helpers.prepareBinaryData(Buffer.from(b64, 'base64'), fileName, 'image/png');
  } catch (e) {
    binary = { data: b64, mimeType: 'image/png', fileName: fileName, fileType: 'png' };
  }
  if (!binary.fileName) binary.fileName = fileName;
  if (!binary.mimeType) binary.mimeType = 'image/png';
  out.push({ json: { job_id: meta.job_id, scene: meta.scene }, binary: binary });
}
return out;
"""
    code("Convert Images to Binary", convert_js.strip(), x_at(2), Y3)
    node(
        "Write Scene Files",
        "n8n-nodes-base.readWriteFile",
        1,
        {
            "operation": "write",
            "fileName": "=/data/shared/img_{{ $json.job_id }}_s{{ $json.scene }}.png",
            "dataPropertyName": "data",
            "options": {},
        },
        x_at(3), Y3,
    )
    build_image_email_js = r"""
const job_id = $('Init Job').first().json.job_id;
const origin = $('Config').first().json.public_origin;
const { script, product } = $('Parse Script').first().json;
const imgs = (product && Array.isArray(product.images)) ? product.images : [];
const hasProductImg = imgs.length > 0;
const base = $execution.resumeUrl;
const parts = script.scenes.map((s) => {
  let src;
  if (s.scene === 1 && hasProductImg) src = imgs[0];
  else src = origin + '/media/img_' + job_id + '_s' + s.scene + '.png';
  return '<div style="margin:8px 0;font-family:sans-serif"><b>Scene ' + s.scene + '</b> (' + s.duration + 's)<br/>' +
    s.text + '<br/><img src="' + src + '" width="220"/></div>';
}).join('');
const html = '<p>Generated scene images for review:</p>' + parts +
  "<p><a href='" + base + "?action=approve&stage=images&job_id=" + job_id + "'>APPROVE</a> | " +
  "<a href='" + base + "?action=reject&stage=images&job_id=" + job_id + "'>REJECT</a></p>";
return [{ json: { job_id: job_id, html: html } }];
"""
    code("Build Image Email", build_image_email_js.strip(), x_at(4), Y3)
    gmail(
        "Email: Image Review",
        "Review scene images",
        "={{ $('Build Image Email').first().json.html }}",
        x_at(5), Y3,
    )
    wait_hook("Wait: Images", "wait-images", x_at(6), Y3)
    parse_decision("Parse Decision: Images", x_at(7), Y3)
    if_node("IF: Images Approved", x_at(8), Y3)
    chain(ROW3)


# --------------------------------------------------------------------------
# Stage 4: video review (row y=60)
# --------------------------------------------------------------------------
Y4 = 60
ROW4 = [
    "Assemble Render Payload",
    "Render Video",
    "Build Video Email",
    "Email: Video Review",
    "Wait: Video",
    "Parse Decision: Video",
    "IF: Video Approved",
]


def stage4():
    assemble_js = r"""
const cfg = $('Config').first().json;
const job_id = $('Init Job').first().json.job_id;
const { script, product } = $('Parse Script').first().json;
const imgs = (product && Array.isArray(product.images)) ? product.images : [];
const hasProductImg = imgs.length > 0;
const scenes = script.scenes.map((s) => {
  const o = { scene: s.scene, duration: s.duration, text: s.text };
  if (s.scene === 1 && hasProductImg) o.image_url = imgs[0];
  else o.image_path = 'img_' + job_id + '_s' + s.scene + '.png';
  return o;
});
return [{ json: { job_id: job_id, scenes: scenes, voice: cfg.voice, captions: true } }];
"""
    code("Assemble Render Payload", assemble_js.strip(), x_at(0), Y4)
    http(
        "Render Video",
        {
            "method": "POST",
            "url": "http://video-renderer:8000/render-video",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify($json) }}",
            "options": {"timeout": 900000},
        },
        x_at(1), Y4,
        creds={"httpHeaderAuth": {"id": "REPLACE_AFTER_IMPORT", "name": "Renderer service"}},
    )
    build_video_email_js = r"""
const v = $input.first().json;
if (v.status !== 'ok') {
  throw new Error('Render failed: ' + JSON.stringify(v).slice(0, 400));
}
const cfg = $('Config').first().json;
const job_id = v.job_id || $('Init Job').first().json.job_id;
const url = v.download_url || (cfg.public_origin + '/media/' + job_id + '.mp4');
const base = $execution.resumeUrl;
const html = '<p><b>Video ready</b> (' + (v.duration_sec || '?') + 's, ' + (v.size_bytes || '?') + ' bytes)</p>' +
  '<p><video controls width="320" src="' + url + '"></video><br/><a href="' + url + '">Download</a></p>' +
  "<p><a href='" + base + "?action=approve&stage=video&job_id=" + job_id + "'>APPROVE</a> | " +
  "<a href='" + base + "?action=reject&stage=video&job_id=" + job_id + "'>REJECT</a></p>";
return [{ json: { job_id: job_id, video_url: url, video_path: v.video_path, html: html } }];
"""
    code("Build Video Email", build_video_email_js.strip(), x_at(2), Y4)
    gmail(
        "Email: Video Review",
        "Review rendered reel",
        "={{ $('Build Video Email').first().json.html }}",
        x_at(3), Y4,
    )
    wait_hook("Wait: Video", "wait-video", x_at(4), Y4)
    parse_decision("Parse Decision: Video", x_at(5), Y4)
    if_node("IF: Video Approved", x_at(6), Y4)
    chain(ROW4)


# --------------------------------------------------------------------------
# Stage 5: publish (row y=-60) + shared stop
# --------------------------------------------------------------------------
Y5 = -60
ROW5 = [
    "Generate Promo Copy",
    "Parse Promo Copy",
    "Read Video File",
    "Upload YouTube",
    "Build Publish Plan",
    "Email: Publish Review",
    "Wait: Publish",
    "Parse Decision: Publish",
    "IF: Publish Approved",
    "Create IG Reel Container",
    "Wait: IG Processing",
    "Publish IG Container",
    "Post First Comment",
    "Set YouTube Public",
    "Final Summary",
    "Email: Published",
]


def stage5():
    promo_system = (
        "You are a social media marketing copywriter. "
        "Return ONLY valid JSON with keys: title (YouTube title, max 90 chars, English), "
        "description (2-3 sentence YouTube description), hashtags (array of 6 Instagram-style hashtags with # prefix), "
        "first_comment (one short line for the IG first comment with 2 hashtags), "
        "strategy (one sentence on the posting strategy)."
    )
    promo_body = (
        "={{ JSON.stringify({"
        " model: $('Config').first().json.openai_model,"
        " temperature: 0.7,"
        " response_format: { type: 'json_object' },"
        " messages: ["
        "  { role: 'system', content: " + json.dumps(promo_system) + " },"
        "  { role: 'user', content: 'Product: ' + JSON.stringify($('Parse Script').first().json.product)"
        " + ' Hook: ' + $('Parse Script').first().json.script.hook"
        " + ' CTA: ' + $('Parse Script').first().json.script.cta }"
        " ]"
        "}) }}"
    )
    http(
        "Generate Promo Copy",
        {
            "method": "POST",
            "url": "https://api.openai.com/v1/chat/completions",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": promo_body,
            "options": {"timeout": 120000},
        },
        x_at(0), Y5,
        creds={"httpHeaderAuth": {"id": "REPLACE_AFTER_IMPORT", "name": "OpenAI API"}},
    )
    parse_promo_js = r"""
const resp = $input.first().json;
let promo;
try {
  promo = JSON.parse(resp.choices[0].message.content);
} catch (e) {
  throw new Error('Promo JSON parse failed: ' + e.message);
}
if (!promo.title || !Array.isArray(promo.hashtags)) {
  throw new Error('Promo missing title/hashtags');
}
promo.hashtags = promo.hashtags.map((h) => {
  h = String(h).trim();
  return h.startsWith('#') ? h : '#' + h.replace(/\s+/g, '');
});
promo.description = String(promo.description || '');
promo.first_comment = String(promo.first_comment || '');
return [{ json: { promo: promo } }];
"""
    code("Parse Promo Copy", parse_promo_js.strip(), x_at(1), Y5)
    node(
        "Read Video File",
        "n8n-nodes-base.readWriteFile",
        1,
        {
            "operation": "read",
            "filePath": "={{ $('Render Video').first().json.video_path }}",
            "options": {},
        },
        x_at(2), Y5,
    )
    yt_desc = (
        "={{ $('Parse Promo Copy').first().json.promo.description"
        " + '\\n\\n' + $('Parse Promo Copy').first().json.promo.hashtags.join(' ') }}"
    )
    node(
        "Upload YouTube",
        "n8n-nodes-base.youTube",
        1,
        {
            "resource": "video",
            "operation": "upload",
            "title": "={{ $('Parse Promo Copy').first().json.promo.title.slice(0, 95) }}",
            "description": yt_desc,
            "privacyStatus": "={{ $('Config').first().json.yt_privacy }}",
            "binaryPropertyName": "data",
            "options": {},
        },
        x_at(3), Y5,
        credentials={"youtubeOAuth2Api": {"id": "REPLACE_AFTER_IMPORT", "name": "YouTube account"}},
        **{"onError": "continueRegularOutput"},
    )
    build_publish_js = r"""
const resp = $input.first().json;
const promo = $('Parse Promo Copy').first().json.promo;
const video = $('Render Video').first().json;
const yt_id = resp.id || (resp.snippet && resp.snippet.resourceId && resp.snippet.resourceId.videoId) || '';
const html = '<p><b>YouTube:</b> ' + (yt_id ? yt_id : '(see channel)') + '</p>' +
  '<p><b>IG caption:</b> ' + promo.description + ' ' + promo.hashtags.join(' ') + '</p>' +
  '<p><b>First comment:</b> ' + promo.first_comment + '</p>' +
  '<p><video controls width="320" src="' + video.download_url + '"></video></p>' +
  '<p><i>YouTube upload is UNVERIFIED via API from this VM. If the upload node failed, download the video and upload manually, then APPROVE to continue with IG.</i></p>';
const job_id = $('Init Job').first().json.job_id;
const base = $execution.resumeUrl;
const html2 = html +
  "<p><a href='" + base + "?action=approve&stage=publish&job_id=" + job_id + "'>APPROVE</a> | " +
  "<a href='" + base + "?action=reject&stage=publish&job_id=" + job_id + "'>REJECT</a></p>";
return [{ json: { job_id: job_id, youtube_id: yt_id, promo: promo, html: html2 } }];
"""
    code("Build Publish Plan", build_publish_js.strip(), x_at(4), Y5)
    gmail(
        "Email: Publish Review",
        "Approve publish to YouTube + Instagram",
        "={{ $('Build Publish Plan').first().json.html }}",
        x_at(5), Y5,
    )
    wait_hook("Wait: Publish", "wait-publish", x_at(6), Y5)
    parse_decision("Parse Decision: Publish", x_at(7), Y5)
    if_node("IF: Publish Approved", x_at(8), Y5)
    ig_caption_expr = (
        "$('Parse Promo Copy').first().json.promo.description"
        " + ' ' + $('Parse Promo Copy').first().json.promo.hashtags.join(' ')"
    )
    http(
        "Create IG Reel Container",
        {
            "method": "POST",
            "url": "=https://graph.facebook.com/v19.0/{{ $('Config').first().json.ig_user_id }}/media",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                "={{ JSON.stringify({ media_type: 'REELS',"
                " video_url: $('Build Video Email').first().json.video_url,"
                " caption: " + ig_caption_expr + " }) }}"
            ),
            "options": {"timeout": 60000},
        },
        x_at(9), Y5,
        creds={"httpHeaderAuth": {"id": "REPLACE_AFTER_IMPORT", "name": "Meta Graph API"}},
    )
    node(
        "Wait: IG Processing",
        "n8n-nodes-base.wait",
        1.1,
        {"resume": "timeInterval", "amount": 45, "unit": "seconds", "options": {}},
        x_at(10), Y5,
        webhookId="wait-ig-processing",
    )
    http(
        "Publish IG Container",
        {
            "method": "POST",
            "url": "=https://graph.facebook.com/v19.0/{{ $json.id }}/published",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({ published: true }) }}",
            "options": {"timeout": 60000},
        },
        x_at(11), Y5,
        creds={"httpHeaderAuth": {"id": "REPLACE_AFTER_IMPORT", "name": "Meta Graph API"}},
    )
    http(
        "Post First Comment",
        {
            "method": "POST",
            "url": "=https://graph.facebook.com/v19.0/{{ $json.id }}/comments",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                "={{ JSON.stringify({ message: $('Parse Promo Copy').first().json.promo.first_comment }) }}"
            ),
            "options": {"timeout": 60000},
        },
        x_at(12), Y5,
        creds={"httpHeaderAuth": {"id": "REPLACE_AFTER_IMPORT", "name": "Meta Graph API"}},
        **{"onError": "continueRegularOutput"},
    )
    node(
        "Set YouTube Public",
        "n8n-nodes-base.youTube",
        1,
        {
            "resource": "video",
            "operation": "update",
            "videoId": "={{ $('Build Publish Plan').first().json.youtube_id }}",
            "title": "={{ $('Parse Promo Copy').first().json.promo.title.slice(0, 95) }}",
            "description": yt_desc,
            "options": {"privacyStatus": "public"},
        },
        x_at(13), Y5,
        credentials={"youtubeOAuth2Api": {"id": "REPLACE_AFTER_IMPORT", "name": "YouTube account"}},
        **{"onError": "continueRegularOutput"},
    )
    final_js = r"""
const promo = $('Parse Promo Copy').first().json.promo;
const video = $('Build Video Email').first().json;
const yt = $('Build Publish Plan').first().json;
const html = '<p>Reel published.</p>' +
  '<p><b>YouTube:</b> ' + (yt.youtube_id || 'manual / unavailable') + '<br/>' +
  '<b>Video:</b> <a href="' + video.video_url + '">download</a><br/>' +
  '<b>IG caption:</b> ' + promo.description + ' ' + promo.hashtags.join(' ') + '</p>' +
  '<p><i>Note: Set YouTube public node is best-effort. If the video is still private, set it public in YouTube Studio.</i></p>';
return [{ json: { html: html } }];
"""
    code("Final Summary", final_js.strip(), x_at(14), Y5)
    gmail(
        "Email: Published",
        "Reel published",
        "={{ $json.html }}",
        x_at(15), Y5,
    )
    node(
        "Stop: Rejected",
        "n8n-nodes-base.stopAndError",
        1,
        {"errorMessage": "Pipeline rejected by reviewer at stage: {{ $json.stage }}"},
        -720, 620,
    )
    chain(ROW5)


def wire():
    stage1()
    stage2()
    stage3()
    stage4()
    stage5()
    link("IF: Product Approved", "Write Reel Script", out=1)
    link("IF: Script Approved", "Prepare Scene Images", out=1)
    link("IF: Images Approved", "Assemble Render Payload", out=1)
    link("IF: Video Approved", "Generate Promo Copy", out=1)
    link("IF: Publish Approved", "Create IG Reel Container", out=1)
    for ifn in [
        "IF: Product Approved",
        "IF: Script Approved",
        "IF: Images Approved",
        "IF: Video Approved",
        "IF: Publish Approved",
    ]:
        link(ifn, "Stop: Rejected", out=0)


def main():
    wire()
    wf = {
        "name": "Meesho Reels Pipeline",
        "nodes": NODES,
        "connections": CONN,
        "settings": {"executionOrder": "v1"},
        "pinData": {},
        "active": False,
    }
    names = {n["name"] for n in NODES}
    for src, c in CONN.items():
        assert src in names, "dangling connection source: " + src
        for outs in c.get("main", []):
            for tgt in outs:
                assert tgt["node"] in names, "dangling target: " + tgt["node"]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(wf, f, indent=2, ensure_ascii=False)
    print("wrote", OUT, "nodes:", len(NODES), "sources:", len(CONN))


if __name__ == "__main__":
    main()
