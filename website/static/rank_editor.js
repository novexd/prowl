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
      xp_ratio: { enabled: true, x: null, y: null },
    },
  };

  const SAMPLE = { displayName: "User", level: 12, xp: 2470, xpNeeded: 150, rank: 3, totalMembers: 284, avatarUrl: "https://cdn.discordapp.com/embed/avatars/0.png" };

  const state = {
    config: JSON.parse(JSON.stringify(DEFAULTS)),
    loaded: {},
    dirty: false,
    dragging: null,
    bgImages: {},
  };

  const card = document.getElementById("rc-card");
  const overlays = document.getElementById("rc-overlays");

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

  // ---- logical (card-space) helpers ----
  function pctOfLogical(logical, total) {
    return (logical / total) * 100;
  }
  function logicalToCss(x, y, w, h) {
    return {
      left: pctOfLogical(x, CV_W) + "%",
      top: pctOfLogical(y, CV_H) + "%",
      width: pctOfLogical(w, CV_W) + "%",
      height: pctOfLogical(h, CV_H) + "%",
    };
  }

  function layout() {
    const e = state.config.elements;
    const avSize = e.avatar.size || 180;
    const avatarX = e.avatar.x != null ? e.avatar.x : 54;
    const avatarY = e.avatar.y != null ? e.avatar.y : 60;
    const textX = avatarX + avSize + 24;
    const textY = avatarY + 12;
    const xpValueY = textY + 42;
    const barX = e.xp_bar.x != null ? e.xp_bar.x : textX;
    const barY = e.xp_bar.y != null ? e.xp_bar.y : avatarY + avSize - 40;
    const barW = e.xp_bar.width != null ? e.xp_bar.width : 840 - (avSize + 120);
    const barH = e.xp_bar.height || 14;
    return { e, avSize, avatarX, avatarY, textX, textY, xpValueY, barX, barY, barW, barH };
  }

  function fmt(n) { return Number(n).toLocaleString(); }

  function renderCard() {
    const e = state.config.elements;
    const L = layout();

    // background
    const bg = $("rc-bg");
    bg.style.background = e.panel.enabled ? "rgba(10,10,16,.65)" : "transparent";
    bg.style.display = "block";

    // panel
    $("rc-panel").style.display = e.panel.enabled ? "block" : "none";

    // avatar
    const avatar = $("rc-avatar");
    avatar.style.left = pctOfLogical(L.avatarX, CV_W) + "%";
    avatar.style.top = pctOfLogical(L.avatarY, CV_H) + "%";
    avatar.style.width = pctOfLogical(L.avSize, CV_W) + "%";
    avatar.style.height = pctOfLogical(L.avSize, CV_W) + "%";
    avatar.style.display = e.avatar.enabled ? "block" : "none";
    avatar.src = SAMPLE.avatarUrl || "https://cdn.discordapp.com/embed/avatars/0.png";

    const primary = rgbToHex(state.config.primary_color);
    const accent = rgbToHex(state.config.accent_color);

    // name + level
    const nameEl = $("rc-name");
    nameEl.style.left = pctOfLogical(e.name.x != null ? e.name.x : L.textX, CV_W) + "%";
    nameEl.style.top = pctOfLogical(e.name.y != null ? e.name.y : L.textY, CV_H) + "%";
    nameEl.style.color = accent;
    nameEl.style.display = e.name.enabled ? "flex" : "none";
    $("rc-name-text").textContent = SAMPLE.displayName || "User";
    $("rc-level").textContent = `● ${SAMPLE.level}`;
    $("rc-level").style.color = accent;

    // xp value
    const xpValue = $("rc-xpvalue");
    xpValue.style.left = pctOfLogical(e.xp_value.x != null ? e.xp_value.x : L.textX, CV_W) + "%";
    xpValue.style.top = pctOfLogical(e.xp_value.y != null ? e.xp_value.y : L.xpValueY, CV_H) + "%";
    xpValue.style.color = accent;
    xpValue.style.display = e.xp_value.enabled ? "block" : "none";
    xpValue.textContent = `${fmt(SAMPLE.xp)} XP`;
    if (e.xp_value.x != null) xpValue.style.left = pctOfLogical(e.xp_value.x, CV_W) + "%";
    if (e.xp_value.y != null) xpValue.style.top = pctOfLogical(e.xp_value.y, CV_H) + "%";

    // rank (right-aligned to bar right by default)
    const rankEl = $("rc-rank");
    rankEl.style.top = pctOfLogical(L.barY - 24, CV_H) + "%";
    rankEl.style.color = primary;
    rankEl.style.display = e.rank.enabled ? "block" : "none";
    rankEl.textContent = `#${SAMPLE.rank} of ${fmt(SAMPLE.totalMembers)}`;
    if (e.rank.x != null) rankEl.style.left = pctOfLogical(e.rank.x, CV_W) + "%";
    if (e.rank.y != null) rankEl.style.top = pctOfLogical(e.rank.y, CV_H) + "%";

    // bar
    const levelXp = 100 * SAMPLE.level + 50 * (SAMPLE.level - 1);
    const nextXp = 100 * (SAMPLE.level + 1) + 50 * SAMPLE.level;
    const progress = Math.max(0, Math.min(1, (SAMPLE.xp - levelXp) / Math.max(1, nextXp - levelXp)));
    const bar = $("rc-bar");
    bar.style.left = pctOfLogical(L.barX, CV_W) + "%";
    bar.style.top = pctOfLogical(L.barY, CV_H) + "%";
    bar.style.width = pctOfLogical(L.barW, CV_W) + "%";
    bar.style.height = pctOfLogical(L.barH, CV_H) + "%";
    bar.style.display = e.xp_bar.enabled ? "block" : "none";
    $("rc-bar-fill").style.width = (progress * 100) + "%";
    $("rc-bar-fill").style.background = primary;

    // xp ratio
    const nextLevelXp = SAMPLE.xp + SAMPLE.xpNeeded > 0 ? SAMPLE.xp + SAMPLE.xpNeeded : nextXp;
    const ratio = $("rc-xpratio");
    ratio.style.left = pctOfLogical(L.barX, CV_W) + "%";
    ratio.style.top = pctOfLogical(L.barY + L.barH + 8, CV_H) + "%";
    ratio.style.color = accent;
    ratio.style.display = e.xp_ratio.enabled ? "block" : "none";
    ratio.textContent = `${fmt(SAMPLE.xp)} / ${fmt(nextLevelXp)} XP`;
    if (e.xp_ratio.x != null) ratio.style.left = pctOfLogical(e.xp_ratio.x, CV_W) + "%";
    if (e.xp_ratio.y != null) ratio.style.top = pctOfLogical(e.xp_ratio.y, CV_H) + "%";

    applyBackgroundStyle();
  }

  // ---- scale-aware coordinate conversion ----
  function cardScale() {
    const cr = card.getBoundingClientRect();
    // displayed size vs logical size
    return { sx: CV_W / cr.width, sy: CV_H / cr.height, cr };
  }
  // convert a viewport point to logical card coords
  function toLogical(clientX, clientY) {
    const s = cardScale();
    const crect = s.cr;
    return { x: (clientX - crect.left) * s.sx, y: (clientY - crect.top) * s.sy };
  }

  // ---- overlay positioning via measured getBoundingClientRect ----
  const HOT_KEYS = ["panel", "avatar", "name", "xp_value", "rank", "xp_bar", "xp_ratio"];
  const LABELS = {
    panel: "Panel", avatar: "Avatar", name: "Name", xp_value: "XP value",
    rank: "Rank", xp_bar: "XP bar", xp_ratio: "XP ratio",
  };

  function measureOverlays() {
    overlays.innerHTML = "";
    const crect = card.getBoundingClientRect();
    HOT_KEYS.forEach(key => {
      const el = $(`rc-${key === "xp_value" ? "xpvalue" : key === "xp_ratio" ? "xpratio" : key === "xp_bar" ? "bar" : key}`);
      if (!el || el.style.display === "none") return;
      const er = el.getBoundingClientRect();
      const left = er.left - crect.left;
      const top = er.top - crect.top;
      const w = er.width;
      const h = er.height;
      const cell = document.createElement("div");
      cell.className = "rc-hot";
      cell.style.left = left + "px";
      cell.style.top = top + "px";
      cell.style.width = w + "px";
      cell.style.height = h + "px";
      cell.dataset.el = key;
      const lbl = document.createElement("span"); lbl.className = "rc-label"; lbl.textContent = LABELS[key];
      const handle = document.createElement("div"); handle.className = "rc-handle"; handle.dataset.el = key;
      cell.appendChild(lbl); cell.appendChild(handle);
      overlays.appendChild(cell);
    });
  }

  // ---- drag handling ----
  let dragInfo = null;
  overlays.addEventListener("mousedown", (ev) => {
    const handle = ev.target.closest(".rc-handle");
    if (!handle) return;
    ev.preventDefault();
    const key = handle.dataset.el;
    const el = state.config.elements[key];
    if (!el) return;
    const start = toLogical(ev.clientX, ev.clientY);
    const baseX = el.x != null ? el.x : (el.y != null ? null : null) || defaultOrigin(key);
    dragInfo = { key, startX: el.x != null ? el.x : defaultOrigin(key).x, startY: el.y != null ? el.y : defaultOrigin(key).y, start };
    ev.target.closest(".rc-hot").classList.add("dragging");
  });

  document.addEventListener("mousemove", (ev) => {
    if (!dragInfo) return;
    const cur = toLogical(ev.clientX, ev.clientY);
    const el = state.config.elements[dragInfo.key];
    el.x = dragInfo.startX + (cur.x - dragInfo.start.x);
    el.y = dragInfo.startY + (cur.y - dragInfo.start.y);
    el.x = Math.max(0, Math.min(CV_W, el.x));
    el.y = Math.max(0, Math.min(CV_H, el.y));
    state.dirty = true;
    renderCard();
    measureOverlays();
  });
  document.addEventListener("mouseup", () => {
    if (dragInfo) {
      const el = state.config.elements[dragInfo.key];
      // snap back to default if near origin
      const origin = defaultOrigin(dragInfo.key);
      if (Math.abs(el.x - origin.x) < 6 && Math.abs(el.y - origin.y) < 6) { el.x = null; el.y = null; }
      dragInfo = null;
      document.querySelectorAll(".rc-hot").forEach(n => n.classList.remove("dragging"));
    }
  });

  function defaultOrigin(key) {
    const L = layout();
    switch (key) {
      case "avatar": return { x: 54, y: 60 };
      case "name": return { x: L.textX, y: L.textY };
      case "xp_value": return { x: L.textX, y: L.xpValueY };
      case "rank": return { x: Math.max(L.barX, L.barX + L.barW - 200), y: L.barY - 24 };
      case "xp_bar": return { x: L.barX, y: L.barY };
      case "xp_ratio": return { x: L.barX, y: L.barY + L.barH + 8 };
      default: return { x: 0, y: 0 };
    }
  }

  // ---- controls ----
  function updateColorInputs() {
    $("re-color-primary").value = rgbToHex(state.config.primary_color);
    $("re-color-accent").value = rgbToHex(state.config.accent_color);
  }

  function bindToggles() {
    document.querySelectorAll('input[data-el]').forEach(inp => {
      inp.checked = state.config.elements[inp.dataset.el].enabled;
      inp.onchange = () => {
        const el = state.config.elements[inp.dataset.el];
        el.enabled = inp.checked;
        state.dirty = true; renderCard(); measureOverlays();
      };
    });
  }

  $("re-color-primary").addEventListener("input", (ev) => {
    state.config.primary_color = hexToRgb(ev.target.value);
    $("re-primary-swatch").style.setProperty("--c", ev.target.value);
    state.dirty = true; renderCard(); measureOverlays();
  });
  $("re-color-accent").addEventListener("input", (ev) => {
    state.config.accent_color = hexToRgb(ev.target.value);
    $("re-accent-swatch").style.setProperty("--c", ev.target.value);
    state.dirty = true; renderCard(); measureOverlays();
  });

  $("re-bg-random").addEventListener("click", () => {
    state.config.background = "random";
    $("re-bg-custom").value = "";
    $("re-bg-custom").disabled = false;
    markBgSelected(null);
    state.dirty = true; renderCard(); measureOverlays();
  });
  $("re-bg-solid").addEventListener("click", () => {
    state.config.background = null;
    $("re-bg-custom").value = "";
    $("re-bg-custom").disabled = true;
    markBgSelected("solid");
    renderCard(); measureOverlays();
  });

  let bgList = [];
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
        state.dirty = true; renderCard(); measureOverlays();
      });
    });
  }

  $("re-bg-custom").addEventListener("change", () => {
    const v = $("re-bg-custom").value.trim();
    if (v) { state.config.background = v; markBgSelected(null); }
    else { state.config.background = "random"; markBgSelected(null); }
    state.dirty = true; renderCard(); measureOverlays();
  });

  async function loadPreview() {
    try {
      const d = await api("/rank-preview");
      if (d.display_name) SAMPLE.displayName = d.display_name;
      if (d.avatar_url) SAMPLE.avatarUrl = d.avatar_url;
    } catch (e) { /* keep defaults */ }
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
      $("re-bg-custom").value = (typeof state.config.background === "string" && state.config.background && state.config.background !== "random") ? state.config.background : "";
      $("re-bg-custom").disabled = !state.config.background || state.config.background === "random" || state.config.background === "solid";
      if (typeof state.config.background === "string" && state.config.background && state.config.background !== "random") {
        markBgSelected(state.config.background);
      } else if (state.config.background === null) {
        markBgSelected("solid");
      }
    } catch (e) { /* keep defaults */ }
  }

  $("re-save").addEventListener("click", async () => {
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
    state.dirty = true; renderCard(); measureOverlays();
    bindToggles(); updateColorInputs();
    $("re-bg-custom").value = ""; $("re-bg-custom").disabled = false; markBgSelected(null);
  });

  // ---- background image loading (preview only) ----
  function applyBackgroundStyle() {
    const bg = $("rc-bg");
    const bgName = state.config.background;
    if (bgName && typeof bgName === "string" && bgName !== "random") {
      if (bgName === "solid") { bg.style.background = "#181a1e"; }
      else {
        const img = state.bgImages[bgName];
        if (img && img.complete) { bg.style.background = `url(${img.src})`; bg.style.backgroundSize="cover"; bg.style.backgroundPosition="center"; }
        else { bg.style.background = "#181a1e"; loadBgImage(bgName); }
      }
    } else {
      bg.style.background = "#181a1e"; // random -> placeholder gradient in preview
    }
  }
  async function loadBgImage(name) {
    if (state.bgImages[name]) { applyBackgroundStyle(); return; }
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => { state.bgImages[name] = img; applyBackgroundStyle(); };
    img.onerror = () => { state.bgImages[name] = null; };
    img.src = `/api/v1/user/backgrounds/${encodeURIComponent(name)}`;
    state.bgImages[name] = img;
  }

  // init
  (async () => {
    await loadSettings();
    bindToggles();
    updateColorInputs();
    await loadPreview();
    renderCard();
    measureOverlays();
    loadBackgrounds();
  })();
})();
