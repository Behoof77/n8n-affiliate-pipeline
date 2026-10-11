/**
 * Dispatch Lambda behind API Gateway HTTP API POST /render.
 *
 * Flow: X-Api-Key auth -> invoke prep Lambda (TTS/images/narration -> inputProps)
 *   -> renderMediaOnLambda -> poll getRenderProgress (1500ms, budget < 900s)
 *   -> presign output mp4 (1h GET) -> v1-shaped response + engine field.
 *
 * HTTP APIs have no API-key support, so the custom header check lives here.
 * Env: REMOTION_FUNCTION, SERVE_URL, PREP_FUNCTION, API_KEY,
 *      COMPOSITION (default "Explainer"), AWS_REGION (runtime-provided).
 */

import { renderMediaOnLambda, getRenderProgress } from "@remotion/lambda/client";
import { LambdaClient, InvokeCommand } from "@aws-sdk/client-lambda";
import { S3Client } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";

const REGION = process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || "ap-south-1";
const REMOTION_FUNCTION = process.env.REMOTION_FUNCTION || "";
const SERVE_URL = process.env.SERVE_URL || "";
const PREP_FUNCTION = process.env.PREP_FUNCTION || "";
const API_KEY = process.env.API_KEY || "";
const COMPOSITION = process.env.COMPOSITION || "Explainer";
const POLL_MS = Number(process.env.POLL_MS || 1500);
const BUDGET_MS = Number(process.env.BUDGET_MS || 840_000); // < 900s Lambda cap
const SIGN_TTL = Number(process.env.SIGN_TTL || 3600);

const lambda = new LambdaClient({ region: REGION });
const s3 = new S3Client({ region: REGION });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const json = (statusCode, body) => ({
  statusCode,
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
});

/** Map a prep-Lambda error message ("400: ...") onto an HTTP status. */
function prepStatus(errorMessage) {
  const m = /^(\d{3}):\s*/.exec(errorMessage || "");
  if (m) {
    return { code: Number(m[1]), detail: errorMessage.slice(m[0].length) };
  }
  return { code: 500, detail: errorMessage || "prep failed" };
}

function outputKeyOf(progress) {
  if (progress.outKey) return progress.outKey;
  const file = progress.outputFile || "";
  if (file.startsWith("s3://")) {
    const rest = file.slice("s3://".length);
    return rest.slice(rest.indexOf("/") + 1);
  }
  return null;
}

export const handler = async (event) => {
  const started = Date.now();

  const key = event?.headers?.["x-api-key"];
  if (!API_KEY) return json(500, { detail: "API_KEY not configured" });
  if (key !== API_KEY) return json(401, { detail: "unauthorized" });

  if (!REMOTION_FUNCTION || !SERVE_URL || !PREP_FUNCTION) {
    return json(500, { detail: "REMOTION_FUNCTION/SERVE_URL/PREP_FUNCTION not configured" });
  }

  let raw;
  try {
    const body = event.isBase64Encoded
      ? Buffer.from(event.body || "", "base64").toString("utf8")
      : event.body || "";
    raw = JSON.parse(body);
  } catch (err) {
    return json(400, { detail: `invalid JSON body: ${err.message}` });
  }

  // 1) prep: TTS + image staging + inputProps assembly
  let prep;
  try {
    const res = await lambda.send(
      new InvokeCommand({
        FunctionName: PREP_FUNCTION,
        InvocationType: "RequestResponse",
        Payload: Buffer.from(JSON.stringify(raw), "utf8"),
      }),
    );
    const payload = JSON.parse(Buffer.from(res.Payload || new Uint8Array()).toString("utf8"));
    if (res.FunctionError) {
      const { code, detail } = prepStatus(payload.errorMessage);
      console.error(`prep failed status=${code} detail=${detail}`);
      return json(code, { detail });
    }
    prep = payload;
  } catch (err) {
    console.error(`prep invoke error: ${err.message}`);
    return json(500, { detail: `prep invoke failed: ${err.message}` });
  }

  const { job_id: jobId, inputProps, duration_sec: durationSec } = prep;

  // 2) kick off the distributed Remotion render
  let renderId;
  let bucketName;
  try {
    const startedRender = await renderMediaOnLambda({
      region: REGION,
      functionName: REMOTION_FUNCTION,
      serveUrl: SERVE_URL,
      composition: COMPOSITION,
      inputProps,
      codec: "h264",
      imageFormat: "jpeg",
      maxRetries: 1,
      privacy: "private",
    });
    renderId = startedRender.renderId;
    bucketName = startedRender.bucketName;
  } catch (err) {
    console.error(`renderMediaOnLambda error: ${err.message}`);
    return json(500, { detail: `render start failed: ${err.message}` });
  }
  console.log(`render started job=${jobId} renderId=${renderId} bucket=${bucketName}`);

  // 3) poll progress inside the render budget
  const deadline = started + BUDGET_MS;
  let progress = null;
  for (;;) {
    if (Date.now() >= deadline) break;
    try {
      progress = await getRenderProgress({
        renderId,
        bucketName,
        functionName: REMOTION_FUNCTION,
        region: REGION,
      });
    } catch (err) {
      console.error(`getRenderProgress error: ${err.message}`);
      await sleep(POLL_MS);
      continue;
    }
    if (progress.fatalErrorEncountered) {
      const detail = (progress.errors || []).map((e) => e.message || String(e)).join("; ");
      console.error(`render fatal job=${jobId}: ${detail}`);
      return json(500, { detail: `render failed: ${detail || "fatal error"}` });
    }
    if (progress.done) {
      if (progress.errors && progress.errors.length > 0) {
        const detail = progress.errors.map((e) => e.message || String(e)).join("; ");
        return json(500, { detail: `render failed: ${detail}` });
      }
      break;
    }
    await sleep(POLL_MS);
  }

  if (!progress || !progress.done) {
    console.error(`render budget exceeded job=${jobId} elapsed=${Date.now() - started}ms`);
    return json(504, { detail: "render budget exceeded" });
  }

  const outKey = outputKeyOf(progress);
  if (!outKey) {
    return json(500, { detail: "render finished without an output key" });
  }

  // 4) presigned download URL + v1-shaped response
  const downloadUrl = await getSignedUrl(
    s3,
    { Bucket: bucketName, Key: outKey },
    { expiresIn: SIGN_TTL },
  );
  console.log(
    `render done job=${jobId} elapsed=${Date.now() - started}ms size=${progress.outputSizeInBytes}`,
  );
  return json(200, {
    status: "ok",
    job_id: jobId,
    video_path: `/data/shared/${jobId}.mp4`,
    download_url: downloadUrl,
    duration_sec: durationSec,
    size_bytes: progress.outputSizeInBytes ?? null,
    engine: "remotion-lambda",
  });
};
