"use strict";

const COLORS = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#be185d", "#4d7c0f"];
const PREFIXES = [[1e9, "G"], [1e6, "M"], [1e3, "k"], [1, ""], [1e-3, "m"], [1e-6, "u"], [1e-9, "n"], [1e-12, "p"]];
const SVG_NS = "http://www.w3.org/2000/svg";

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

function fmt(value, unit) {
  if (value === null || value === undefined || !Number.isFinite(value)) return String(value);
  const symbol = unit === "ohm" ? "Ω" : unit;
  if (unit === "dB" || unit === "deg") return `${+value.toPrecision(4)}${symbol}`;
  if (Math.abs(value) < 1e-15) return `0${symbol}`; // below anything a simulation resolves
  const abs = Math.abs(value);
  for (const [scale, prefix] of PREFIXES) {
    if (abs >= scale * 0.9995) return `${+(value / scale).toPrecision(4)}${prefix}${symbol}`;
  }
  return `${value.toExponential(3)}${symbol}`;
}

function niceTicks(lo, hi, count = 5) {
  if (!(hi > lo)) { hi = lo + 1; lo -= 1; }
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const ticks = [];
  const first = Math.floor(lo / step + 1e-9), last = Math.ceil(hi / step - 1e-9);
  for (let k = first; k <= last; k++) ticks.push(+(k * step).toPrecision(12));
  return ticks;
}

// --- issues, tests, schematic, log ---------------------------------------------------

function renderIssues(state) {
  const seen = new Set();
  const items = [];
  for (const key of ["check", "fw", "sim", "render", "pcb"]) {
    const result = state[key];
    if (!result) continue;
    for (const [kind, list] of [["error", result.errors], ["warning", result.warnings]]) {
      for (const issue of list || []) {
        const id = `${kind}|${issue.rule}|${issue.message}`;
        if (seen.has(id)) continue;
        seen.add(id);
        const where = issue.file ? `${issue.file}${issue.line ? `:${issue.line}` : ""}` : "";
        items.push(el("li", { class: kind },
          el("span", { class: "rule" }, issue.rule), " ",
          where ? el("span", { class: "where" }, where) : null, " ",
          issue.message));
      }
    }
  }
  $("issues").replaceChildren(...items);
  $("issues-section").hidden = items.length === 0;
}

function renderStatus(state) {
  const badge = $("status");
  const results = ["check", "fw", "sim", "render", "pcb"].map((k) => state[k]).filter(Boolean);
  if (results.length === 0) {
    badge.textContent = "no results";
    badge.className = "badge";
    return;
  }
  const failed = results.some((r) => !r.ok);
  badge.textContent = failed ? "failing" : "all passing";
  badge.className = `badge ${failed ? "fail" : "pass"}`;
}

function renderSchematic(state, version) {
  const path = state.render?.data?.schematic;
  const box = $("schematic");
  if (!path) {
    box.replaceChildren(el("p", { class: "muted" }, "No schematic yet."));
    return;
  }
  const parts = [el("img", { src: `/${path}?v=${version}`, alt: "schematic" })];
  const kicad = state.render?.data?.kicad;
  if (kicad?.svg) {
    const check = kicad.netlist_matches ? "netlist matches the design" : "netlist NOT checked";
    parts.push(el("details", {},
      el("summary", {}, `KiCad schematic · ${kicad.schematic} · ${kicad.paper} · ${check}`),
      el("img", { src: `/${kicad.view ?? kicad.svg}?v=${version}`, alt: "KiCad schematic" }),
      el("p", {}, el("a", { href: `/${kicad.svg}?v=${version}`, target: "_blank" }, "Full sheet (SVG)"))));
  }
  box.replaceChildren(...parts);
}

