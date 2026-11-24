// Dumps frame choices from the extension's animator so Python can compare them with the desktop one.
const fs = require("fs");
const path = require("path");
const { Animator } = require(path.join(__dirname, "..", "extension", "companion-anim.js"));
const reg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const poses = ["idle", "content", "happy", "curious", "sleeping", "sad", "excited", "celebrating", "wobble", "cracked"];
const times = [];
for (let i = 0; i < 120; i++) times.push(i * 0.1 + (i % 7) * 0.013);
times.push(4.65, 4.7, 9.4, 14.1);
const out = {};
for (const entry of [reg.egg].concat(reg.species)) {
  const a = new Animator(entry, reg.animation);
  const id = entry.id || "egg";
  out[id] = {};
  for (const pose of poses) out[id][pose] = times.map((t) => { const f = a.frame(pose, t); return [f.index, f.dy]; });
}
process.stdout.write(JSON.stringify({ times, out }));
