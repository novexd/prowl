/*
 * redirect.js - OAuth redirect handling with a persistent loading overlay.
 *
 * Problem this fixes: on Vercel, after you sit in the Discord/GitHub
 * consent window and get redirected back, the server can cold-start for ~5s
 * with no loading animation visible. Opening the OAuth flow in a POPUP keeps the
 * current page alive (showing the loader) through the whole round-trip, and once
 * the popup lands back on our own domain we move the main window there. The popup
 * also warms the destination so the final hop is fast.
 */

(function () {
  "use strict";

  function ensureStyles() {
    if (document.getElementById("prowl-redirect-kf")) return;
    var s = document.createElement("style");
    s.id = "prowl-redirect-kf";
    s.textContent =
      "@keyframes prowl-rl-spin{to{transform:rotate(360deg)}}" +
      "@keyframes prowl-rl-pulse{0%,100%{opacity:.55;transform:scale(.96)}50%{opacity:1;transform:scale(1)}}";
    document.head.appendChild(s);
  }

  function showLoader() {
    ensureStyles();
    var existing = document.getElementById("prowl-redirect-loader");
    if (existing) return;
    var el = document.createElement("div");
    el.id = "prowl-redirect-loader";
    el.setAttribute("aria-hidden", "true");
    el.innerHTML =
      '<div class="prowl-rl-inner">' +
      '<div class="prowl-rl-ringwrap">' +
      '<svg class="prowl-rl-ring" viewBox="0 0 68 68"><circle cx="34" cy="34" r="30"/></svg>' +
      '<img src="/static/favicon.png" alt="Prowl" />' +
      "</div></div>";
    el.style.cssText =
      "position:fixed;inset:0;z-index:100000;background:#0a0a0a;display:flex;" +
      "align-items:center;justify-content:center;opacity:0;transition:opacity .2s ease;";
    var wrap = el.querySelector(".prowl-rl-ringwrap");
    wrap.style.cssText = "position:relative;width:68px;height:68px;";
    var img = el.querySelector("img");
    img.style.cssText =
      "position:absolute;inset:0;margin:auto;width:38px;height:38px;border-radius:10px;" +
      "animation:prowl-rl-pulse 1.1s ease-in-out infinite;";
    var ring = el.querySelector(".prowl-rl-ring");
    ring.style.cssText = "width:68px;height:68px;animation:prowl-rl-spin 1s linear infinite;";
    var circle = el.querySelector("circle");
    circle.style.cssText =
      "fill:none;stroke:rgba(255,255,255,.85);stroke-width:3.5;stroke-linecap:round;stroke-dasharray:140 60;";
    document.body.appendChild(el);
    requestAnimationFrame(function () {
      el.style.opacity = "1";
    });
  }

  function hideLoader() {
    var el = document.getElementById("prowl-redirect-loader");
    if (el) el.remove();
  }

  function isOurHost(loc) {
    try {
      return loc && loc.host === window.location.host;
    } catch (e) {
      return false; // cross-origin -> external provider
    }
  }

  function prowlOAuth(url, opts) {
    opts = opts || {};
    showLoader();
    var popup;
    try {
      popup = window.open(url, "prowl_oauth", "width=620,height=820");
    } catch (e) {
      popup = null;
    }
    if (!popup || popup.closed) {
      // Popup blocked - fall back to the old top-level navigation.
      window.location.href = url;
      return;
    }
    var done = { v: false };
    var finished = function (path, search) {
      if (done.v) return;
      done.v = true;
      clearInterval(poll);
      clearInterval(closeChk);
      try { popup.close(); } catch (e) {}
      window.location.href = (path || "/servers") + (search || "");
    };
    var poll = setInterval(function () {
      var loc;
      try { loc = popup.location; } catch (e) { return; } // still on external provider
      if (!isOurHost(loc)) return;
      var p = loc.pathname;
      // Success / return paths from our own callbacks.
      if (
        p === "/servers" ||
        p.indexOf("/dashboard") === 0 ||
        p === "/callback" ||
        p.indexOf("/callback/") === 0
      ) {
        finished(p, loc.search);
      } else if (p === "/login") {
        // Failure (or link flow that bounced) - go back to login.
        finished("/login", "");
      }
      // Note: the OAuth *start* paths (/auth/discord, /login/github) are
      // also on our host but are not matched here, so we correctly keep
      // waiting until the provider redirects us back.
    }, 400);
    var closeChk = setInterval(function () {
      if (popup.closed && !done.v) {
        done.v = true;
        clearInterval(poll);
        clearInterval(closeChk);
        hideLoader();
      }
    }, 800);
  }

  window.prowlOAuth = prowlOAuth;
  window.prowlShowLoader = showLoader;
  window.prowlHideLoader = hideLoader;

  // Intercept clicks on the OAuth start links anywhere on the page (covers both
  // the static login buttons and dynamically-injected links) without needing
  // per-link wiring.
  document.addEventListener("click", function (e) {
    var a = e.target.closest ? e.target.closest("a") : null;
    if (!a) return;
    if (a.target === "_blank") return; // real new-tab links (bot invites)
    var href = a.getAttribute("href") || "";
    if (href.indexOf("/auth/discord") === 0) {
      e.preventDefault();
      prowlOAuth(href);
    } else if (href.indexOf("/login/github") === 0) {
      e.preventDefault();
      prowlOAuth(href);
    }
  });
})();