function renderTests(state) {
  const tests = state.sim?.data?.tests;
  const box = $("tests");
  if (!tests || Object.keys(tests).length === 0) {
    box.replaceChildren(el("p", { class: "muted" }, "No simulation results yet."));
    return;
  }
  const blocks = [];
  for (const [name, test] of Object.entries(tests)) {
    blocks.push(el("div", { class: "test-name" },
      name,
      el("span", { class: `badge ${test.passed ? "pass" : "fail"}` }, test.passed ? "pass" : "fail"),
      el("span", { class: "muted" }, test.analysis)));
    const rows = test.expectations.map((e) => el("tr", {},
      el("td", {}, el("code", {}, e.measure)),
      el("td", {}, e.display ?? "—"),
      el("td", {}, e.expected ?? ""),
      el("td", { class: e.passed ? "ok" : "bad" }, e.passed ? "✓" : "✗")));
    blocks.push(el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "measure"), el("th", {}, "value"),
        el("th", {}, "expected"), el("th", {}, ""))),
      el("tbody", {}, ...rows)));
    if (test.displays) {
      blocks.push(el("div", { class: "screens" }, ...Object.entries(test.displays).map(([ref, d]) =>
        el("figure", { class: "screen" },
          el("img", { src: `/${d.image}?v=${state.version}`, alt: `${ref} at the end of ${name}` }),
          el("figcaption", {}, el("code", {}, ref), " ", el("code", { class: "screen-text" }, d.text))))));
    }
    if (test.events) {
      blocks.push(el("p", { class: "muted" },
        "events: " + test.events.map((e) => `${e.display} ${e.event}`).join(" · ")));
    }
    if (test.firmware) {
      const fw = test.firmware;
      const serial = Object.entries(fw.serial).map(([net, text]) =>
        el("div", {}, el("code", {}, net), el("pre", { class: "serial" }, text || "(nothing sent)")));
      blocks.push(el("details", {},
        el("summary", {}, `firmware in the loop · ${fw.image} · ${fw.windows} windows` +
          (fw.halted !== null ? ` · halted (${fw.halted})` : "")),
        ...serial));
    }
    if (test.operating_point) {
      const opRows = Object.entries(test.operating_point).map(([signal, v]) =>
        el("tr", {}, el("td", {}, el("code", {}, signal)), el("td", {}, v.display)));
      blocks.push(el("details", {}, el("summary", {}, "operating point"),
        el("table", {}, el("tbody", {}, ...opRows))));
    }
  }
  box.replaceChildren(...blocks);
}

function renderFirmware(state) {
  const data = state.fw?.data;
  const section = $("fw-section");
  if (!data || !data.pins) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const rows = data.pins.map((p) => el("tr", {},
    el("td", {}, el("code", {}, p.macro)),
    el("td", {}, `${p.pin} (GPIO ${p.gpio})`),
    el("td", {}, p.role + (p.pull !== "none" ? `, pull-${p.pull}` : "") + (p.initial ? `, starts ${p.initial}` : ""))));
  const build = data.build;
  let built = "not built";
  if (build && build.built) {
    built = `built ${build.elf} · flash ${build.size.flash} B` +
      (build.size_percent ? ` (${build.size_percent.flash}%)` : "") + ` · RAM ${build.size.ram} B`;
  } else if (build && build.built === false) {
    built = "build failed (see issues)";
  }
  $("fw").replaceChildren(
    el("p", { class: "muted" }, `${data.mcu} · ${data.family} · generated ${data.generated.join(", ")}`),
    el("p", { class: "muted" }, built),
    el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "macro"), el("th", {}, "pin"), el("th", {}, "role"))),
      el("tbody", {}, ...rows)));
}

function renderBoard(state, version) {
  const data = state.pcb?.data;
  $("board-section").hidden = false;
  if (!state.has_board) {
    $("board").replaceChildren(el("p", { class: "muted" },
      "No board for this project. Add ", el("code", {}, "design/board.yaml"), " with ",
      el("code", {}, "board: {}"), " and the next run places, routes, and exports one."));
    return;
  }
  if (!data || !data.outputs) {
    $("board").replaceChildren(el("p", { class: "muted" },
      state.pcb ? "The board was not built; see the issues above." : "Board not built yet."));
    return;
  }
  const r = data.routing || {};
  const d = data.drc || {};
  const facts = el("table", {}, el("tbody", {},
    el("tr", {}, el("td", {}, "size"), el("td", {}, `${data.size_mm[0]} × ${data.size_mm[1]} mm, 2 layers`)),
    el("tr", {}, el("td", {}, "routing"), el("td", {},
      `${r.tracks} tracks, ${r.vias} vias, ${r.track_length_mm} mm`,
      r.unrouted ? el("span", { class: "bad" }, ` · ${r.unrouted} unrouted`) : el("span", { class: "ok" }, " · fully routed"))),
    el("tr", {}, el("td", {}, "DRC"), el("td", { class: d.errors ? "bad" : "ok" },
      `${d.errors} errors, ${d.warnings} warnings`)),
    el("tr", {}, el("td", {}, "PCB house"), el("td", {},
      data.fab ? `limits of ${data.fab} checked` : "none set (add fab: jlcpcb to board.yaml)")),
    el("tr", {}, el("td", {}, "fabrication"), el("td", {},
      el("a", { href: `/${data.outputs.fabrication}` }, "Gerbers + drill + BOM (.zip)")))));
  const images = el("div", { class: "board-images" });
  if (data.outputs.svg) images.append(el("div", { class: "schematic" }, el("img", { src: `/${data.outputs.svg}?v=${version}`, alt: "board layout" })));
  if (data.outputs.assembly_svg) images.append(el("div", { class: "schematic" },
    el("img", { src: `/${data.outputs.assembly_svg}?v=${version}`, alt: "assembly drawing" }),
    el("p", { class: "muted" }, el("a", { href: `/${data.outputs.assembly_svg}`, target: "_blank" },
      "Assembly drawing"), " (true size: print at 100% scale)")));
  if (data.outputs.render_3d) images.append(el("img", { class: "render", src: `/${data.outputs.render_3d}?v=${version}`, alt: "3D render" }));
  $("board").replaceChildren(images, facts);
}

