// staff_portal/static/noreload.js - NR-1 (2026-10-20): every form in the portal works without reloading the page.
//
// One mechanism for every page, no page-specific code:
//   · A submitted form (POST or GET) is sent in the background with the same fields and the same CSRF token.
//   · The server answers exactly as before (redirect + flash cookie, or a page). The answer's content area, menu,
//     top bar and title replace the current ones; the flash becomes the usual toast; the page's own scripts run again.
//   · Same page -> the scroll position is kept. Another page -> the address bar follows (Back works) and it scrolls up.
//   · A file in the answer (export/download) is saved; a sign-in page or an answer that is not a portal page opens
//     normally; any failure falls back to the page loading the old way (nothing is sent twice).
//   · Opt-out: <form data-reload> (one form), <main data-reload> (one page), PORTAL_NORELOAD=0 (whole portal).
(function () {
  "use strict";
  if (window.NourNoReload) return;
  var busy = false;

  function eligible(f, sub) {
    if (!f || f.tagName !== "FORM" || f.hasAttribute("data-reload")) return false;
    var main = document.querySelector("main.content");
    if (!main || main.hasAttribute("data-reload") || !main.contains(f) && !f.closest(".n-main")) return false;
    if ((sub && sub.hasAttribute("formtarget")) || (f.target && f.target !== "_self")) return false;
    if (f.hasAttribute("hx-post") || f.hasAttribute("hx-get") || (sub && sub.hasAttribute("data-reload"))) return false;
    var m = ((sub && sub.getAttribute("formmethod")) || f.getAttribute("method") || "get").toLowerCase();
    if (m !== "post" && m !== "get") return false;
    var a = new URL((sub && sub.getAttribute("formaction")) || f.getAttribute("action") || location.href, location.href);
    return a.origin === location.origin;
  }

  function runScripts(root) {
    // Scripts that wait for DOMContentLoaded are called at once (the document is already loaded).
    var add = document.addEventListener;
    document.addEventListener = function (type, fn, opt) { if (type === "DOMContentLoaded") { try { fn.call(document, new Event("DOMContentLoaded")); } catch (e) { console.error(e); } return; } return add.call(document, type, fn, opt); };
    try {
      root.querySelectorAll("script").forEach(function (old) {
        var s = document.createElement("script");
        for (var i = 0; i < old.attributes.length; i++) s.setAttribute(old.attributes[i].name, old.attributes[i].value);
        if (!old.src) s.textContent = old.textContent; else s.async = false;
        old.replaceWith(s);
      });
    } finally { document.addEventListener = add; }
  }

  function swap(doc, url, samePage) {
    var m1 = document.querySelector("main.content"), m2 = doc.querySelector("main.content");
    var y = window.scrollY;
    [".n-side", ".n-crumb"].forEach(function (sel) { var a = document.querySelector(sel), b = doc.querySelector(sel); if (a && b) a.innerHTML = b.innerHTML; });
    m1.innerHTML = m2.innerHTML;
    if (m2.hasAttribute("data-reload")) m1.setAttribute("data-reload", ""); else m1.removeAttribute("data-reload");
    document.title = doc.title;
    var p1 = document.getElementById("n-page-scripts"), p2 = doc.getElementById("n-page-scripts");
    if (p1 && p2) p1.innerHTML = p2.innerHTML;
    if (!samePage) history.pushState({ nr: 1 }, "", url);
    else if (url !== location.href) history.replaceState({ nr: 1 }, "", url);
    window.scrollTo(0, samePage ? y : 0);
    var fl = doc.getElementById("n-flash");                             // the server's message -> the usual toast
    if (fl && fl.dataset.msg && window.Nour) {
      window.Nour.toast({ kind: { success: "ok", error: "danger", info: "info" }[fl.dataset.kind] || "info", text: fl.dataset.msg });
    }
    document.cookie = "flash=; max-age=0; path=/";
    try { if (window.NourTbl) window.NourTbl.scan(); } catch (e) { }
    try { if (window.htmx) window.htmx.process(m1); } catch (e) { }
    try {                                                               // live.js: the page as the server sent it, from now on
      var L = window.NourLive && window.NourLive.state;
      if (L) { L.base = {}; L.pending = {}; L.rest = ""; L.leaving = false; window.NourLive.regions(m1).forEach(function (r) { L.base[r.id] = r.cloneNode(true); }); if (L.pageChip) { L.pageChip.remove(); L.pageChip = null; } }
    } catch (e) { }
    runScripts(m1);
    if (p1) runScripts(p1);
    document.dispatchEvent(new CustomEvent("nour:swapped", { detail: { url: url } }));
  }

  function filename(r) {
    var cd = r.headers.get("content-disposition") || "", m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
    return m ? decodeURIComponent(m[1]) : "download";
  }

  window.addEventListener("submit", function (e) {
    var f = e.target, sub = e.submitter;
    if (e.defaultPrevented || !eligible(f, sub)) return;                // a confirm dialog (nour.js) cancels first and submits again when agreed
    e.preventDefault();
    if (busy) return;                                                   // a double click sends once
    busy = true;
    var method = ((sub && sub.getAttribute("formmethod")) || f.getAttribute("method") || "get").toUpperCase();
    var action = new URL((sub && sub.getAttribute("formaction")) || f.getAttribute("action") || location.href, location.href);
    var data = new FormData(f, sub || undefined), init = { method: method, credentials: "same-origin", headers: { "X-NoReload": "1" } };
    if (method === "GET") { action.search = new URLSearchParams(data).toString(); } else { init.body = data; }
    var btns = f.querySelectorAll("button, input[type=submit]");
    btns.forEach(function (b) { b.dataset.nrWas = b.disabled ? "1" : ""; b.disabled = true; });
    f.setAttribute("aria-busy", "true");
    var here = location.pathname + location.search;
    function done() { busy = false; f.removeAttribute("aria-busy"); btns.forEach(function (b) { if (b.isConnected) b.disabled = b.dataset.nrWas === "1"; }); }
    fetch(action.toString(), init).then(function (r) {
      var to = new URL(r.url || action.toString());
      if (/^\/login\b/.test(to.pathname)) { location.href = to.toString(); return; }
      var ct = r.headers.get("content-type") || "";
      var isFile = /attachment/i.test(r.headers.get("content-disposition") || "") ||
                   (r.ok && /^(application\/(pdf|zip|octet-stream|vnd\.|msword|x-zip)|text\/csv|image\/|audio\/|video\/)/i.test(ct));
      if (!isFile && ct && !/text\/html/i.test(ct)) {                   // not a page and not a file (an error text, JSON): show it as the browser would
        return r.text().then(function (txt) { document.open(); document.write("<pre style=\"white-space:pre-wrap;font:14px monospace;padding:16px\"></pre>"); document.close(); document.querySelector("pre").textContent = txt; });
      }
      if (isFile) {
        return r.blob().then(function (b) {                             // a file: save it, the page stays
          var a = document.createElement("a"), u = URL.createObjectURL(b);
          a.href = u; a.download = filename(r); document.body.appendChild(a); a.click(); a.remove(); setTimeout(function () { URL.revokeObjectURL(u); }, 4000);
          done();
        });
      }
      return r.text().then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        if (!doc.querySelector("main.content")) { document.open(); document.write(html); document.close(); return; }   // not a portal page (an error): show it as it is
        swap(doc, to.toString(), to.pathname + to.search === here || (method === "POST" && to.pathname === location.pathname));
        done();
      });
    }).catch(function () {
      done();
      if (window.Nour) window.Nour.toast({ kind: "danger", text: document.documentElement.lang === "ar" ? "تعذّر الإرسال. تحقق من الاتصال وحاول مرة أخرى." : "Could not send. Check the connection and try again." });
    });
  });

  window.addEventListener("popstate", function (e) { if (e.state && e.state.nr) location.reload(); });   // Back/Forward over a swapped page: load it properly
  window.NourNoReload = { on: true };
})();
