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

  function updateColorInputs() {
    $("re-color-primary").value = rgbToHex(state.config.primary_color);
    $("re-primary-hex").value = rgbToHex(state.config.primary_color);
    $("re-color-accent").value = rgbToHex(state.config.accent_color);
    $("re-accent-hex").value = rgbToHex(state.config.accent_color);
  }

  function syncColorPair(colorId, hexId, apply) {
    const col = $(colorId), hex = $(hexId);
    if (!col || !hex) return;
    col.addEventListener("input", () => { hex.value = col.value; apply(hexToRgb(col.value)); });
    hex.addEventListener("input", () => {
      if (/^#[0-9a-fA-F]{6}$/.test(hex.value)) { col.value = hex.value; apply(hexToRgb(hex.value)); }
    });
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
      b.addEventListener("click", () => apply(hexToRgb(hex)));
      el.appendChild(b);
    });
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
      };
    });
  }

  function markBgSelected(name) {
    document.querySelectorAll(".bg-thumb").forEach(t => t.classList.toggle("selected", t.dataset.name === name));
  }

  function refreshGallery() {
    const g = $("bg-gallery");
    if (!g) return;
    if (!bgList.length) { g.innerHTML = '<div class="re-gallery-empty">No server backgrounds available.</div>'; return; }
    g.innerHTML = bgList.map(name =>
      `<img class="bg-thumb" src="/api/v1/user/backgrounds/${encodeURIComponent(name)}" data-name="${name}" alt="${name}" loading="lazy" />`
    ).join("");
    g.querySelectorAll(".bg-thumb").forEach(thumb => {
      if (thumb.dataset.name === state.config.background) thumb.classList.add("selected");
      thumb.addEventListener("click", () => {
        state.config.background = thumb.dataset.name;
        $("re-bg-custom").value = state.config.background;
        $("re-bg-custom").disabled = false;
        markBgSelected(state.config.background);
      });
    });
  }

  async function loadBackgrounds() {
    try { bgList = (await api("/backgrounds")).backgrounds || []; } catch (e) { bgList = []; }
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
      const bgInput = $("re-bg-custom");
      const bg = state.config.background;
      const isCustom = typeof bg === "string" && bg && bg !== "random";
      bgInput.value = isCustom ? bg : "";
      bgInput.disabled = !bg || bg === "random" || bg === "solid";
      if (isCustom) markBgSelected(bg);
      else if (bg === null) markBgSelected("solid");
    } catch (e) { /* keep defaults */ }
  }

  function bindStatic() {
    $("re-bg-random").addEventListener("click", () => {
      state.config.background = "random";
      $("re-bg-custom").value = "";
      $("re-bg-custom").disabled = false;
      markBgSelected(null);
    });

    $("re-bg-solid").addEventListener("click", () => {
      state.config.background = null;
      $("re-bg-custom").value = "";
      $("re-bg-custom").disabled = true;
      markBgSelected("solid");
    });

    $("re-bg-custom").addEventListener("change", () => {
      const v = $("re-bg-custom").value.trim();
      if (v) { state.config.background = v; markBgSelected(null); }
      else { state.config.background = "random"; markBgSelected(null); }
    });

    const setPrimary = (rgb) => { state.config.primary_color = rgb; refreshColorUI(); };
    const setAccent = (rgb) => { state.config.accent_color = rgb; refreshColorUI(); };
    syncColorPair("re-color-primary", "re-primary-hex", setPrimary);
    syncColorPair("re-color-accent", "re-accent-hex", setAccent);
    renderPresets("re-primary-presets", setPrimary);
    renderPresets("re-accent-presets", setAccent);

    $("re-save").addEventListener("click", async () => {
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
        if (typeof showToast === "function") showToast("Rank card saved.", "success");
      } catch (err) {
        if (typeof showToast === "function") showToast(String(err.message || err), "error", 5000);
      } finally {
        btn.disabled = false;
        btn.innerHTML = original;
        if (typeof lucide !== "undefined") lucide.createIcons();
      }
    });

    $("re-reset").addEventListener("click", () => {
      if (!confirm("Reset your rank card to defaults? This can't be undone.")) return;
      state.config = JSON.parse(JSON.stringify(DEFAULTS));
      bindToggles();
      refreshColorUI();
      $("re-bg-custom").value = "";
      $("re-bg-custom").disabled = false;
      markBgSelected(null);
      if (typeof showToast === "function") showToast("Reset to defaults. Hit Save to keep it.", "info");
    });
  }

  async function init() {
    await loadSettings();
    refreshColorUI();
    bindToggles();
    bindStatic();
    await loadBackgrounds();
    if (typeof lucide !== "undefined") lucide.createIcons();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