function renderLog(state) {
  const items = [...(state.log || [])].reverse().map((entry) => {
    const time = new Date(entry.time).toLocaleTimeString();
    if (entry.kind === "note") {
      return el("li", { class: "note" }, `${time} — note: ${entry.message}`);
    }
    const outcome = entry.ok ? el("span", { class: "ok" }, "pass")
      : el("span", { class: "bad" }, `${entry.error_count} error(s)`);
    const first = entry.errors?.[0] ? ` — ${entry.errors[0].rule}: ${entry.errors[0].message}` : "";
    const item = el("li", {}, `${time} eda ${entry.command} `, outcome, first);
    if (entry.note) item.append(el("div", { class: "note" }, entry.note));
    const changes = entry.design_changes?.length ? entry.design_changes
      : entry.changed?.length ? [`files: ${entry.changed.join(", ")}`] : [];
    if (changes.length) item.append(el("ul", { class: "changes" }, ...changes.map((c) => el("li", {}, el("code", {}, c)))));
    return item;
  });
  $("log").replaceChildren(...items);
}

// --- interactive waveforms ------------------------------------------------------------

const chartState = new Map(); // test name → { enabled: Set, view: [t0, t1] | null, play }
const playheads = new Map(); // test name → (time | null) => void, set by buildChart

// Fetches every test's waveform once; returns test name → data (for playback too).
async function renderWaveforms(state, version) {
  const tests = state.sim?.data?.tests || {};
  const tran = Object.entries(tests).filter(([, t]) => t.waveform);
  const box = $("waveforms");
  const loaded = new Map();
  playheads.clear();
  if (tran.length === 0) {
    box.replaceChildren(el("p", { class: "muted" }, "No transient tests."));
    return loaded;
  }
  const charts = [];
  for (const [name, test] of tran) {
    try {
      const response = await fetch(`/${test.waveform}?v=${version}`);
      const data = await response.json();
      loaded.set(name, data);
      charts.push(buildChart(name, test, data));
    } catch (err) {
      charts.push(el("p", { class: "bad" }, `${name}: cannot load waveform (${err})`));
    }
  }
  box.replaceChildren(...charts);
  return loaded;
}

function probeNames(measure) {
  return [...measure.matchAll(/(voltage|current)\([^()]*\)/g)].map((m) => m[0]);
}

function unwrapDegrees(values) {
  const out = [];
  let offset = 0;
  values.forEach((v, i) => {
    if (i) {
      const step = v + offset - out[i - 1];
      if (step > 180) offset -= 360; else if (step < -180) offset += 360;
    }
    out.push(v + offset);
  });
  return out;
}

// AC data stores complex values; the chart shows magnitude (dB) and phase per signal.
function bodeSignals(raw) {
  const signals = {};
  for (const [n, s] of Object.entries(raw)) {
    const im = s.imag || s.values.map(() => 0);
    signals[`db(${n})`] = {
      unit: "dB",
      values: s.values.map((re, i) => 20 * Math.log10(Math.max(Math.hypot(re, im[i]), 1e-300))),
    };
    signals[`phase(${n})`] = {
      unit: "deg",
      values: unwrapDegrees(s.values.map((re, i) => (Math.atan2(im[i], re) * 180) / Math.PI)),
    };
  }
  return signals;
}

