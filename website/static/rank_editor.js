/* Rank Card Editor — controls shell (no canvas editor yet).
 * Owns: header buttons, background block, color blocks, element toggles.
 * State shape matches the saved rank_card payload so the future canvas
 * step can plug straight in. Canvas placeholder (#re-canvas) is static.
 */
(() => {
  const DEFAULTS = {
    background: "random",
    primary_color: [255, 255, 255],
    accent_color: [255, 255, 255],
    elements: {
      panel: { enabled: true },
      avatar: { enabled: true, x: null, y: null, size: null },
      name: { enabled: true, x: null, y: null },
      xp_value: { enabled: true, x: null, y: null },
      rank: { enabled: true, x: null, y: null },
      xp_bar: { enabled: true, x: null, y: null, width: null, height: 14 },
      xp_ratio: { enabled: true },
    },
  };

  const PRESETS = ["#5865f2", "#57f287", "#fee75c", "#faa61a", "#ed4245", "#eb459e", "#00c8ff", "#b6a2e0", "#ffffff", "#313338", "#000000"];

  const state = {
    config: JSON.parse(JSON.stringify(DEFAULTS)),
    loaded: {},
  };

  let bgList = [];
  let renderTimer = null, renderSeq = 0, previewURL = null;

  const $ = (id) => document.getElementById(id);
  const api = (path, opts) => {
    opts = Object.assign({ credentials: "include" }, opts);
    return fetch(`/api/v1/user${path}`, opts).then(r => r.ok ? r.json() : r.text().then(t => { throw new Error(t); }));
  };

  function hexToRgb(h) {
    let s = String(h || "").replace(/^#/, "");
    if (s.length === 3) s = s.split("").map(c => c + c).join("");
    const n = parseInt(s, 16);
    if (!s || isNaN(n)) return [255, 255, 255];
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function rgbToHex(c) {
    const to = (n) => Math.round(Math.min(255, Math.max(0, n || 0))).toString(16).padStart(2, "0");
    return "#" + to(c[0]) + to(c[1]) + to(c[2]);
  }

  function setVal(id, v) {
    const el = $(id);
    if (el) el.value = v;
  }

  function on(id, ev, fn) {
    const el = $(id);
    if (el) el.addEventListener(ev, fn);
  }

  function updateColorInputs() {
    setVal("re-color-primary", rgbToHex(state.config.primary_color));
    setVal("re-primary-hex", rgbToHex(state.config.primary_color));
    setVal("re-color-accent", rgbToHex(state.config.accent_color));
    setVal("re-accent-hex", rgbToHex(state.config.accent_color));
  }

  function syncColorPair(colorId, hexId, apply) {
    const col = $(colorId), hex = $(hexId);
    if (!col || !hex) return;
    col.addEventListener("input", () => { hex.value = col.value; apply(hexToRgb(col.value)); });
    hex.addEventListener("input", () => {
      if (/^#[0-9a-fA-F]{6}$/.test(hex.value)) { col.value = hex.value; apply(hexToRgb(hex.value)); }
    });
  }

  function isDark(hex) {
    const [r, g, b] = hexToRgb(hex);
    return (0.299 * r + 0.587 * g + 0.114 * b) < 110;
  }

  function renderPresets(elId, apply) {
    const el = $(elId);
    if (!el) return;
    el.innerHTML = "";
    PRESETS.forEach(hex => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "md-preset";
      b.textContent = hex;
      b.style.color = hex;
      if (isDark(hex)) b.style.background = "#fff";
      b.addEventListener("click", () => apply(hexToRgb(hex)));
      el.appendChild(b);
    });
  }

  function setDirty(dirty) {
    const btn = $("re-save");
    if (!btn) return;
    btn.style.boxShadow = dirty ? "0 0 14px rgba(255,255,255,0.35)" : "";
    btn.style.borderColor = dirty ? "rgba(255,255,255,0.35)" : "";
    if (dirty) scheduleRender();
  }

  function scheduleRender() {
    clearTimeout(renderTimer);
    renderTimer = setTimeout(renderPreview, 450);
  }

  async function renderPreview() {
    const img = $("re-preview"), ph = $("re-canvas-ph"), st = $("re-preview-status");
    const my = ++renderSeq;
    if (st) st.textContent = "Rendering preview…";
    try {
      const res = await fetch("/api/v1/user/rank-preview/render", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: state.config }),
      });
      if (!res.ok) {
        let msg = `Preview failed (HTTP ${res.status}).`;
        try { const d = await res.json(); if (d && d.error) msg = d.error; } catch (e) {}
        throw new Error(msg);
      }
      const blob = await res.blob();
      if (my !== renderSeq) return;
      const url = URL.createObjectURL(blob);
      if (previewURL) URL.revokeObjectURL(previewURL);
      previewURL = url;
      if (img) { img.src = url; img.style.display = "block"; }
      if (ph) ph.style.display = "none";
      if (st) st.textContent = "";
    } catch (err) {
      if (my !== renderSeq) return;
      if (ph) ph.style.display = "";
      if (st) st.textContent = String((err && err.message) || err);
    }
  }

  function refreshColorUI() {
    updateColorInputs();
  }

  function bindToggles() {
    document.querySelectorAll('input[data-el]').forEach(inp => {
      const key = inp.dataset.el;
      if (state.config.elements[key]) inp.checked = !!state.config.elements[key].enabled;
      inp.onchange = () => {
        if (state.config.elements[key]) state.config.elements[key].enabled = inp.checked;
        renderOverlay();
        setDirty(true);
      };
    });
  }

  /* ── Canvas overlay: drag/resize writing card-space x/y/size ── */
  const CARD_W = 900, CARD_H = 300;
  const OVERLAY_DEFS = {
    avatar:   { label: "Avatar",  resize: "square" },
    name:     { label: "Name" },
    xp_value: { label: "XP" },
    rank:     { label: "Rank" },
    xp_bar:   { label: "XP bar",  resize: "rect" },
    xp_ratio: { label: "XP ratio" },
  };
  let selectedEl = null;
  let rankTextW = 110;

  function measureRankText() {
    try {
      const ctx = document.createElement("canvas").getContext("2d");
      ctx.font = "700 22px Lexend, sans-serif";
      rankTextW = Math.max(40, Math.round(ctx.measureText("#3 of 284").width));
    } catch (e) { rankTextW = 110; }
  }

  // Mirror of image_builder geometry (bot `or` semantics: 0 falls to default).
  function geomFor(key) {
    const els = state.config.elements;
    const av = els.avatar || {};
    const asize = av.size || 180;
    const ax = av.x || 54;
    const ay = av.y || (30 + Math.floor((240 - asize) / 2));
    const tx = ax + asize + 24, ty = ay + 12;
    const bar = els.xp_bar || {};
    const bx = bar.x || tx, by = bar.y || (ay + asize - 40);
    const bw = bar.width || (840 - (asize + 120)), bh = bar.height || 14;
    switch (key) {
      case "avatar": return { x: ax, y: ay, w: asize, h: asize };
      case "name": {
        const e = els.name || {};
        return { x: e.x || tx, y: e.y || ty, w: 220, h: 34 };
      }
      case "xp_value": {
        const e = els.xp_value || {};
        return { x: e.x || tx, y: e.y || (ty + 42), w: 140, h: 26 };
      }
      case "rank": {
        const e = els.rank || {};
        return { x: e.x || Math.max(bx, bx + bw - rankTextW), y: e.y || (by - 24), w: 150, h: 28 };
      }
      case "xp_bar": return { x: bx, y: by, w: bw, h: bh };
      case "xp_ratio": {
        const e = els.xp_ratio || {};
        return { x: e.x || bx, y: e.y || (by + bh + 8), w: 180, h: 22 };
      }
      default: return null;
    }
  }

  function chipPos(el, g) {
    el.style.left = (g.x / CARD_W * 100) + "%";
    el.style.top = (g.y / CARD_H * 100) + "%";
    el.style.width = (g.w / CARD_W * 100) + "%";
    el.style.height = (g.h / CARD_H * 100) + "%";
  }

  function clampNum(v, lo, hi) {
    const n = Math.round(Number(v));
    if (isNaN(n)) return lo;
    return Math.max(lo, Math.min(hi, n));
  }

  function renderOverlay() {
    const layer = $("re-overlay");
    if (!layer) return;
    layer.innerHTML = "";
    Object.keys(OVERLAY_DEFS).forEach(key => {
      const cfg = state.config.elements[key];
      if (!cfg || !cfg.enabled) return;
      const g = geomFor(key);
      if (!g) return;
      const chip = document.createElement("div");
      chip.className = "re-chip" + (selectedEl === key ? " is-selected" : "");
      chip.dataset.el = key;
      chipPos(chip, g);
      chip.innerHTML = `<span class="re-chip-tag">${OVERLAY_DEFS[key].label}</span>` +
        (OVERLAY_DEFS[key].resize ? `<span class="re-handle"></span>` : ``);
      chip.addEventListener("pointerdown", e => onChipDown(e, key));
      chip.addEventListener("dblclick", e => {
        e.stopPropagation();
        const c = state.config.elements[key];
        if (!c) return;
        ["x", "y", "size", "width", "height"].forEach(f => { delete c[f]; });
        renderOverlay();
        setDirty(true);
      });
      layer.appendChild(chip);
    });
  }

  function positionChips() {
    const layer = $("re-overlay");
    if (!layer) return;
    layer.querySelectorAll(".re-chip").forEach(chip => {
      const g = geomFor(chip.dataset.el);
      if (g) chipPos(chip, g);
    });
  }

  function onChipDown(e, key) {
    if (e.button !== undefined && e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    selectedEl = key;
    document.querySelectorAll(".re-chip").forEach(c => c.classList.toggle("is-selected", c.dataset.el === key));
    const stage = $("re-stage");
    const el = state.config.elements[key];
    if (!stage || !el) return;
    const g0 = geomFor(key);
    const start = {
      x: el.x || g0.x, y: el.y || g0.y,
      size: el.size || g0.w, width: el.width || g0.w, height: el.height || g0.h,
    };
    const resizing = !!(e.target.closest && e.target.closest(".re-handle"));
    const sx = e.clientX, sy = e.clientY;
    const move = ev => {
      const scale = stage.clientWidth / CARD_W || 1;
      const dx = Math.round((ev.clientX - sx) / scale);
      const dy = Math.round((ev.clientY - sy) / scale);
      if (resizing) {
        if (key === "avatar") {
          el.size = clampNum(Math.max(start.size + dx, start.size + dy), 16, 512);
        } else if (key === "xp_bar") {
          el.width = clampNum(start.width + dx, 50, 900);
          el.height = clampNum(start.height + dy, 4, 100);
        }
      } else {
        el.x = clampNum(start.x + dx, 0, 900);
        el.y = clampNum(start.y + dy, 0, 300);
      }
      positionChips();
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      renderOverlay();
      setDirty(true);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  }

  function markBgSelected(name) {
    document.querySelectorAll(".bg-thumb").forEach(t => t.classList.toggle("selected", t.dataset.name === name));
  }

  function setBgStatus(msg) {
    const el = $("bg-status");
    if (el) el.textContent = msg;
  }

  function refreshGallery() {
    const g = $("bg-gallery");
    if (!g) return;
    if (!bgList.length) {
      g.innerHTML = '<div class="re-gallery-empty">No server backgrounds available.</div>';
      setBgStatus("0 backgrounds found.");
      return;
    }
    setBgStatus(`${bgList.length} backgrounds loaded.`);
    g.innerHTML = bgList.map(entry => {
      // Manifest objects {id, url, thumb} or legacy filename strings.
      const id = typeof entry === "string" ? entry : entry.id;
      const raw = typeof entry === "string"
        ? `/api/v1/user/backgrounds/${encodeURIComponent(entry)}`
        : (entry.thumb || entry.url);
      // Full-res files (6K+) load through a resize proxy for thumbs;
      // falls back to the raw URL if the proxy ever fails.
      let src = raw;
      try {
        const u = new URL(raw, location.href);
        if (/^https?:$/.test(u.protocol) && u.origin !== location.origin) {
          const hostpath = `${u.host}${u.pathname}${u.search}`;
          src = `https://images.weserv.nl/?url=${hostpath}&w=320&q=70&output=jpg`;
        }
      } catch (e) { /* keep raw */ }
      const safe = String(id).replace(/"/g, "");
      return `<img class="bg-thumb" src="${src}" data-name="${safe}" alt="${safe}" loading="lazy" decoding="async" onerror="this.onerror=null;this.src='${raw}';" />`;
    }).join("");
    g.querySelectorAll(".bg-thumb").forEach(thumb => {
      if (thumb.dataset.name === state.config.background) thumb.classList.add("selected");
      thumb.addEventListener("click", () => {
        state.config.background = thumb.dataset.name;
        markBgSelected(state.config.background);
        setDirty(true);
      });
    });
  }

  async function loadBackgrounds() {
    try {
      const res = await fetch("/api/v1/user/backgrounds", { credentials: "include" });
      if (res.status === 401) throw new Error("Not signed in (401) — log in again.");
      if (!res.ok) throw new Error(`Request failed (HTTP ${res.status}).`);
      const d = await res.json();
      bgList = d.backgrounds || [];
    } catch (e) {
      bgList = [];
      setBgStatus(`Could not load backgrounds: ${e.message || e}`);
    }
    refreshGallery();
  }

  async function loadSettings() {
    try {
      const d = await api("/rank-card");
      const saved = d.rank_card || {};
      state.config = {
        background: Object.prototype.hasOwnProperty.call(saved, "background") ? saved.background : DEFAULTS.background,
        primary_color: (saved.primary_color && saved.primary_color.length) ? saved.primary_color : DEFAULTS.primary_color,
        accent_color: (saved.accent_color && saved.accent_color.length) ? saved.accent_color : DEFAULTS.accent_color,
        elements: {},
      };
      for (const name in DEFAULTS.elements) {
        state.config.elements[name] = { ...DEFAULTS.elements[name], ...((saved.elements && saved.elements[name]) || {}) };
      }
      state.loaded = JSON.parse(JSON.stringify(state.config));
      const bg = state.config.background;
      if (typeof bg === "string" && bg && bg !== "random") markBgSelected(bg);
      else if (bg === null) markBgSelected("solid");
      setDirty(false);
    } catch (e) { /* keep defaults */ }
  }

  function bindStatic() {
    on("re-bg-random", "click", () => {
      state.config.background = "random";
      markBgSelected(null);
      setDirty(true);
    });

    on("re-bg-solid", "click", () => {
      state.config.background = null;
      markBgSelected("solid");
      setDirty(true);
    });

    const setPrimary = (rgb) => { state.config.primary_color = rgb; refreshColorUI(); setDirty(true); };
    const setAccent = (rgb) => { state.config.accent_color = rgb; refreshColorUI(); setDirty(true); };
    syncColorPair("re-color-primary", "re-primary-hex", setPrimary);
    syncColorPair("re-color-accent", "re-accent-hex", setAccent);
    renderPresets("re-primary-presets", setPrimary);
    renderPresets("re-accent-presets", setAccent);

    on("re-save", "click", async () => {
      const btn = $("re-save");
      const original = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = "Saving...";
      try {
        const res = await fetch("/api/v1/user/rank-card", {
          method: "POST", credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: state.config }),
        });
        const d = await res.json();
        if (!res.ok) throw new Error(d.detail || d.error || "save failed");
        state.loaded = JSON.parse(JSON.stringify(state.config));
        setDirty(false);
        if (typeof showToast === "function") showToast("Rank card saved.", "success");
      } catch (err) {
        if (typeof showToast === "function") showToast(String(err.message || err), "error", 5000);
      } finally {
        btn.disabled = false;
        btn.innerHTML = original;
        if (typeof lucide !== "undefined") lucide.createIcons();
      }
    });

    on("re-reset", "click", () => {
      if (!confirm("Reset your rank card to defaults? This can't be undone.")) return;
      state.config = JSON.parse(JSON.stringify(DEFAULTS));
      bindToggles();
      refreshColorUI();
      markBgSelected(null);
      setDirty(true);
      if (typeof showToast === "function") showToast("Reset to defaults. Hit Save to keep it.", "info");
    });
  }

  function initScrollbar() {
    // JS-drawn scrollbar: identical look in every browser, even ones
    // without native scrollbar styling. Falls back to native on failure.
    try {
      if (window.SimpleBar && window.ResizeObserver) {
        new SimpleBar(document.getElementById("app-shell"), { autoHide: false });
      }
    } catch (e) { /* native scrollbar fallback */ }
  }

  async function init() {
    try {
      initScrollbar();
      measureRankText();
      await loadSettings();
      refreshColorUI();
      bindToggles();
      bindStatic();
      renderOverlay();
      const stage = $("re-stage");
      if (stage) stage.addEventListener("pointerdown", e => {
        if (!e.target.closest || !e.target.closest(".re-chip")) {
          selectedEl = null;
          document.querySelectorAll(".re-chip").forEach(c => c.classList.remove("is-selected"));
        }
      });
    } catch (e) {
      if (typeof console !== "undefined") console.error("rank-editor init failed:", e);
    }
    try {
      await loadBackgrounds();
    } catch (e) {
      if (typeof console !== "undefined") console.error("rank-editor backgrounds failed:", e);
      const g = $("bg-gallery");
      if (g) g.innerHTML = '<div class="re-gallery-empty">Could not load backgrounds.</div>';
      setBgStatus(`Could not load backgrounds: ${e.message || e}`);
    }
    scheduleRender();
    if (typeof lucide !== "undefined") lucide.createIcons();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
