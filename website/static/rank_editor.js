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
        setDirty(true);
      };
    });
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
      if (/^https?:\/\//i.test(raw)) {
        const hostpath = raw.replace(/^https?:\/\//i, "");
        src = `https://images.weserv.nl/?url=${hostpath}&w=320&q=70&output=jpg`;
      }
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
      await loadSettings();
      refreshColorUI();
      bindToggles();
      bindStatic();
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
    if (typeof lucide !== "undefined") lucide.createIcons();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