function buildChart(name, test, data) {
  const freq = data.axis === "frequency";
  const signals = freq ? bodeSignals(data.signals) : data.signals;
  const names = Object.keys(signals);
  let st = chartState.get(name);
  if (!st) {
    const probes = test.expectations.flatMap((e) => probeNames(e.measure));
    const shown = freq ? probes.flatMap((p) => [`db(${p})`, `phase(${p})`]) : probes;
    const wanted = new Set(shown.filter((n) => n in signals));
    st = { enabled: wanted.size ? wanted : new Set(names.slice(0, 2)), view: null };
    chartState.set(name, st);
  }
  const container = el("div", { class: "chart" });
  const head = el("div", { class: "chart-head" }, el("strong", {}, name));
  if (data.x_label) head.append(el("span", { class: "muted" }, `sweep ${data.x_label}`));
  // Signals the test measures stay in view; the rest sit in a collapsible list.
  const more = el("div", { class: "chart-head" });
  const initial = new Set(st.enabled);
  names.forEach((signal, i) => {
    const box = el("input", { type: "checkbox" });
    box.checked = st.enabled.has(signal);
    box.addEventListener("change", () => {
      if (box.checked) st.enabled.add(signal); else st.enabled.delete(signal);
      draw();
    });
    const label = el("label", { style: `color:${COLORS[i % COLORS.length]}` }, box, ` ${signal}`);
    (initial.has(signal) ? head : more).append(label);
  });
  head.append(el("span", { class: "hint muted" }, "wheel: zoom · drag: pan · double-click: reset"));
  if (more.childElementCount) {
    head.append(el("details", { class: "more" },
      el("summary", {}, `all signals (${names.length})`), more));
  }
  const plot = svg("svg");
  container.append(head, plot);

  // Internal x axis: time, the swept value (dc), or log10(frequency) for AC so zoom and
  // pan work in decades.
  const time = freq ? data.x.map(Math.log10) : data.x;
  const xUnit = freq ? "Hz" : data.axis === "sweep" ? data.x_unit : "s";
  const xValue = (v) => (freq ? 10 ** v : v);
  const W = 900, PANEL = 200, L = 70, R = 16, T = 10, GAP = 34;
  let placePlayhead = () => {};
  if (data.axis === "time") {
    playheads.set(name, (t) => {
      st.play = t;
      placePlayhead();
    });
  }

  function draw() {
    const view = st.view || [time[0], time[time.length - 1]];
    const active = names.filter((n) => st.enabled.has(n));
    const units = [...new Set(active.map((n) => signals[n].unit))];
    const H = T + Math.max(units.length, 1) * (PANEL + GAP);
    plot.setAttribute("viewBox", `0 0 ${W} ${H}`);
    plot.replaceChildren();
    const sx = (t) => L + (t - view[0]) / (view[1] - view[0] || 1) * (W - L - R);
    const i0 = Math.max(0, lowerBound(time, view[0]) - 1);
    const i1 = Math.min(time.length, lowerBound(time, view[1]) + 1);
    const panels = [];
    units.forEach((unit, p) => {
      const top = T + p * (PANEL + GAP), bottom = top + PANEL;
      const inPanel = active.filter((n) => signals[n].unit === unit);
      let lo = Infinity, hi = -Infinity;
      for (const n of inPanel) {
        for (let i = i0; i < i1; i++) {
          const v = signals[n].values[i];
          if (v < lo) lo = v;
          if (v > hi) hi = v;
        }
      }
      const yTicks = niceTicks(lo, hi);
      const y0 = Math.min(lo, yTicks[0]), y1 = Math.max(hi, yTicks[yTicks.length - 1]);
      const sy = (v) => bottom - (v - y0) / (y1 - y0 || 1) * PANEL;
      plot.append(svg("rect", { x: L, y: top, width: W - L - R, height: PANEL, fill: "none", stroke: "var(--axis)" }));
      for (const tick of yTicks) {
        plot.append(svg("line", { x1: L, x2: W - R, y1: sy(tick), y2: sy(tick), stroke: "var(--grid)" }));
        const label = svg("text", { x: L - 6, y: sy(tick) + 4, "text-anchor": "end", "font-size": 11, fill: "currentColor" });
        label.textContent = fmt(tick, unit);
        plot.append(label);
      }
      const decades = [];
      for (let k = Math.ceil(view[0]); k <= Math.floor(view[1]); k++) decades.push(k);
      const xTicks = freq && decades.length >= 2 ? decades : niceTicks(view[0], view[1], 8);
      for (const tick of xTicks) {
        if (tick < view[0] || tick > view[1]) continue;
        plot.append(svg("line", { x1: sx(tick), x2: sx(tick), y1: top, y2: bottom, stroke: "var(--grid)" }));
        const label = svg("text", { x: sx(tick), y: bottom + 14, "text-anchor": "middle", "font-size": 11, fill: "currentColor" });
        label.textContent = fmt(xValue(tick), xUnit);
        plot.append(label);
      }
      for (const n of inPanel) {
        const values = signals[n].values;
        const pts = [];
        for (let i = i0; i < i1; i++) pts.push(`${sx(time[i]).toFixed(1)},${sy(values[i]).toFixed(1)}`);
        const color = COLORS[names.indexOf(n) % COLORS.length];
        plot.append(svg("polyline", { points: pts.join(" "), fill: "none", stroke: color, "stroke-width": 1.6 }));
      }
      panels.push({ top, bottom, sy, inPanel, unit });
    });
    // Playback position (see renderPlayback), moved without redrawing the traces.
    const playLine = svg("line", { y1: T, y2: H - GAP + 4, stroke: "#f59e0b", "stroke-width": 2, visibility: "hidden" });
    plot.append(playLine);
    placePlayhead = () => {
      const t = st.play;
      if (t === null || t === undefined || t < view[0] || t > view[1]) {
        playLine.setAttribute("visibility", "hidden");
        return;
      }
      playLine.setAttribute("x1", sx(t));
      playLine.setAttribute("x2", sx(t));
      playLine.setAttribute("visibility", "visible");
    };
    placePlayhead();
    const cursor = svg("g", { visibility: "hidden" });
    plot.append(cursor);
    plot.onmousemove = (ev) => {
      const t = toData(ev)[0];
      if (t < view[0] || t > view[1]) { cursor.setAttribute("visibility", "hidden"); return; }
      const i = Math.min(time.length - 1, lowerBound(time, t));
      cursor.replaceChildren();
      cursor.setAttribute("visibility", "visible");
      const x = sx(time[i]);
      cursor.append(svg("line", { x1: x, x2: x, y1: T, y2: H - GAP + 4, stroke: "currentColor", "stroke-dasharray": "3 3", opacity: 0.6 }));
      for (const panel of panels) {
        panel.inPanel.forEach((n, k) => {
          const color = COLORS[names.indexOf(n) % COLORS.length];
          cursor.append(svg("circle", { cx: x, cy: panel.sy(signals[n].values[i]), r: 3, fill: color }));
          const label = svg("text", { x: x + 6, y: panel.top + 14 + k * 14, "font-size": 11, fill: color });
          label.textContent = `${n} = ${fmt(signals[n].values[i], panel.unit)} @ ${fmt(xValue(time[i]), xUnit)}`;
          cursor.append(label);
        });
      }
    };
    plot.onmouseleave = () => cursor.setAttribute("visibility", "hidden");

    function toData(ev) {
      const box = plot.getBoundingClientRect();
      const px = (ev.clientX - box.left) / box.width * W;
      return [view[0] + (px - L) / (W - L - R) * (view[1] - view[0]), px];
    }
    plot.onwheel = (ev) => {
      ev.preventDefault();
      const [t] = toData(ev);
      const factor = ev.deltaY > 0 ? 1.25 : 0.8;
      st.view = clampView([t - (t - view[0]) * factor, t + (view[1] - t) * factor]);
      draw();
    };
    plot.onpointerdown = (ev) => {
      const startView = [...view];
      plot.setPointerCapture(ev.pointerId);
      plot.onpointermove = (move) => {
        const box = plot.getBoundingClientRect();
        const dt = (move.clientX - ev.clientX) / box.width * W / (W - L - R) * (startView[1] - startView[0]);
        st.view = clampView([startView[0] - dt, startView[1] - dt]);
        draw();
      };
      plot.onpointerup = () => { plot.onpointermove = null; };
    };
    plot.ondblclick = () => { st.view = null; draw(); };
  }

  function clampView([a, b]) {
    const lo = time[0], hi = time[time.length - 1];
    const span = Math.min(b - a, hi - lo);
    if (a < lo) { a = lo; b = lo + span; }
    if (b > hi) { b = hi; a = hi - span; }
    return [a, b];
  }

  draw();
  return container;
}

