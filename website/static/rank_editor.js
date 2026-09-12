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
    panel_color: [10, 10, 16, 170],
    elements: {
      panel: { enabled: true },
      avatar: { enabled: true, x: null, y: null, size: null },
      name: { enabled: true, x: null, y: null, color: "accent" },
      xp_value: { enabled: true, x: null, y: null, color: "accent" },
      rank: { enabled: true, x: null, y: null, color: "primary" },
      xp_bar: { enabled: true, x: null, y: null, width: null, height: 14, color: "primary" },
      xp_ratio: { enabled: true, color: "accent" },
    },
  };

  // Elements whose fill color can switch between the Primary/Accent slots.
  const COLOR_ELEMENTS = ["name", "xp_value", "rank", "xp_bar", "xp_ratio"];
  const EL_LABELS = {
    panel: "Dark panel", avatar: "Avatar", name: "Name & Level",
    xp_value: "XP value", rank: "Rank", xp_bar: "XP bar", xp_ratio: "XP ratio text",
  };
  const GRADIENT_DIRS = [
    ["horizontal", "Left to right"],
    ["vertical", "Top to bottom"],
    ["diagonal", "Diagonal"],
    ["radial", "Radial"],
  ];
  const MAX_STOPS = 4;

  const PRESETS = ["#5865f2", "#57f287", "#fee75c", "#faa61a", "#ed4245", "#eb459e", "#00c8ff", "#b6a2e0", "#ffffff", "#313338", "#000000"];

  const state = {
    config: JSON.parse(JSON.stringify(DEFAULTS)),
    loaded: {},
  };

  let bgList = [];
  let renderTimer = null, renderSeq = 0, previewURL = null;
  const colorSlots = [];
  const DEBUG = true;

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

  function on(id, ev, fn) {
    const el = $(id);
    if (el) el.addEventListener(ev, fn);
  }

  function clampCh(n, dflt) {
    n = Math.round(Number(n));
    if (isNaN(n)) return dflt;
    return Math.max(0, Math.min(255, n));
  }

  // Color model: a slot is either a solid [r,g,b,a?] or a gradient
  // {direction, colors:[[r,g,b,a], ...]}. Alpha defaults to 255.
  function isGrad(v) {
    return !!v && typeof v === "object" && !Array.isArray(v);
  }
  function normSolid(v) {
    if (isGrad(v)) v = (v.colors && v.colors[0]) || [255, 255, 255];
    if (!Array.isArray(v)) return [255, 255, 255, 255];
    return [clampCh(v[0], 255), clampCh(v[1], 255), clampCh(v[2], 255),
      v.length > 3 ? clampCh(v[3], 255) : 255];
  }
  function normGradient(v, fallback) {
    const dirs = GRADIENT_DIRS.map(d => d[0]);
    let direction = "horizontal", colors = null;
    if (isGrad(v)) {
      if (dirs.includes(v.direction)) direction = v.direction;
      const raw = v.colors || v.stops;
      if (Array.isArray(raw) && raw.length >= 2) {
        colors = raw.slice(0, MAX_STOPS).map(c => normSolid(c));
      }
    }
    if (!colors) {
      const base = normSolid(fallback !== undefined ? fallback : [139, 92, 246]);
      const second = normSolid(v);
      colors = [base, (second.join() === base.join()) ? [34, 211, 238, 255] : second];
    }
    return { direction, colors };
  }
  function alphaPct(c) {
    return Math.round((normSolid(c)[3] / 255) * 100);
  }

  /* Reusable solid/gradient color editor.
   * container: element to render into. opts: {label, get, set, presets=true}
   * get() returns the slot value from state; set(v) writes it back (then the
   * caller re-renders previews via setDirty). refresh() re-renders from state.
   */
  function colorField(container, opts) {
    if (!container) return { refresh() {} };
    const get = opts.get, set = opts.set;
    const showPresets = opts.presets !== false;

    function solidRow(color) {
      const c = normSolid(color);
      const wrap = document.createElement("div");
      wrap.className = "re-color-pick";
      const pct = Math.round((c[3] / 255) * 100);
      wrap.innerHTML =
        `<input type="color" class="re-swatch" value="${rgbToHex(c)}" />` +
        `<input type="text" class="md-input" value="${rgbToHex(c)}" style="max-width:90px;" />` +
        `<div class="re-alpha" data-tooltip="Transparency"><input type="range" min="0" max="100" value="${pct}" /><span>${pct}%</span></div>`;
      const [sw, hex, rangeWrap] = [wrap.children[0], wrap.children[1], wrap.children[2]];
      const range = rangeWrap.querySelector("input"), label = rangeWrap.querySelector("span");
      const apply = (rgb, a) => {
        const cur = normSolid(get());
        set([rgb[0], rgb[1], rgb[2], a !== undefined ? a : cur[3]]);
        setDirty(true);
      };
      sw.addEventListener("input", () => { hex.value = sw.value; apply(hexToRgb(sw.value)); });
      hex.addEventListener("input", () => {
        if (/^#[0-9a-fA-F]{6}$/.test(hex.value)) { sw.value = hex.value; apply(hexToRgb(hex.value)); }
      });
      range.addEventListener("input", () => {
        label.textContent = range.value + "%";
        apply(hexToRgb(sw.value), Math.round((Number(range.value) / 100) * 255));
      });
      return wrap;
    }

    function stopRow(grad, idx) {
      const c = normSolid(grad.colors[idx]);
      const row = document.createElement("div");
      row.className = "re-stop";
      const pct = Math.round((c[3] / 255) * 100);
      row.innerHTML =
        `<input type="color" class="re-swatch" value="${rgbToHex(c)}" />` +
        `<input type="text" class="md-input" value="${rgbToHex(c)}" style="max-width:82px;" />` +
        `<div class="re-alpha" title="Transparency"><input type="range" min="0" max="100" value="${pct}" /><span>${pct}%</span></div>` +
        `<button type="button" class="re-stop-remove" data-tooltip="Remove color" ${grad.colors.length <= 2 ? "disabled" : ""}><i data-lucide="x"></i></button>`;
      const [sw, hex, rangeWrap, rm] = [row.children[0], row.children[1], row.children[2], row.children[3]];
      const range = rangeWrap.querySelector("input"), label = rangeWrap.querySelector("span");
      const commit = (rgb, a) => {
        const g = normGradient(get());
        g.colors[idx] = [rgb[0], rgb[1], rgb[2], a !== undefined ? a : normSolid(g.colors[idx])[3]];
        set(g);
        setDirty(true);
      };
      sw.addEventListener("input", () => { hex.value = sw.value; commit(hexToRgb(sw.value)); });
      hex.addEventListener("input", () => {
        if (/^#[0-9a-fA-F]{6}$/.test(hex.value)) { sw.value = hex.value; commit(hexToRgb(hex.value)); }
      });
      range.addEventListener("input", () => {
        label.textContent = range.value + "%";
        commit(hexToRgb(sw.value), Math.round((Number(range.value) / 100) * 255));
      });
      rm.addEventListener("click", () => {
        const g = normGradient(get());
        if (g.colors.length <= 2) return;
        g.colors.splice(idx, 1);
        set(g);
        refresh();
        setDirty(true);
      });
      return row;
    }

    function gradientBody(grad) {
      const body = document.createElement("div");
      const dirSel = document.createElement("select");
      dirSel.className = "md-select re-dir";
      GRADIENT_DIRS.forEach(([val, label]) => {
        const o = document.createElement("option");
        o.value = val;
        o.textContent = label;
        if (val === grad.direction) o.selected = true;
        dirSel.appendChild(o);
      });
      dirSel.addEventListener("change", () => {
        const g = normGradient(get());
        g.direction = dirSel.value;
        set(g);
        setDirty(true);
      });
      body.appendChild(dirSel);
      grad.colors.forEach((_, i) => body.appendChild(stopRow(grad, i)));
      const add = document.createElement("button");
      add.type = "button";
      add.className = "re-add-stop";
      add.textContent = "+ Add color";
      if (grad.colors.length >= MAX_STOPS) add.disabled = true;
      add.addEventListener("click", () => {
        const g = normGradient(get());
        if (g.colors.length >= MAX_STOPS) return;
        g.colors.push([255, 255, 255, 255]);
        set(g);
        refresh();
        setDirty(true);
      });
      body.appendChild(add);
      return body;
    }

    function refresh() {
      const val = get();
      const grad = isGrad(val);
      container.innerHTML = "";
      const head = document.createElement("div");
      head.className = "re-field-head";
      const lab = document.createElement("div");
      lab.className = "md-behavior-label";
      lab.textContent = opts.label;
      head.appendChild(lab);
      if (!opts.lockMode) {
        const seg = document.createElement("div");
        seg.className = "re-seg";
        [["solid", "Solid"], ["gradient", "Gradient"]].forEach(([mode, text]) => {
          const b = document.createElement("button");
          b.type = "button";
          b.textContent = text;
          if ((mode === "gradient") === grad) b.classList.add("on");
          b.addEventListener("click", () => {
            const cur = get();
            if (mode === "gradient" && !isGrad(cur)) set(normGradient(null, cur));
            if (mode === "solid" && isGrad(cur)) set(normSolid(cur));
            refresh();
            setDirty(true);
          });
          seg.appendChild(b);
        });
        head.appendChild(seg);
      }
      container.appendChild(head);
      if (grad || opts.lockMode === "gradient") {
        container.appendChild(gradientBody(normGradient(val)));
      } else {
        container.appendChild(solidRow(val));
        if (showPresets) {
          const p = document.createElement("div");
          p.className = "md-presets";
          p.style.cssText = "margin:0.4rem 0 0 0;flex-wrap:wrap;";
          container.appendChild(p);
          PRESETS.forEach(hex => {
            const b = document.createElement("button");
            b.type = "button";
            b.className = "md-preset";
            b.textContent = hex;
            b.style.color = hex;
            if (isDark(hex)) b.style.background = "#fff";
            b.addEventListener("click", () => {
              const cur = normSolid(get());
              set([hexToRgb(hex)[0], hexToRgb(hex)[1], hexToRgb(hex)[2], cur[3]]);
              refresh();
              setDirty(true);
            });
            p.appendChild(b);
          });
        }
      }
      if (typeof lucide !== "undefined") lucide.createIcons();
    }

    refresh();
    return { refresh };
  }

  /* Dashboard tooltip replica (dashboard.js is not loaded on this page):
   * floating #prowl-tooltip for [data-tooltip], 400ms delay. */
  function initTooltips() {
    if (document.getElementById("prowl-tooltip")) return;
    const el = document.createElement("div");
    el.id = "prowl-tooltip";
    el.style.display = "none";
    document.body.appendChild(el);
    let timer;
    document.addEventListener("mouseover", (e) => {
      const target = (e.target && e.target.closest) ? e.target.closest("[data-tooltip]") : null;
      if (!target) { el.style.display = "none"; return; }
      clearTimeout(timer);
      timer = setTimeout(() => {
        el.textContent = target.getAttribute("data-tooltip");
        el.style.display = "block";
        position(e);
      }, 400);
    });
    document.addEventListener("mouseout", (e) => {
      if (e.target && e.target.closest && e.target.closest("[data-tooltip]")) {
        clearTimeout(timer);
        el.style.display = "none";
      }
    });
    document.addEventListener("mousemove", (e) => {
      if (el.style.display === "block") position(e);
    });
    function position(e) {
      const mx = e.clientX, my = e.clientY;
      let x = mx + 12, y = my + 12;
      const w = el.offsetWidth, h = el.offsetHeight;
      if (x + w > window.innerWidth) x = mx - w - 8;
      if (y + h > window.innerHeight) y = my - h - 8;
      el.style.left = x + "px";
      el.style.top = y + "px";
    }
  }

  function isDark(hex) {
    const [r, g, b] = hexToRgb(hex);
    return (0.299 * r + 0.587 * g + 0.114 * b) < 110;
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
    renderTimer = setTimeout(renderPreview, 250);
  }

  const POLL_MS = 750, POLL_MAX = 60;

  async function renderPreview() {
    const img = $("re-preview"), ov = $("re-ph-overlay"), st = $("re-preview-status");
    const my = ++renderSeq;
    const fail = msg => {
      if (my !== renderSeq) return;
      if (ov) ov.style.display = "";
      if (st) st.textContent = msg;
    };
    const show = blob => {
      if (my !== renderSeq) return;
      const url = URL.createObjectURL(blob);
      if (previewURL) URL.revokeObjectURL(previewURL);
      previewURL = url;
      if (DEBUG) console.info("[preview] received", blob.size, "bytes");
      if (img) { img.src = url; img.style.display = "block"; }
      if (ov) ov.style.display = "none";
      if (st) st.textContent = "";
    };
    if (st) st.textContent = "Rendering preview…";
    let job;
    try {
      const res = await fetch("/api/v1/user/rank-preview/render", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: state.config }),
      });
      job = await res.json().catch(() => ({}));
      if (!res.ok || !job.job_id) throw new Error((job && job.error) || `Preview failed (HTTP ${res.status}).`);
      if (DEBUG) console.info("[preview] dispatched job", job.job_id);
    } catch (err) {
      fail(String((err && err.message) || err));
      return;
    }
    for (let i = 0; i < POLL_MAX; i++) {
      if (my !== renderSeq) return;
      await new Promise(r => setTimeout(r, POLL_MS));
      if (my !== renderSeq) return;
      try {
        const res = await fetch(`/api/v1/user/rank-preview/result/${encodeURIComponent(job.job_id)}`, { credentials: "include" });
        if (res.headers.get("content-type", "").startsWith("image/")) {
          show(await res.blob());
          return;
        }
        const d = await res.json().catch(() => ({}));
        if (d && d.ready === false) continue;
        throw new Error((d && (d.error || d.message)) || `Preview failed (HTTP ${res.status}).`);
      } catch (err) {
        fail(String((err && err.message) || err));
        return;
      }
    }
    fail("Preview timed out — the bot may be warming up. Try again in a moment.");
  }

  function refreshColorUI() {
    colorSlots.forEach(s => { try { s.refresh(); } catch (e) { /* keep going */ } });
    refreshBgPanels();
  }

  function renderElementList() {
    const list = $("re-element-list");
    if (!list) return;
    list.innerHTML = "";
    Object.keys(EL_LABELS).forEach(key => {
      const cfg = state.config.elements[key] || {};
      const item = document.createElement("div");
      item.className = "md-behavior-item";
      const label = document.createElement("label");
      label.className = "md-toggle";
      const inp = document.createElement("input");
      inp.type = "checkbox";
      inp.checked = cfg.enabled !== false;
      inp.addEventListener("change", () => {
        if (state.config.elements[key]) state.config.elements[key].enabled = inp.checked;
        renderOverlay();
        setDirty(true);
      });
      const slider = document.createElement("span");
      slider.className = "md-toggle-slider";
      label.appendChild(inp);
      label.appendChild(slider);
      item.appendChild(label);
      const textWrap = document.createElement("div");
      const text = document.createElement("div");
      text.className = "md-behavior-label";
      text.textContent = EL_LABELS[key];
      textWrap.appendChild(text);
      item.appendChild(textWrap);
      if (COLOR_ELEMENTS.includes(key)) {
        const sel = document.createElement("select");
        sel.className = "md-select re-el-color";
        sel.setAttribute("data-tooltip", "Color source");
        [["primary", "Primary"], ["accent", "Accent"]].forEach(([val, textContent]) => {
          const o = document.createElement("option");
          o.value = val;
          o.textContent = textContent;
          sel.appendChild(o);
        });
        sel.value = (cfg.color === "primary" || cfg.color === "accent")
          ? cfg.color
          : (DEFAULTS.elements[key].color || "accent");
        sel.addEventListener("change", () => {
          if (state.config.elements[key]) state.config.elements[key].color = sel.value;
          setDirty(true);
        });
        item.appendChild(sel);
      }
      list.appendChild(item);
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
      chip.addEventListener("contextmenu", e => {
        e.preventDefault();
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

  function bgModeOf(bg) {
    if (bg && typeof bg === "object" && !Array.isArray(bg)) {
      return bg.mode === "gradient" ? "gradient" : "solid";
    }
    return "image";
  }

  function refreshBgPanels() {
    const mode = bgModeOf(state.config.background);
    const sel = $("re-bg-mode");
    if (sel) sel.value = mode;
    const gal = $("bg-gallery"), solid = $("re-bg-solidwrap"), grad = $("re-bg-gradwrap");
    if (gal) gal.style.display = mode === "image" ? "" : "none";
    if (solid) solid.style.display = mode === "solid" ? "" : "none";
    if (grad) grad.style.display = mode === "gradient" ? "" : "none";
    if (mode !== "image") markBgSelected(null);
  }

  function setBgMode(mode) {
    if (mode === "solid") {
      state.config.background = { mode: "solid", color: [25, 25, 35, 255] };
    } else if (mode === "gradient") {
      state.config.background = {
        mode: "gradient", direction: "horizontal",
        colors: [[139, 92, 246, 255], [34, 211, 238, 255]],
      };
    } else {
      state.config.background = "random";
    }
    refreshColorUI();
    setDirty(true);
  }

  function refreshGallery() {
    const g = $("bg-gallery");
    if (!g) return;
    if (!bgList.length) {
      g.innerHTML = '<div class="re-gallery-empty">No server backgrounds available.</div>';
      return;
    }
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
      let bg = Object.prototype.hasOwnProperty.call(saved, "background") ? saved.background : DEFAULTS.background;
      if (bg === null || bg === undefined) bg = { mode: "solid", color: [25, 25, 35, 255] };
      if (isGrad(bg)) {
        bg = bg.mode === "gradient"
          ? { mode: "gradient", ...normGradient(bg) }
          : { mode: "solid", color: normSolid(bg.color) };
      } else if (typeof bg !== "string" || !bg) {
        bg = DEFAULTS.background;
      }
      const slot = (v, dflt) => (isGrad(v) ? normGradient(v, dflt) : normSolid(v || dflt));
      state.config = {
        background: bg,
        primary_color: slot(saved.primary_color, DEFAULTS.primary_color),
        accent_color: slot(saved.accent_color, DEFAULTS.accent_color),
        panel_color: slot(saved.panel_color, DEFAULTS.panel_color),
        elements: {},
      };
      for (const name in DEFAULTS.elements) {
        const merged = { ...DEFAULTS.elements[name], ...((saved.elements && saved.elements[name]) || {}) };
        const c = merged.color;
        merged.color = (c === "primary" || c === "accent") ? c : (DEFAULTS.elements[name].color || "accent");
        state.config.elements[name] = merged;
      }
      state.loaded = JSON.parse(JSON.stringify(state.config));
      if (typeof state.config.background === "string" && state.config.background !== "random") {
        markBgSelected(state.config.background);
      }
      refreshBgPanels();
      setDirty(false);
    } catch (e) { /* keep defaults */ }
  }

  function bindStatic() {
    on("re-bg-random", "click", () => {
      state.config.background = "random";
      markBgSelected(null);
      refreshColorUI();
      setDirty(true);
    });

    const modeSel = $("re-bg-mode");
    if (modeSel) modeSel.addEventListener("change", () => setBgMode(modeSel.value));

    colorSlots.length = 0;
    colorSlots.push(colorField($("re-color-primary-wrap"), {
      label: "Primary",
      get: () => state.config.primary_color,
      set: (v) => { state.config.primary_color = v; },
    }));
    colorSlots.push(colorField($("re-color-accent-wrap"), {
      label: "Accent",
      get: () => state.config.accent_color,
      set: (v) => { state.config.accent_color = v; },
    }));
    colorSlots.push(colorField($("re-color-panel-wrap"), {
      label: "Panel",
      get: () => state.config.panel_color,
      set: (v) => { state.config.panel_color = v; },
    }));
    colorSlots.push(colorField($("re-bg-solidwrap"), {
      label: "Background color",
      get: () => {
        const bg = state.config.background;
        return (bg && bg.mode === "solid") ? (bg.color || [25, 25, 35, 255]) : [25, 25, 35, 255];
      },
      set: (v) => { state.config.background = { mode: "solid", color: normSolid(v) }; },
    }));
    colorSlots.push(colorField($("re-bg-gradwrap"), {
      label: "Background gradient",
      lockMode: "gradient",
      presets: false,
      get: () => {
        const bg = state.config.background;
        return (bg && bg.mode === "gradient") ? bg : normGradient(null, [139, 92, 246]);
      },
      set: (v) => { state.config.background = { mode: "gradient", ...normGradient(v) }; },
    }));

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
      renderElementList();
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
      initTooltips();
      measureRankText();
      await loadSettings();
      refreshColorUI();
      renderElementList();
      bindStatic();
      refreshColorUI();
      renderOverlay();
      const stage = $("re-stage");
      if (stage) stage.addEventListener("pointerdown", e => {
        if (!e.target.closest || !e.target.closest(".re-chip")) {
          selectedEl = null;
          document.querySelectorAll(".re-chip").forEach(c => c.classList.remove("is-selected"));
        }
      });
      const pimg = $("re-preview");
      if (pimg) pimg.addEventListener("error", () => {
        const ov = $("re-ph-overlay"), st = $("re-preview-status");
        if (ov) ov.style.display = "";
        if (st) st.textContent = "Image was blocked from rendering.";
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
