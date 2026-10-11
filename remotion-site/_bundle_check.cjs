// Credential-free smoke: bundle the fps-15-patched site exactly as
// `remotion lambda sites create` will, before any AWS access exists.
const path = require("path");
const { bundle } = require("@remotion/bundler");

bundle({ entryPoint: path.resolve(__dirname, "src", "index.tsx"), onProgress: () => {} })
  .then((dir) => {
    console.log("BUNDLE_OK", dir);
    process.exit(0);
  })
  .catch((err) => {
    console.error("BUNDLE_FAIL", err && err.message ? err.message : err);
    process.exit(1);
  });