function lowerBound(array, value) {
  let lo = 0, hi = array.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (array[mid] < value) lo = mid + 1; else hi = mid;
  }
  return lo;
}

// --- playback -------------------------------------------------------------------------
// Replays a transient test in real time (or slower/faster): LEDs on the KiCad schematic
// glow with their simulated current, and the waveform charts show where the replay is.

const LED_COLORS = { red: "#ff3030", yellow: "#ffd60a", green: "#2ee65a", blue: "#3b82f6", white: "#f8fafc" };
const LED_FULL_A = 10e-3; // current drawn at full brightness
const LED_ON_A = 0.1e-3; // below this the LED is dark
const SPEEDS = [0.1, 0.25, 0.5, 1, 2, 5, 10];
const player = { test: null, t: 0, end: 0, speed: 1, playing: false, running: false, last: null, show: () => {} };

function sampleAt(xs, ys, t) {
  const i = lowerBound(xs, t);
  if (i <= 0) return ys[0];
  if (i >= xs.length) return ys[ys.length - 1];
  const f = (t - xs[i - 1]) / (xs[i] - xs[i - 1] || 1);
  return ys[i - 1] + f * (ys[i] - ys[i - 1]);
}

// Display parts: frames recorded by `eda sim` (out/display/<test>.<ref>.json), drawn here
// from their state, so the screens follow the replay.
const frameFiles = new Map(); // path?v=version → promise of the frames file

