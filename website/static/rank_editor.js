(function () {
  const GID = window.__GUILD_ID;
  const CV_W = 900, CV_H = 300;
  const AVATAR_SIZE = 180, PADDING = 30;
  const PANEL_X = PADDING, PANEL_Y = PADDING;
  const PANEL_W = CV_W - 2 * PADDING, PANEL_H = CV_H - 2 * PADDING;
  const PANEL_COLOR = [10, 10, 16, 170];
  const PROGRESS_BG = [20, 20, 28, 190];
  const WHITE = [255, 255, 255];
  const SOLID_BG = [25, 25, 35];

  const DEFAULT_CONFIG = {
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

  // Sample display data used purely for the editor preview.
  let SAMPLE = { displayName: "User", level: 12, xp: 2470, xpNeeded: 150, rank: 3, totalMembers: 284, avatarUrl: "https://cdn.discordapp.com/embed/avatars/0.png" };

  const state = {
    config: JSON.parse(JSON.stringify(DEFAULT_CONFIG)),
    loaded: {},
    dirty: false,
    dragging: null,
    bgImages: {},
    avatarImg: null,
  };

  const canvas = document.getElementById("rank-canvas");
  const ctx = canvas.getContext("2d");
  const wrap = document.getElementById("re-canvas-wrap");

  function api(path, opts) {
    opts = opts || {};
    opts.credentials = "include";
    return fetch(`/api/v1/leveling/${GID}${path}`, opts).then(r => r.ok ? r.json() : r.text().then(t => { throw new Error(t) }));
  }

  function hexToRgb(h) {
    h = String(h || "").replace(/^#/, "");
    if (h.length === 3) h = h.split("").map(c => c + c).join("");
    let r, g, b;
    if (h.length === 6) { r = parseInt(h.slice(0, 2), 16); g = parseInt(h.slice(2, 4), 16); b = parseInt(h.slice(4, 6), 16); }
    else { r = g = b = 0; }
    return [r, g, b, 255];
  }
  function rgbToHex(c) {
    if (!c) return "#ffffff";
    const to = (n) => Math.round(Math.min(255, Math.max(0, n || 0))).toString(16).padStart(2, "0");
    return "#" + to(c[0]) + to(c[1]) + to(c[2]);
  }

  // ---- element bounds (mirrors image_builder geometry) ----
  function layout(e) {
    const avatarSize = e.avatar.size || AVATAR_SIZE;
    const ax = e.avatar.x != null ? e.avatar.x : PANEL_X + 24;
    const ay = e.avatar.y != null ? e.avatar.y : PANEL_Y + (PANEL_H - avatarSize) / 2;
    const tx = ax + avatarSize + 24;
    const ty = ay + 12;
    const xpValueY = ty + 42;
    const bx = e.xp_bar.x != null ? e.xp_bar.x : tx;
    const by = e.xp_bar.y != null ? e.xp_bar.y : ay + avatarSize - 40;
    const bw = e.xp_bar.width != null ? e.xp_bar.width : PANEL_W - (avatarSize + 120);
    const bh = e.xp_bar.height || 14;
    const rankY = by - 24;
    return { ax, ay, avatarSize, tx, ty, xpValueY, bx, by, bw, bh, rankY };
  }

  function textWidth(text, font) {
    ctx.font = font;
    return ctx.measureText(text).actualBoundingBoxLength + 4; // small pad
  }
  function fitText(text, font, maxWidth, fontSize) {
    ctx.font = `${fontSize}px Lexend, sans-serif`;
    if (ctx.measureText(text).actualBoundingBoxLength <= maxWidth) return text;
    let s = "...";
    let lo = 0, hi = text.length;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      const cand = text.slice(0, mid) + s;
      if (ctx.measureText(cand).actualBoundingBoxLength <= maxWidth) lo = mid; else hi = mid - 1;
    }
    return text.slice(0, lo) + s;
  }

  function drawRounded(x, y, w, h, r, color) {
    ctx.fillStyle = `rgba(${color[0]},${color[1]},${color[2]},${color[3] != null ? color[3] / 255 : 1})`;
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.fill("evenodd");
  }

  function drawAvatarCircle(ax, ay, size) {
    const img = state.avatarImg;
    if (img && img.complete) {
      ctx.save();
      ctx.beginPath();
      ctx.arc(ax + size / 2, ay + size / 2, size / 2, 0, Math.PI * 2);
      ctx.closePath();
      ctx.clip();
      ctx.drawImage(img, ax, ay, size, size);
      ctx.restore();
      ctx.strokeStyle = "rgba(255,255,255,1)";
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.arc(ax + size / 2, ay + size / 2, size / 2 + 3, 0, Math.PI * 2);
      ctx.stroke();
    } else {
      // default avatar fallback
      const colors = [[139, 92, 246], [22, 163, 74], [249, 115, 22], [239, 68, 68], [6, 182, 212], [236, 72, 153]];
      const c = colors[Math.abs(SAMPLE.rank) % colors.length];
      ctx.fillStyle = `rgba(${c[0]},${c[1]},${c[2]},1)`;
      const r = size / 2;
      ctx.beginPath();
      ctx.arc(ax + r, ay + r, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "rgba(255,255,255,1)";
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.arc(ax + r, ay + r, r + 3, 0, Math.PI * 2);
      ctx.stroke();
      ctx.fillStyle = "rgba(255,255,255,1)";
      ctx.font = `bold ${size * 0.45}px Lexend, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(String(SAMPLE.displayName)[0].toUpperCase(), ax + r, ay + r);
    }
  }

  function render() {
    const e = state.config.elements;
    // clear
    ctx.clearRect(0, 0, CV_W, CV_H);
    // background
    const bg = state.bgImage;
    if (bg && bg.complete) {
      ctx.drawImage(bg, 0, 0, CV_W, CV_H);
      ctx.fillStyle = "rgba(0,0,0,0.43)";
      ctx.fillRect(0, 0, CV_W, CV_H);
    } else {
      ctx.fillStyle = `rgba(${SOLID_BG[0]},${SOLID_BG[1]},${SOLID_BG[2]},1)`;
      ctx.fillRect(0, 0, CV_W, CV_H);
    }

    const L = layout(e);

    // panel
    if (e.panel.enabled) drawRounded(PANEL_X, PANEL_Y, PANEL_W, PANEL_H, 16, PANEL_COLOR);

    const pc = rgbToHex(state.config.primary_color);
    const ac = rgbToHex(state.config.accent_color);

    // avatar
    if (e.avatar.enabled) drawAvatarCircle(L.ax, L.ay, L.avatarSize);

    ctx.textAlign = "left";
    ctx.textBaseline = "top";

    // name + level
    if (e.name.enabled) {
      ctx.font = `bold 32px Lexend, sans-serif`;
      const levelSuffix = `● ${SAMPLE.level}`;
      const maxWidth = PANEL_X + PANEL_W - L.tx - 20 - textWidth(levelSuffix, `bold 32px Lexend, sans-serif`) - 12;
      const name = fitText(SAMPLE.displayName, `bold 32px Lexend, sans-serif`, maxWidth, 32) || "";
      const label = name ? `${name} ${levelSuffix}` : levelSuffix;
      ctx.fillStyle = `rgba(${state.config.accent_color[0]},${state.config.accent_color[1]},${state.config.accent_color[2]},1)`;
      ctx.fillText(label, L.tx, L.ty);
    }

    // xp value
    if (e.xp_value.enabled) {
      ctx.font = `bold 18px Lexend, sans-serif`;
      ctx.fillStyle = `rgba(${state.config.accent_color[0]},${state.config.accent_color[1]},${state.config.accent_color[2]},1)`;
      ctx.fillText(`${SAMPLE.xp.toLocaleString()} XP`, L.tx, L.xpValueY);
    }

    // rank
    if (e.rank.enabled) {
      ctx.font = `bold 22px Lexend, sans-serif`;
      const rankText = fitText(`#${SAMPLE.rank} of ${SAMPLE.totalMembers.toLocaleString()}`, `bold 22px Lexend, sans-serif`, L.bw, 22);
      const rw = ctx.measureText(rankText).actualBoundingBoxLength;
      const rx = e.rank.x != null ? e.rank.x : Math.max(L.bx, L.bx + L.bw - rw);
      const ry = e.rank.y != null ? e.rank.y : L.rankY;
      ctx.fillStyle = `rgba(${state.config.primary_color[0]},${state.config.primary_color[1]},${state.config.primary_color[2]},1)`;
      ctx.textAlign = "left";
      ctx.fillText(rankText, rx, ry);
      ctx.textAlign = "left";
    }

    // XP bar
    const levelXp = 100 * SAMPLE.level + 50 * (SAMPLE.level - 1);
    const nextXp = 100 * (SAMPLE.level + 1) + 50 * SAMPLE.level;
    const progress = Math.max(0, Math.min(1, (SAMPLE.xp - levelXp) / Math.max(1, nextXp - levelXp)));
    if (e.xp_bar.enabled) {
      // bg
      drawRounded(L.bx, L.by, L.bw, L.bh, 7, PROGRESS_BG);
      const fillW = Math.max(0, Math.floor(L.bw * progress));
      if (fillW > 0) drawRounded(L.bx, L.by, fillW, L.bh, 7, state.config.primary_color);
    }

    // xp ratio
    if (e.xp_ratio.enabled) {
      const nextLevelXp = SAMPLE.xp + SAMPLE.xpNeeded > 0 ? SAMPLE.xp + SAMPLE.xpNeeded : nextXp;
      ctx.font = `16px Lexend, sans-serif`;
      ctx.fillStyle = `rgba(${state.config.accent_color[0]},${state.config.accent_color[1]},${state.config.accent_color[2]},1)`;
      ctx.fillText(`${SAMPLE.xp.toLocaleString()} / ${nextLevelXp.toLocaleString()} XP`, L.bx, L.by + L.bh + 8);
    }
  }

  // ---- hot-spots for dragging (only positional elements) ----
  const HOTSPOTS = [
    { key: "avatar", label: "Avatar", bounds: () => { const e = state.config.elements; const L = layout(e); return [e.avatar.x != null ? e.avatar.x : L.ax, e.avatar.y != null ? e.avatar.y : L.ay, L.avatarSize, L.avatarSize]; } },
    { key: "xp_bar", label: "XP bar", bounds: () => { const e = state.config.elements; const L = layout(e); return [e.xp_bar.x != null ? e.xp_bar.x : L.bx, e.xp_bar.y != null ? e.xp_bar.y : L.by, L.bw, L.bh]; } },
    { key: "rank", label: "Rank", bounds: () => { const e = state.config.elements; const L = layout(e); const rw = 200; return [e.rank.x != null ? e.rank.x : L.bx + L.bw - rw, e.rank.y != null ? e.rank.y : L.rankY, rw, 26]; } },
    { key: "xp_ratio", label: "XP ratio", bounds: () => { const e = state.config.elements; const L = layout(e); return [e.xp_ratio.x != null ? e.xp_ratio.x : L.bx, e.xp_ratio.y != null ? e.xp_ratio.y : L.by + L.bh + 8, 200, 20]; } },
  ];

  function findHotspot(mx, my) {
    // reversed: top-most first
    for (let i = HOTSPOTS.length - 1; i >= 0; i--) {
      const hs = HOTSPOTS[i];
      const el = state.config.elements[hs.key];
      if (!el.enabled) continue;
      const [x, y, w, h] = hs.bounds();
      if (mx >= x - 4 && mx <= x + w + 4 && my >= y - 4 && my <= y + h + 4) return hs;
    }
    return null;
  }

  function placeHandles() {
    // remove existing
    document.querySelectorAll(".re-hotcell").forEach(n => n.remove());
    HOTSPOTS.forEach(hs => {
      const el = state.config.elements[hs.key];
      if (!el.enabled) return;
      const [x, y, w, h] = hs.bounds();
      const cell = document.createElement("div");
      cell.className = "re-hotcell";
      cell.style.left = `${x}px`;
      cell.style.top = `${y}px`;
      cell.style.width = `${w}px`;
      cell.style.height = `${h}px`;
      cell.dataset.el = hs.key;
      const handle = document.createElement("div");
      handle.className = "re-handle";
      cell.appendChild(handle);
      cell.title = hs.label;
      wrap.appendChild(cell);
    });
  }

  // ---- dragging ----
  let dragStart = { x: 0, y: 0 };
  let dragOrigin = { x: 0, y: 0 };
  wrap.addEventListener("mousedown", (ev) => {
    if (ev.button !== 0) return;
    const rect = canvas.getBoundingClientRect();
    const mx = (ev.clientX - rect.left) * (CV_W / rect.width);
    const my = (ev.clientY - rect.top) * (CV_H / rect.height);
    const hs = findHotspot(mx, my);
    if (!hs) { state.dragging = null; return; }
    const el = state.config.elements[hs.key];
    dragOrigin = { x: el.x == null ? hs.bounds()[0] : el.x, y: el.y == null ? hs.bounds()[1] : el.y };
    dragStart = { x: ev.clientX, y: ev.clientY };
    state.dragging = hs;
    ev.preventDefault();
  });
  document.addEventListener("mousemove", (ev) => {
    if (!state.dragging) return;
    const rect = canvas.getBoundingClientRect();
    const scaleX = CV_W / rect.width, scaleY = CV_H / rect.height;
    const dx = (ev.clientX - dragStart.x) * scaleX;
    const dy = (ev.clientY - dragStart.y) * scaleY;
    const el = state.config.elements[state.dragging.key];
    el.x = Math.max(0, Math.min(CV_W, dragOrigin.x + dx));
    el.y = Math.max(0, Math.min(CV_H, dragOrigin.y + dy));
    markDirty();
    render();
    placeHandles();
  });
  document.addEventListener("mouseup", () => {
    if (state.dragging) {
      const key = state.dragging.key;
      // snap back to default if dragged within 6px of origin
      const el = state.config.elements[key];
      const hs = HOTSPOTS.find(h => h.key === key);
      if (hs && el && el.x != null && el.y != null) {
        const [dx, dy] = hs.bounds();
        if (Math.abs(el.x - dx) < 6 && Math.abs(el.y - dy) < 6) { el.x = null; el.y = null; }
      }
      state.dragging = null;
    }
  });

  // ---- UI wiring ----
  function markDirty() { state.dirty = true; }

  function updateColorInputs() {
    document.getElementById("re-color-primary").value = rgbToHex(state.config.primary_color);
    document.getElementById("re-color-accent").value = rgbToHex(state.config.accent_color);
  }

  document.getElementById("re-color-primary").addEventListener("input", (ev) => {
    state.config.primary_color = hexToRgb(ev.target.value);
    markDirty(); render();
  });
  document.getElementById("re-color-accent").addEventListener("input", (ev) => {
    state.config.accent_color = hexToRgb(ev.target.value);
    markDirty(); render();
  });

  document.getElementById("re-bg-random").addEventListener("click", () => {
    state.config.background = "random";
    state.bgImage = null;
    markDirty(); render(); refreshBgGallery();
  });
  document.getElementById("re-bg-none").addEventListener("click", () => {
    state.config.background = null;
    state.bgImage = null;
    markDirty(); render(); refreshBgGallery();
  });
  document.getElementById("re-bg-custom").addEventListener("change", () => {
    const v = document.getElementById("re-bg-custom").value.trim();
    if (v) {
      state.config.background = v;
      loadBgImage(v);
    } else {
      state.config.background = "random";
      loadBgImage("random");
    }
    markDirty(); render(); refreshBgGallery();
  });

  let bgList = [];
  async function loadBackgrounds() {
    try {
      const d = await api("/backgrounds");
      bgList = d.backgrounds || [];
    } catch (e) {
      bgList = [];
    }
    refreshBgGallery();
  }
  function refreshBgGallery() {
    const g = document.getElementById("bg-gallery");
    if (!bgList.length) { g.innerHTML = '<div style="text-align:center;padding:0.8rem;color:rgba(255,255,255,0.25);font-size:0.8rem;">No server backgrounds available.</div>'; return; }
    g.innerHTML = bgList.map(name => {
      const selected = state.config.background === name;
      return `<img class="bg-thumb${selected ? ' selected' : ''}" src="/api/v1/leveling/${GID}/backgrounds/${encodeURIComponent(name)}" loading="lazy" data-name="${name}" alt="${name}" />`;
    }).join("");
    g.querySelectorAll(".bg-thumb").forEach(thumb => {
      thumb.addEventListener("click", () => {
        state.config.background = thumb.dataset.name;
        loadBgImage(thumb.dataset.name);
        markDirty(); render(); refreshBgGallery();
      });
    });
  }
  state.bgImage = null;
  async function loadBgImage(name) {
    if (name === "random") {
      // don't preload random; render will use solid + note
      state.bgImage = null;
      return;
    }
    if (!name) { state.bgImage = null; return; }
    if (state.bgImages[name]) { state.bgImage = state.bgImages[name]; return; }
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => { state.bgImages[name] = img; if (state.config.background === name) { state.bgImage = img; markDirty(); render(); } };
    img.onerror = () => { state.bgImages[name] = null; };
    img.src = `/api/v1/leveling/${GID}/backgrounds/${encodeURIComponent(name)}`;
    state.bgImage = img;
  }

  async function loadAvatar() {
    try {
      const d = await api("/role-preview");
      SAMPLE.displayName = d.display_name || SAMPLE.displayName;
      SAMPLE.avatarUrl = d.avatar_url || SAMPLE.avatarUrl;
    } catch (e) { /* keep defaults */ }
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => { state.avatarImg = img; render(); };
    img.onerror = () => { state.avatarImg = null; };
    img.src = SAMPLE.avatarUrl;
    state.avatarImg = img;
  }

  function bindToggles() {
    document.querySelectorAll('input[data-el]').forEach(inp => {
      inp.addEventListener("change", () => {
        const el = state.config.elements[inp.dataset.el];
        el.enabled = inp.checked;
        markDirty(); render(); placeHandles();
      });
    });
  }

  document.getElementById("re-save").addEventListener("click", saveChanges);
  document.getElementById("re-reset").addEventListener("click", resetEditor);

  async function saveChanges() {
    const btn = document.getElementById("re-save");
    btn.disabled = true; btn.textContent = "Saving...";
    try {
      const res = await fetch(`/api/v1/leveling/${GID}/settings`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: "rank_card", value: state.config }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || d.error || "save failed");
      for (const k in state.config) state.loaded[k] = JSON.parse(JSON.stringify(state.config[k]));
      state.dirty = false;
      if (typeof showToast === "function") showToast("Rank card settings saved.", "success");
    } catch (err) {
      if (typeof showToast === "function") showToast(String(err.message || err), "error", 5000);
    } finally {
      btn.disabled = false; btn.textContent = "Save";
    }
  }

  function resetEditor() {
    if (!confirm("Reset the rank card to defaults? This can't be undone.")) return;
    state.config = JSON.parse(JSON.stringify(DEFAULT_CONFIG));
    state.bgImage = null;
    markDirty();
    render(); placeHandles(); refreshBgGallery(); updateColorInputs();
    document.querySelectorAll('input[data-el]').forEach(inp => { inp.checked = state.config.elements[inp.dataset.el].enabled; });
  }

  async function loadSettings() {
    try {
      const d = await api("/settings");
      const s = d.settings || {};
      const saved = s.rank_card || {};
      // merge with defaults
      state.config = {
        background: Object.prototype.hasOwnProperty.call(saved, "background") ? saved.background : DEFAULT_CONFIG.background,
        primary_color: (saved.primary_color && saved.primary_color.length) ? saved.primary_color : DEFAULT_CONFIG.primary_color,
        accent_color: (saved.accent_color && saved.accent_color.length) ? saved.accent_color : DEFAULT_CONFIG.accent_color,
        elements: {},
      };
      const defs = DEFAULT_CONFIG.elements;
      const userElems = saved.elements || {};
      for (const name in defs) {
        state.config.elements[name] = { ...defs[name], ...(userElems[name] || {}) };
      }
      state.loaded = JSON.parse(JSON.stringify(state.config));
      // hydrate UI
      document.querySelectorAll('input[data-el]').forEach(inp => { inp.checked = state.config.elements[inp.dataset.el].enabled; });
      document.getElementById("re-bg-custom").value = typeof state.config.background === "string" && !["random", null].includes(state.config.background) ? state.config.background : "";
      if (typeof state.config.background === "string" && state.config.background && state.config.background !== "random") {
        loadBgImage(state.config.background);
      }
    } catch (e) { /* keep defaults */ }
  }

  // init
  (async function () {
    if (GID) {
      loadBackgrounds();
      await loadSettings();
    }
    bindToggles();
    await loadAvatar();
    updateColorInputs();
    render();
    placeHandles();
  })();
})();
