// popupshim.js: loaded before popup.js. Hardens the old popup code without
// rewriting it, and marks side-panel mode.
//  - chrome.runtime.sendMessage callbacks: always read lastError (no "Unchecked
//    runtime.lastError" noise), retry once when the worker was asleep, answer
//    undefined (which popup.js already treats as "no data") on failure.
//  - Side panel: body.side; window.close() becomes a no-op because a panel is
//    not a popup and must stay open.
(function () {
  "use strict";
  const rt = chrome.runtime;
  const orig = rt.sendMessage.bind(rt);
  const TRANSIENT = /receiving end does not exist|message port closed|could not establish connection/i;
  rt.sendMessage = function (msg, cb) {
    if (typeof cb !== "function") return orig(msg).catch(() => undefined);
    let tries = 0;
    const go = () => {
      let done = false;
      const timer = setTimeout(() => { if (!done) { done = true; cb(undefined); } }, 30000);
      try {
        orig(msg, (resp) => {
          const le = chrome.runtime.lastError;
          if (done) return;
          if (le && tries++ < 1 && TRANSIENT.test(le.message || "")) { clearTimeout(timer); done = true; setTimeout(go, 300); return; }
          done = true; clearTimeout(timer);
          cb(le ? undefined : resp);
        });
      } catch (e) { if (!done) { done = true; clearTimeout(timer); cb(undefined); } }
    };
    go();
  };
  const isSide = /sidepanel\.html$/.test(location.pathname);
  document.documentElement.classList.toggle("side", isSide);
  if (isSide) window.close = function () {};
})();