function loadFrames(path, version) {
  const key = `${path}?v=${version}`;
  if (!frameFiles.has(key)) {
    frameFiles.set(key, fetch(`/${key}`, { cache: "no-store" }).then((r) => r.json()));
  }
  return frameFiles.get(key);
}

function frameIndex(frames, t) {
  let lo = 0;
  let hi = frames.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (frames[mid].time <= t + 1e-12) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

const SEGMENT_COLORS = { red: "#ff2a1a", yellow: "#ffc21a", green: "#38e04a" };
const SEGMENT_POLYGONS = {
  a: "12,4 40,4 35,10 17,10", b: "41,6 37,40 32,36 35,11", c: "36,44 32,78 27,73 30,48",
  d: "8,80 30,80 25,74 12,74", e: "11,44 15,48 12,73 7,78", f: "16,6 19,11 16,36 11,40",
  g: "13,42 18,39 31,39 35,42 30,45 17,45",
};

function drawSegments(file, frame) {
  const digits = frame.state.digits;
  const meta = { color: "red", segments: Object.keys(digits[0]?.mean ?? {}), lit: 1e-4, ...file.meta };
  const on = SEGMENT_COLORS[meta.color] ?? SEGMENT_COLORS.red;
  const width = digits.length * 62 + 14;
  const root = svg("svg", { viewBox: `0 0 ${width} 112`, class: "screen-svg" });
  root.append(svg("rect", { width, height: 112, rx: 6, fill: "#1b1b1b" }));
  digits.forEach((digit, index) => {
    const group = svg("g", { transform: `translate(${14 + index * 62},14)` });
    for (const segment of meta.segments) {
      const lit = digit.lit.includes(segment);
      const level = Math.min(1, (digit.mean[segment] ?? 0) / (30 * meta.lit));
      const attrs = lit ? { fill: on, "fill-opacity": (0.35 + 0.65 * level).toFixed(2) } : { fill: "#3a3a3a" };
      group.append(segment === "dp"
        ? svg("circle", { cx: 43, cy: 77, r: 3.5, ...attrs })
        : svg("polygon", { points: SEGMENT_POLYGONS[segment], ...attrs }));
    }
    root.append(group);
  });
  return root;
}

// HD44780 character LCD: dots from the frame's character codes, the font and colours
// from the frames file, CGRAM characters from the frame; a blinking cursor blinks.
function drawLcd(file, frame, t) {
  const { rows, cgram, cursor, underline, blink, contrast } = frame.state;
  const on = frame.state.powered && frame.state.on;
  const colors = file.meta.colors;
  const pitch = 3.6;
  const cellW = 5 * pitch + 3;
  const cellH = 8 * pitch + 3;
  const width = 24 + rows[0].length * cellW;
  const height = 24 + rows.length * cellH;
  const ink = contrast === "faint" ? colors.faint : colors.lit;
  const unlit = contrast === "dark" ? colors.dark : colors.unlit;
  const blinkOn = blink && Math.floor((t ?? 0) / 0.4096) % 2 === 0;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, class: "screen-svg" });
  root.append(svg("rect", { width, height, rx: 4, fill: colors.glass }));
  rows.forEach((row, r) => row.forEach((code, c) => {
    let dots = [0, 0, 0, 0, 0, 0, 0, 0];
    if (on && code < 16 && cgram) dots = cgram.slice((code & 7) * 8, (code & 7) * 8 + 8).map((v) => v & 31);
    else if (on) dots = file.meta.font[String(code)] ?? [31, 17, 17, 17, 17, 17, 31, 0];
    const here = cursor && cursor[0] === r && cursor[1] === c;
    if (here && blinkOn) dots = [31, 31, 31, 31, 31, 31, 31, 31];
    else if (here && underline) dots = [...dots.slice(0, 7), 31];
    for (let y = 0; y < 8; y++) {
      for (let x = 0; x < 5; x++) {
        root.append(svg("rect", {
          x: 12 + c * cellW + x * pitch, y: 12 + r * cellH + y * pitch, width: 3, height: 3,
          fill: dots[y] & (1 << (4 - x)) ? ink : unlit,
        }));
      }
    }
  }));
  return root;
}

