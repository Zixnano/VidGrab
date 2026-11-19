// Micro pill logic (full-size source, menu rows, post batch) tested without a browser:
// the functions are sliced out of content.js between marker comments and run against a
// tiny fake DOM. If those markers are renamed, update the cut() calls below.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const src = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const cut = (a, b) => src.slice(src.indexOf(a), src.indexOf(b));
const code = [
  cut("  function microAbs(u, base)", "  function microEligible"),
  cut("  function microMenuRows(el)", "  function openMicroMenu"),
  cut('  // "Download ALL Images": every <img>', "  // mouseenter does not bubble"),
].join("\n");
const toasts = [];
const mk = new Function("document", "cfg", "vgSend", "microToastShow", "pageRefFor", "microFilenameFor", "sendMicro", "copyToClipboard", "window", "chrome", "MICRO_IMG_EXT_RE",
  code + "\n;return {microUrlFor, microMenuRows, collectImageUrls, microPostRoot, downloadAllPageImages, microSizeChoices};");

function el(tag, o) {
  const e = Object.assign({
    tagName: tag.toUpperCase(), children: [], parentElement: null, baseURI: "https://pawchive.pw/post/1", attrs: {},
    naturalWidth: 400, naturalHeight: 300, width: 400, height: 300,
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    getBoundingClientRect() { return { width: 400, height: 300 }; },
    closest() { for (let n = this; n; n = n.parentElement) if (n.tagName === "A" && n.attrs.href != null) return n; return null; },
    matches(sel) {
      return sel.split(",").some((s) => {
        s = s.trim();
        if (s.startsWith("[class*='")) return !!this.cls && this.cls.includes(s.slice(9, -2));
        return s.toLowerCase() === this.tagName.toLowerCase();
      });
    },
    querySelectorAll() { const out = []; (function walk(n) { for (const c of n.children) { if (c.tagName === "IMG") out.push(c); walk(c); } })(this); return out; },
  }, o || {});
  return e;
}
const add = (p, c) => { c.parentElement = p; p.children.push(c); return c; };
const body = el("body"), docEl = el("html"); body.parentElement = docEl;
const post = add(body, el("div", { cls: "post-card" })), other = add(body, el("div", { cls: "post-card" }));
const mkImg = (parent, href, src, extra) => { const a = add(parent, el("a", { attrs: { href } })); return add(a, el("img", Object.assign({ src, currentSrc: src }, extra || {}))); };
const g1 = mkImg(post, "https://n2.pawchive.pw/data/a/h1.gif?f=a.gif", "https://img.pawchive.pw/thumbnail/data/a/h1.jpg");
const g2 = mkImg(post, "https://n2.pawchive.pw/data/b/h2.png", "https://img.pawchive.pw/thumbnail/data/b/h2.jpg");
const o1 = mkImg(other, "/user/1/post/9", "https://img.pawchive.pw/thumbnail/data/c/h3.jpg");
mkImg(other, "https://x.test/i.png", "https://x.test/i.png", { naturalWidth: 20, naturalHeight: 20, width: 20, height: 20 });
const doc = { baseURI: "https://pawchive.pw/post/1", querySelectorAll: (s) => body.querySelectorAll(s), body, documentElement: docEl };
const sent = [];
const api = mk(doc, { smartOneClick: true },
  (m, cb) => { sent.push(m); setTimeout(() => cb({ ok: true, results: m.items.map((i) => ({ ok: true, url: i.url })) }), 0); },
  (t) => toasts.push(t), () => "https://pawchive.pw/post/1", () => undefined, () => {}, () => {}, { open() {} }, { runtime: { lastError: null } },
  /\.(jpe?g|png|gif|webp|avif|bmp|svg|tiff?|heic|heif|jxl|ico)$/i);

const eq = (n, got, want) => assert.deepStrictEqual(got, want, n);

test("micro pill: sources, menu rows and the post batch", async () => {
  eq("gif thumbnail resolves to the linked full file", api.microUrlFor(g1), "https://n2.pawchive.pw/data/a/h1.gif?f=a.gif");
  eq("link to a post page falls back to the displayed image", api.microUrlFor(o1), "https://img.pawchive.pw/thumbnail/data/c/h3.jpg");
  const lazy = el("img", { src: "https://x.test/t.jpg", currentSrc: "https://x.test/t.jpg", attrs: { "data-full-src": "/big/t.jpg" } });
  eq("data-full-src is used", api.microUrlFor(lazy), "https://pawchive.pw/big/t.jpg");
  const sz = el("img", { src: "https://x.test/a-640.jpg", currentSrc: "https://x.test/a-640.jpg", attrs: { srcset: "https://x.test/a-640.jpg 640w, https://x.test/a-1280.jpg 1280w, https://x.test/a-2x.jpg 2x" } });
  eq("size choices: widths only, largest first", api.microSizeChoices(sz).map((x) => x.w), [1280, 640]);
  eq("menu for a gif thumbnail", api.microMenuRows(g1).map((r) => r.header || r.label || "---").filter((x) => !/^Download images in this post/.test(x)),
    ["GIF: full-size file found", "Download Image", "Download as shown (preview)", "Download ALL Images", "---", "Open original in new tab", "Copy Image URL"]);
  eq("batch: tiny images skipped", api.collectImageUrls(doc).length, 3);
  const pr = api.microPostRoot(g1);
  eq("post root found for the first post (2 of 3 images)", pr && pr.count, 2);
  assert.ok(api.microMenuRows(g1).some((r) => r.label === "Download images in this post (2)"));
  eq("no post root when the container has a single image", api.microPostRoot(o1), null);
  api.downloadAllPageImages(pr.root);
  await new Promise((r) => setTimeout(r, 40));
  eq("SEND_MANY carries the full-size urls", sent[0].type + ":" + sent[0].items.map((i) => i.url.split("/").pop()).join(","), "SEND_MANY:h1.gif?f=a.gif,h2.png");
  eq("summary toast", toasts[toasts.length - 1], "Queued 2 image(s)");
});
