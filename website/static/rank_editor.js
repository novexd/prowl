(() => {
  const CV_W = 900, CV_H = 300;
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

  const SAMPLE = { displayName: "User", level: 12, xp: 2470, xpNeeded: 150, rank: 3, totalMembers: 284, avatarUrl: "https://cdn.discordapp.com/embed/avatars/0.png" };

  const state = {
    config: JSON.parse(JSON.stringify(DEFAULTS)),
    loaded: {},
    dirty: false,
    bgImages: {},
  };

  let editor = null;
  let bgList = [];

  const $ = (id) => document.getElementById(id);
  const api = (path, opts) => {
    opts = Object.assign({ credentials: "include" }, opts);
    return fetch(`/api/v1/user${path}`, opts).then(r => r.ok ? r.json() : r.text().then(t => { throw new Error(t) }));
  };

  function hexToRgb(h) {
    let s = String(h || "").replace(/^#/, "");
    if (s.length === 3) s = s.split("").map(c => c + c).join("");
    const n = parseInt(s, 16);
    if (!s || isNaN(n)) return [255, 255, 255, 255];
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255, 255];
  }
  function rgbToHex(c) {
    const to = (n) => Math.round(Math.min(255, Math.max(0, n || 0))).toString(16).padStart(2, "0");
    return "#" + to(c[0]) + to(c[1]) + to(c[2]);
  }

  const EL_LABELS = {
    panel: "Panel", avatar: "Avatar", name: "Name", xp_value: "XP value",
    rank: "Rank", xp_bar: "XP bar", xp_ratio: "XP ratio",
  };

  const EL_STYLES = {
    avatar: { width: "180px", height: "180px", borderRadius: "50%", objectFit: "cover" },
    name: { color: "#60a5fa", fontSize: "32px", fontWeight: "700" },
    xp_value: { color: "#60a5fa", fontSize: "18px" },
    rank: { color: "#ffffff", fontSize: "22px", fontWeight: "700" },
    xp_bar: { height: "14px", borderRadius: "7px", background: "rgba(20,20,28,.6)", overflow: "hidden" },
    xp_ratio: { color: "#60a5fa", fontSize: "16px" },
  };

  const EL_TEXT = {
    avatar: null,
    name: (s) => s.displayName + " ● " + s.level,
    xp_value: (s) => s.xp.toLocaleString() + " XP",
    rank: (s) => "#" + s.rank + " of " + s.totalMembers.toLocaleString(),
    xp_bar: null,
    xp_ratio: (s) => s.xp.toLocaleString() + " / " + (s.xp + s.xpNeeded).toLocaleString() + " XP",
  };

  const DEFAULT_ORIGINS = {
    avatar: { x: 54, y: 60 },
    name: { x: 274, y: 72 },
    xp_value: { x: 274, y: 114 },
    rank: { x: 514, y: 216 },
    xp_bar: { x: 274, y: 216 },
    xp_ratio: { x: 274, y: 238 },
  };

  function parseStyleNum(val) {
    const n = parseInt(val, 10);
    return isNaN(n) ? null : n;
  }

  function initGrapesJS() {
    const root = document.getElementById("gjs");
    if (editor) {
      editor.destroy();
      editor = null;
    }

    editor = grapesjs.init({
      container: root,
      width: "auto",
      height: 300,
      storageManager: false,
      panels: { defaults: [] },
      deviceManager: { devices: [{ name: "Card", width: CV_W, height: CV_H }] },
      blockManager: {
        blocks: Object.keys(EL_LABELS)
          .filter((k) => k !== "panel")
          .map((type) => ({
            id: type,
            label: EL_LABELS[type],
            category: "Elements",
            content: { type, active: true },
          })),
      },
      telemetry: false,
    });

    // Inject canvas sizing styles
    const styleEl = document.createElement("style");
    styleEl.textContent = `
      .gjs-canvas { width: ${CV_W}px !important; height: ${CV_H}px !important; }
      .gjs-cv-canvas { width: ${CV_W}px !important; height: ${CV_H}px !important; }
      .gjs-drop-area { width: ${CV_W}px !important; height: ${CV_H}px !important; }
      .gjs-drop-zone { width: ${CV_W}px !important; height: ${CV_H}px !important; }
    `;
    document.head.appendChild(styleEl);

    const Components = editor.Components;

    Object.entries(EL_LABELS).forEach(([type]) => {
      Components.addType(type, {
        model: {
          defaults: {
            name: EL_LABELS[type],
            tagName: type === "avatar" ? "img" : "div",
            draggable: true,
            resizable: {
              width: type === "avatar" || type === "xp_bar",
              height: type === "avatar",
              minDim: 10,
            },
            style: { ...EL_STYLES[type] },
            active: true,
          },
        },
        view: {
          onRender: function() {
            const el = this.el;
            if (type === "avatar") {
              el.src = SAMPLE.avatarUrl;
            } else {
              const textFn = EL_TEXT[type];
              if (textFn) el.textContent = textFn(SAMPLE);
            }
            if (type === "xp_bar") {
              const inner = document.createElement("div");
              inner.style.height = "100%";
              inner.style.width = "40%";
              inner.style.background = "#ffffff";
              inner.style.borderRadius = "7px";
              el.appendChild(inner);
            }
          },
        },
      });
    });

    const wrapper = editor.getWrapper();
    wrapper.setStyle({ position: "relative", background: getBgStyle(), backgroundSize: "cover", backgroundPosition: "center" });

    if (state.config.elements.panel.enabled) {
      editor.addComponent({
        type: "panel",
        tagName: "div",
        style: { position: "absolute", inset: "0", borderRadius: "12px", background: "rgba(10,10,16,.65)" },
        active: false,
      });
    }

    loadElementsToCanvas();

    editor.on("component:dragend", () => { state.dirty = true; });
    editor.on("component:resizestop", () => { state.dirty = true; });
    editor.on("style:change:position", () => { state.dirty = true; });
  }

  function getBgStyle() {
    const bg = state.config.background;
    if (bg === "random" || bg === null || bg === "solid") return "#181a1e";
    return `url(/api/v1/user/backgrounds/${encodeURIComponent(bg)})`;
  }

  function loadElementsToCanvas() {
    const e = state.config.elements;

    Object.entries(e).forEach(([key, cfg]) => {
      if (!cfg.enabled) return;
      if (key === "panel") return;

      const x = cfg.x != null ? cfg.x : DEFAULT_ORIGINS[key].x;
      const y = cfg.y != null ? cfg.y : DEFAULT_ORIGINS[key].y;

      const style = {
        position: "absolute",
        left: x + "px",
        top: y + "px",
      };

      if (key === "avatar" && cfg.size != null) {
        style.width = cfg.size + "px";
        style.height = cfg.size + "px";
      }

      if (key === "xp_bar") {
        if (cfg.width != null) style.width = cfg.width + "px";
        if (cfg.height != null) style.height = cfg.height + "px";
      }

      editor.addComponent({ type: key, style, active: true });
    });
  }

  function serializeToConfig() {
    const components = editor.getComponents();
    components.each((model) => {
      const type = model.get("type");
      if (!type || !EL_LABELS[type]) return;

      const left = parseStyleNum(model.style("left"));
      const top = parseStyleNum(model.style("top"));
      const width = parseStyleNum(model.style("width"));
      const height = parseStyleNum(model.style("height"));

      const el = state.config.elements[type];
      if (!el) return;

      el.enabled = model.isVisible() !== false;

      const origin = DEFAULT_ORIGINS[type] || { x: 0, y: 0 };
      el.x = (left != null && Math.abs(left - origin.x) >= 6) ? left : null;
      el.y = (top != null && Math.abs(top - origin.y) >= 6) ? top : null;

      if (type === "avatar") {
        el.size = (width != null && Math.abs(width - 180) >= 6) ? width : null;
      }
      if (type === "xp_bar") {
        const defaultW = 400;
        el.width = (width != null && Math.abs(width - defaultW) >= 6) ? width : null;
        if (height != null) el.height = height;
      }
    });

    const bgStyle = editor.getWrapper().getStyle();
    const bgMatch = bgStyle.match(/url\(['"]?([^'")]+)['"]?\)/);
    if (bgMatch) {
      const url = bgMatch[1];
      const name = url.replace("/api/v1/user/backgrounds/", "");
      if (name) state.config.background = name;
    }
  }

  function updateColorInputs() {
    $("re-color-primary").value = rgbToHex(state.config.primary_color);
    $("re-color-accent").value = rgbToHex(state.config.accent_color);
  }

  function bindToggles() {
    document.querySelectorAll('input[data-el]').forEach(inp => {
      const elKey = inp.dataset.el;
      inp.checked = state.config.elements[elKey].enabled;
      inp.onchange = () => {
        const el = state.config.elements[elKey];
        el.enabled = inp.checked;

        if (editor) {
          const components = editor.getComponents();
          components.each((model) => {
            if (model.get("type") === elKey) {
              model.setVisible(inp.checked);
            }
          });
        }

        state.dirty = true;
      };
    });
  }

  $("re-color-primary").addEventListener("input", (ev) => {
    state.config.primary_color = hexToRgb(ev.target.value);
    $("re-primary-swatch").style.setProperty("--c", ev.target.value);
    state.dirty = true;
  });

  $("re-color-accent").addEventListener("input", (ev) => {
    state.config.accent_color = hexToRgb(ev.target.value);
    $("re-accent-swatch").style.setProperty("--c", ev.target.value);
    state.dirty = true;
  });

  $("re-bg-random").addEventListener("click", () => {
    state.config.background = "random";
    $("re-bg-custom").value = "";
    $("re-bg-custom").disabled = false;
    markBgSelected(null);
    updateCanvasBg();
  });

  $("re-bg-solid").addEventListener("click", () => {
    state.config.background = null;
    $("re-bg-custom").value = "";
    $("re-bg-custom").disabled = true;
    markBgSelected("solid");
    updateCanvasBg();
  });

  async function loadBackgrounds() {
    try { bgList = (await api("/backgrounds")).backgrounds || []; } catch (e) { bgList = []; }
    refreshGallery();
  }

  function markBgSelected(name) {
    document.querySelectorAll(".bg-thumb").forEach(t => t.classList.toggle("selected", t.dataset.name === name));
  }

  function refreshGallery() {
    const g = $("bg-gallery");
    if (!bgList.length) { g.innerHTML = '<div class="re-gallery-empty">No server backgrounds available.</div>'; return; }
    g.innerHTML = bgList.map(name =>
      `<img class="bg-thumb" src="/api/v1/user/backgrounds/${encodeURIComponent(name)}" data-name="${name}" alt="${name}" loading="lazy" />`
    ).join("");
    g.querySelectorAll(".bg-thumb").forEach(thumb => {
      thumb.addEventListener("click", () => {
        state.config.background = thumb.dataset.name;
        $("re-bg-custom").value = state.config.background;
        $("re-bg-custom").disabled = false;
        markBgSelected(state.config.background);
        updateCanvasBg();
      });
    });
  }

  $("re-bg-custom").addEventListener("change", () => {
    const v = $("re-bg-custom").value.trim();
    if (v) { state.config.background = v; markBgSelected(null); }
    else { state.config.background = "random"; markBgSelected(null); }
    updateCanvasBg();
  });

  function updateCanvasBg() {
    if (!editor) return;
    const bg = getBgStyle();
    editor.getWrapper().setStyle({ position: "relative", background: bg, backgroundSize: "cover", backgroundPosition: "center" });
  }

  async function loadPreview() {
    try {
      const d = await api("/rank-preview");
      if (d.display_name) SAMPLE.displayName = d.display_name;
      if (d.avatar_url) SAMPLE.avatarUrl = d.avatar_url;
    } catch (e) { }
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
        state.config.elements[name] = { ...DEFAULTS.elements[name], ...(saved.elements && saved.elements[name] || {}) };
      }
      state.loaded = JSON.parse(JSON.stringify(state.config));
      const bgInput = $("re-bg-custom");
      const bgVal = typeof state.config.background === "string" && state.config.background && state.config.background !== "random";
      bgInput.value = bgVal ? state.config.background : "";
      bgInput.disabled = !state.config.background || state.config.background === "random" || state.config.background === "solid";
      if (bgVal) markBgSelected(state.config.background);
      else if (state.config.background === null) markBgSelected("solid");
    } catch (e) { }
  }

  $("re-save").addEventListener("click", async () => {
    serializeToConfig();
    const btn = $("re-save");
    btn.disabled = true; btn.textContent = "Saving...";
    try {
      const res = await fetch("/api/v1/user/rank-card", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: state.config }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || d.error || "save failed");
      state.loaded = JSON.parse(JSON.stringify(state.config));
      state.dirty = false;
      if (typeof showToast === "function") showToast("Rank card saved.", "success");
    } catch (err) {
      if (typeof showToast === "function") showToast(String(err.message || err), "error", 5000);
    } finally {
      btn.disabled = false; btn.textContent = "Save";
    }
  });

  $("re-reset").addEventListener("click", () => {
    if (!confirm("Reset your rank card to defaults? This can't be undone.")) return;
    state.config = JSON.parse(JSON.stringify(DEFAULTS));
    state.dirty = true;
    initGrapesJS();
    bindToggles();
    updateColorInputs();
    $("re-bg-custom").value = ""; $("re-bg-custom").disabled = false; markBgSelected(null);
    updateCanvasBg();
  });

  async function init() {
    await loadSettings();
    updateColorInputs();
    bindToggles();
    await loadBackgrounds();
    await loadPreview();
    initGrapesJS();
  }

  init();
})();