const DRAWERS = { segments: drawSegments, hd44780: drawLcd };

// One screen in the playback: shows the frame at time t, redrawn only when it changes.
function screenView(ref, path, version) {
  const holder = el("div", { class: "screen-live" }, el("p", { class: "muted" }, `${ref}: loading…`));
  const caption = el("figcaption");
  const view = { node: el("figure", { class: "screen" }, holder, caption), file: null, shown: -1 };
  loadFrames(path, version).then((file) => { view.file = file; view.shown = -1; player.show(); })
    .catch((error) => holder.replaceChildren(el("p", { class: "muted" }, `${ref}: no frames (${error})`)));
  view.show = (t) => {
    if (!view.file) return;
    const index = frameIndex(view.file.frames, t);
    const frame = view.file.frames[index];
    const blinking = Boolean(frame.state.blink && frame.state.cursor);
    const phase = blinking ? Math.floor(t / 0.4096) % 2 : 0;
    if (index === view.shown && phase === view.phase) return;
    view.shown = index;
    view.phase = phase;
    const draw = DRAWERS[view.file.kind];
    holder.replaceChildren(draw ? draw(view.file, frame, t) : el("pre", {}, frame.text));
    caption.replaceChildren(el("code", {}, ref), " ", el("code", { class: "screen-text" }, frame.text));
  };
  return view;
}

