// vgcore.gif.test.js: file naming for GIFs and still images (micro-pill / Smart One-Click).
// Run:  node extension/tests/vgcore.gif.test.js        (exit code 1 on failure)
"use strict";
const assert = require("node:assert");
const path = require("node:path");
const C = require(path.join(__dirname, "..", "vgcore.js"));

const cases = [
  // GIF: the extension comes from the query, the content type, or the path.
  ["twitter ?format=gif, extensionless name",
    { filename: "Fx1AbCdEfGhIjKl", url: "https://pbs.twimg.com/media/Fx1AbCdEfGhIjKl?format=gif&name=small" }, "Fx1AbCdEfGhIjKl.gif"],
  ["twitter ?format=gif, no name at all",
    { url: "https://pbs.twimg.com/media/Fx1AbCdEfGhIjKl?format=gif&name=small" }, "pbs.twimg.com.gif"],
  ["format wins over a .jpg that came from the URL path",
    { filename: "cat.jpg", url: "https://x.test/cat.jpg?format=gif" }, "cat.gif"],
  ["plain .gif URL",
    { filename: "cat.gif", url: "https://x.test/cat.gif" }, "cat.gif"],
  ["upper-case query value",
    { filename: "cat", url: "https://x.test/cat?format=GIF" }, "cat.gif"],
  ["image/gif content type, nothing else to go on",
    { filename: "cat", url: "https://x.test/cat", contentType: "image/gif" }, "cat.gif"],
  ["image/gif content type with parameters",
    { filename: "cat", url: "https://x.test/cat", contentType: "image/gif; charset=binary" }, "cat.gif"],
  // Still images keep working.
  ["twitter ?format=jpg",
    { filename: "cat", url: "https://pbs.twimg.com/media/cat?format=jpg&name=large" }, "cat.jpg"],
  ["?format=png",
    { filename: "cat", url: "https://x.test/cat?format=png" }, "cat.png"],
  ["?fm=webp (imgix style)",
    { filename: "cat", url: "https://x.test/cat?fm=webp&w=400" }, "cat.webp"],
  ["plain .png URL",
    { filename: "cat.png", url: "https://x.test/cat.png" }, "cat.png"],
  ["plain .jpeg URL keeps jpeg",
    { filename: "cat.jpeg", url: "https://x.test/cat.jpeg" }, "cat.jpeg"],
  ["image/jpeg content type",
    { filename: "cat", url: "https://x.test/cat", contentType: "image/jpeg" }, "cat.jpg"],
  ["image/png content type",
    { filename: "cat", url: "https://x.test/cat", contentType: "image/png" }, "cat.png"],
  ["image/webp content type",
    { filename: "cat", url: "https://x.test/cat", contentType: "image/webp" }, "cat.webp"],
  ["image/svg+xml content type",
    { filename: "logo", url: "https://x.test/logo", contentType: "image/svg+xml" }, "logo.svg"],
  // Things the query must NOT change.
  ["?format=json is not an extension",
    { filename: "cat", url: "https://x.test/cat?format=json" }, "cat"],
  ["a name the user typed keeps its extension",
    { filename: "my pic.jpg", url: "https://x.test/other?format=gif" }, "my pic.jpg"],
  ["a playlist is never given a query extension",
    { filename: "index.m3u8", url: "https://x.test/index.m3u8?format=mp4" }, "index"],
];
let failed = 0;
for (const [name, ctx, want] of cases) {
  const got = C.improveFilename(ctx);
  if (got === want) console.log("ok   " + name);
  else { failed++; console.log(`FAIL ${name}\n       want ${JSON.stringify(want)}, got ${JSON.stringify(got)}`); }
}
const extra = [
  ["extFromQuery reads format", C.extFromQuery("https://pbs.twimg.com/media/a?format=gif&name=small"), "gif"],
  ["extFromQuery ignores non-media values", C.extFromQuery("https://x.test/a?format=json"), ""],
  ["extFromQuery ignores non-URLs", C.extFromQuery("cat.gif"), ""],
  ["kindOf sees ?format=gif as image", C.kindOf({ url: "https://pbs.twimg.com/media/a?format=gif" }), "image"],
  ["inspectItem.ext falls back to the query", C.inspectItem({ url: "https://pbs.twimg.com/media/a?format=gif" }).ext, "gif"],
  ["kindOf: .gif path still image", C.kindOf({ url: "https://x.test/a.gif" }), "image"],
];
for (const [name, got, want] of extra) {
  if (got === want) console.log("ok   " + name);
  else { failed++; console.log(`FAIL ${name}\n       want ${JSON.stringify(want)}, got ${JSON.stringify(got)}`); }
}
console.log(failed ? `\n${failed} failed` : `\nall ${cases.length + extra.length} passed`);
process.exitCode = failed ? 1 : 0;