function renderPlayback(state, version, waves) {
  const section = $("playback-section");
  const tests = state.sim?.data?.tests || {};
  const names = [...waves.keys()].filter((n) => waves.get(n).axis === "time");
  if (names.length === 0) {
    section.hidden = true;
    player.playing = false;
    return;
  }
  section.hidden = false;
  if (!names.includes(player.test)) {
    player.test = names.find((n) => tests[n]?.firmware) ?? names[0];
    player.t = 0;
  }
  const data = waves.get(player.test);
  const xs = data.x;
  const end = xs[xs.length - 1];
  player.end = end;
  player.t = Math.min(player.t, end);

  const parts = state.check?.data?.parts || {};
  const leds = Object.entries(parts)
    .filter(([ref, p]) => p.type === "led" && data.signals[`current(${ref})`])
    .map(([ref, p]) => ({ ref, color: LED_COLORS[p.params?.color] ?? "#ffd60a" }));

  // Schematic with a glow on every LED.
  const kicad = state.render?.data?.kicad;
  const stage = el("div", { class: "stage" });
  if (kicad?.view && kicad.view_box && kicad.bodies) {
    const [vx, vy, vw, vh] = kicad.view_box;
    stage.style.maxWidth = `calc(78vh * ${(vw / vh).toFixed(4)})`; // the whole sheet fits the screen
    stage.append(el("img", { src: `/${kicad.view}?v=${version}`, alt: "KiCad schematic" }));
    for (const led of leds) {
      const box = kicad.bodies[led.ref];
      if (!box) continue;
      const [x0, y0, x1, y1] = box;
      const size = Math.max(x1 - x0, y1 - y0) * 2.4;
      led.glow = el("div", { class: "glow" });
      led.glow.style.cssText =
        `left:${((x0 + x1) / 2 - vx) / vw * 100}%;top:${((y0 + y1) / 2 - vy) / vh * 100}%;` +
        `width:${size / vw * 100}%;--led:${led.color}`;
      led.tag = el("div", { class: "glow-tag" });
      led.tag.style.cssText = `left:${((x0 + x1) / 2 - vx) / vw * 100}%;top:${(y1 - vy) / vh * 100}%`;
      stage.append(led.glow, led.tag);
    }
  } else {
    const why = kicad?.view ? "run eda render again to place the parts on it"
      : "it needs the KiCad schematic view (kicad-cli)";
    stage.append(el("p", { class: "muted" }, `No schematic to animate: ${why}.`));
  }

  // Controls.
  const select = el("select", {}, ...names.map((n) => {
    const option = el("option", { value: n }, n + (tests[n]?.firmware ? " (firmware)" : ""));
    option.selected = n === player.test;
    return option;
  }));
  select.addEventListener("change", () => {
    player.test = select.value;
    player.t = 0;
    renderPlayback(state, version, waves);
  });
  const play = el("button", { type: "button" });
  const restart = el("button", { type: "button", title: "back to 0" }, "⏮");
  const speed = el("select", {}, ...SPEEDS.map((v) => {
    const option = el("option", { value: v }, `${v}×`);
    option.selected = v === player.speed;
    return option;
  }));
  const slider = el("input", { type: "range", min: 0, max: 1000, step: 1, class: "scrub" });
  const clock = el("span", { class: "clock" });
  play.addEventListener("click", () => {
    if (!player.playing && player.t >= end) player.t = 0;
    player.playing = !player.playing;
    if (player.playing) startTicking();
    player.show();
  });
  restart.addEventListener("click", () => { player.t = 0; player.show(); });
  speed.addEventListener("change", () => { player.speed = Number(speed.value); });
  slider.addEventListener("input", () => { player.t = Number(slider.value) / 1000 * end; player.show(); });

  // Display parts of this test, following the replay.
  const screens = Object.entries(tests[player.test]?.displays ?? {})
    .map(([ref, d]) => screenView(ref, d.frames, version));

  // Readouts: LED currents, then every net voltage.
  const rows = [];
  const readouts = [];
  for (const led of leds) {
    const cell = el("td");
    rows.push(el("tr", {}, el("td", {}, el("code", {}, `current(${led.ref})`)), cell));
    readouts.push([cell, `current(${led.ref})`, "A"]);
  }
  for (const [signal, s] of Object.entries(data.signals)) {
    if (!signal.startsWith("voltage(")) continue;
    const cell = el("td");
    rows.push(el("tr", {}, el("td", {}, el("code", {}, signal)), cell));
    readouts.push([cell, signal, s.unit]);
  }

  player.show = () => {
    const t = player.t;
    play.textContent = player.playing ? "⏸ pause" : "▶ play";
    slider.value = String(Math.round(t / end * 1000));
    clock.textContent = `${fmt(t, "s")} / ${fmt(end, "s")}`;
    for (const led of leds) {
      const amps = sampleAt(xs, data.signals[`current(${led.ref})`].values, t);
      const level = amps <= LED_ON_A ? 0 : Math.min(1, amps / LED_FULL_A) ** 0.5;
      if (led.glow) led.glow.style.opacity = String(level);
      if (led.tag) led.tag.textContent = `${led.ref} ${level ? "ON" : "off"} · ${fmt(amps, "A")}`;
    }
    for (const [cell, signal, unit] of readouts) {
      cell.textContent = fmt(sampleAt(xs, data.signals[signal].values, t), unit);
    }
    for (const screen of screens) screen.show(t);
    for (const [name, place] of playheads) place(name === player.test ? t : null);
  };

  const controls = el("div", { class: "player" }, select, restart, play, speed, slider, clock);
  const side = el("table", { class: "readouts" }, el("tbody", {}, ...rows));
  section.querySelector("#playback").replaceChildren(
    controls,
    ...(screens.length ? [el("div", { class: "screens" }, ...screens.map((v) => v.node))] : []),
    el("div", { class: "player-body" }, stage, side),
    el("p", { class: "muted" },
      "Replays the simulated waveforms; LEDs glow with their current " +
      `(full at ${fmt(LED_FULL_A, "A")}). The orange line on the waveforms follows the replay.`));
  player.show();
  if (player.playing) startTicking();
}

function startTicking() {
  if (player.running) return; // one animation loop at a time
  player.running = true;
  player.last = null;
  requestAnimationFrame(tick);
}

function tick(now) {
  if (player.playing && player.last !== null) {
    player.t += (now - player.last) / 1000 * player.speed;
    if (player.t >= player.end) {
      player.t = player.end;
      player.playing = false;
    }
  }
  player.last = now;
  player.show();
  if (player.playing) requestAnimationFrame(tick);
  else player.running = false;
}

// --- refresh loop ---------------------------------------------------------------------

async function refresh() {
  const response = await fetch("/api/state", { cache: "no-store" });
  const state = await response.json();
  $("project").textContent = state.project;
  document.title = `${state.project} · eda`;
  renderStatus(state);
  renderIssues(state);
  renderSchematic(state, state.version);
  renderTests(state);
  renderFirmware(state);
  renderBoard(state, state.version);
  renderLog(state);
  const waves = await renderWaveforms(state, state.version);
  renderPlayback(state, state.version, waves);
  $("updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
}

function connect() {
  const events = new EventSource("/events");
  events.addEventListener("update", () => refresh().catch(console.error));
  events.onopen = () => { $("connection").textContent = "live"; };
  events.onerror = () => { $("connection").textContent = "reconnecting…"; };
}

refresh().catch(console.error);
// `?snapshot` skips the live connection so headless screenshots can finish loading.
if (!new URLSearchParams(location.search).has("snapshot")) connect();
