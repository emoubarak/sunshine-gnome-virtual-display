// Shared by the animated scenes: palette (the one of sunshine/make-icons), SVG helpers, timing.
const NS = 'http://www.w3.org/2000/svg';
const C = {
  bg: '#0e151c', text: '#ecf1f5', muted: '#8c9ba8', line: '#2a3846',
  accent: '#4dd1c5', accentDim: '#26605c', accentFill: '#163e3c',
  frame: '#8c9ba8', frameFill: '#18222c', wall: '#263440', bar: '#080c11',
  amber: '#f0b35b', winBody: '#0f161d', winTitle: '#3a4a58', off: '#141c24',
};
const FONT = "'Noto Sans', 'Segoe UI', Helvetica, Arial, sans-serif";
const MONO = "'Source Code Pro', 'DejaVu Sans Mono', monospace";

function set(e, attrs) {
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'text') e.textContent = v;
    else e.setAttribute(k, v);
  }
  return e;
}
function el(parent, tag, attrs = {}) {
  const e = document.createElementNS(NS, tag);
  set(e, attrs);
  if (parent) parent.appendChild(e);
  return e;
}
function text(parent, x, y, s, attrs = {}) {
  return el(parent, 'text', {x, y, fill: C.text, 'font-family': FONT, 'font-size': 16, text: s, ...attrs});
}

const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
const ease = x => (x < 0.5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2);
// 0 before a, 1 after b, eased in between.
const prog = (t, a, b) => ease(clamp((t - a) / (b - a)));
const lerp = (a, b, k) => a + (b - a) * k;
// Rises over a..b, stays at 1, falls over c..d.
const fade = (t, a, b, c, d) => Math.min(prog(t, a, b), 1 - prog(t, c, d));

function svgRoot(width, height) {
  const svg = el(null, 'svg', {width, height, viewBox: `0 0 ${width} ${height}`});
  document.body.appendChild(svg);
  el(svg, 'rect', {width, height, fill: C.bg});
  return svg;
}

// Numbered caption at the top; steps: [[start, end, text], ...] (seconds).
function captions(svg, steps, duration) {
  const items = steps.map(([a, b, s], i) => {
    const g = el(svg, 'g', {opacity: 0});
    el(g, 'circle', {cx: 44, cy: 38, r: 13, fill: C.accent});
    text(g, 44, 43.5, String(i + 1), {'text-anchor': 'middle', fill: C.bg, 'font-weight': 700, 'font-size': 15});
    text(g, 68, 45, s, {'font-size': 21, 'font-weight': 600});
    return {g, a, b};
  });
  const track = el(svg, 'rect', {x: 32, y: 430, width: 736, height: 3, rx: 1.5, fill: C.line});
  const bar = el(svg, 'rect', {x: 32, y: 430, width: 0, height: 3, rx: 1.5, fill: C.accent});
  // The first caption is already shown at t = 0: it fades in at the end of the loop.
  return t => {
    items.forEach(({g, a, b}, i) => set(g, {opacity: i === 0
      ? Math.max(1 - prog(t, b - 0.25, b), prog(t, duration - 0.3, duration))
      : fade(t, a, a + 0.25, b - 0.25, b)}));
    set(bar, {width: 736 * clamp(t / duration)});
  };
}

// A monitor: frame and inner screen. Returns the screen rectangle and the groups to draw in.
function monitor(parent, {x, y, w, h, bezel = 6, virtual = false, stand = false}) {
  const g = el(parent, 'g');
  const s = {x: x + bezel, y: y + bezel, w: w - 2 * bezel, h: h - 2 * bezel};
  if (stand) {
    el(g, 'rect', {x: x + w / 2 - 9, y: y + h, width: 18, height: 11, fill: C.frame});
    el(g, 'rect', {x: x + w / 2 - 36, y: y + h + 10, width: 72, height: 6, rx: 3, fill: C.frame});
  }
  if (virtual) {
    el(g, 'rect', {x: x + 1, y: y + 1, width: w - 2, height: h - 2, rx: 6, fill: C.bg,
      stroke: C.accent, 'stroke-width': 2.5, 'stroke-dasharray': '8 6'});
  } else {
    el(g, 'rect', {x, y, width: w, height: h, rx: 7, fill: C.frameFill, stroke: C.frame, 'stroke-width': 2.5});
  }
  const screen = el(g, 'rect', {x: s.x, y: s.y, width: s.w, height: s.h, rx: 2,
    fill: virtual ? C.accentDim : C.wall});
  return {g, s, screen};
}

// GNOME's top bar (on the primary monitor) with its clock.
function topBar(parent, s) {
  const g = el(parent, 'g');
  el(g, 'rect', {x: s.x, y: s.y, width: s.w, height: 9, fill: C.bar});
  el(g, 'rect', {x: s.x + s.w / 2 - 11, y: s.y + 3, width: 22, height: 3, rx: 1.5, fill: C.muted});
  el(g, 'rect', {x: s.x + 5, y: s.y + 3, width: 12, height: 3, rx: 1.5, fill: C.muted});
  return g;
}

// A terminal-like window; place it with translate().
function windowShape(parent, w, h) {
  const g = el(parent, 'g');
  el(g, 'rect', {width: w, height: h, rx: 4, fill: C.winBody, stroke: C.muted, 'stroke-width': 1.5});
  el(g, 'rect', {width: w, height: 13, rx: 4, fill: C.winTitle});
  el(g, 'rect', {y: 9, width: w, height: 4, fill: C.winTitle});
  el(g, 'circle', {cx: w - 8, cy: 6.5, r: 2.5, fill: C.muted});
  const lines = [[0.55, C.accent], [0.75, C.muted], [0.4, C.muted], [0.62, C.accent], [0.3, C.muted]];
  lines.forEach(([k, c], i) => {
    if (21 + i * 10 < h - 6) el(g, 'rect', {x: 8, y: 21 + i * 10, width: (w - 16) * k, height: 4, rx: 2, fill: c});
  });
  return g;
}

function pointer(parent) {
  return el(parent, 'path', {d: 'M0 0 L0 17 L4.5 12.8 L7.6 19.6 L10.6 18.3 L7.6 11.6 L13.4 11.4 Z',
    fill: '#ffffff', stroke: '#000000', 'stroke-width': 1.3, 'stroke-linejoin': 'round'});
}

// Moonlight's app list with our three entries, drawn in screen rectangle s.
function moonlightGrid(parent, s) {
  const g = el(parent, 'g');
  el(g, 'rect', {x: s.x, y: s.y, width: s.w, height: s.h, fill: '#111922'});
  const size = s.w * 0.23, gap = s.w * 0.07;
  const x0 = s.x + (s.w - 3 * size - 2 * gap) / 2, y0 = s.y + s.h * 0.2;
  const names = ['Extend', 'Virtual only', 'Remote'];
  const tiles = names.map((name, i) => {
    const x = x0 + i * (size + gap);
    const tile = el(g, 'g');
    const ring = el(tile, 'rect', {x: x - 3, y: y0 - 3, width: size + 6, height: size + 6, rx: 7,
      fill: 'none', stroke: C.accent, 'stroke-width': 2.5, opacity: 0});
    el(tile, 'rect', {x, y: y0, width: size, height: size, rx: 5, fill: '#0b1117', stroke: C.line});
    const u = size / 10;
    if (i === 0) {
      el(tile, 'rect', {x: x + 1.4 * u, y: y0 + 2.8 * u, width: 2.6 * u, height: 4.4 * u, rx: 0.5 * u,
        fill: 'none', stroke: C.muted, 'stroke-width': 1.3});
      el(tile, 'rect', {x: x + 4.8 * u, y: y0 + 3.6 * u, width: 4 * u, height: 2.8 * u, rx: 0.5 * u,
        fill: C.accentDim, stroke: C.accent, 'stroke-width': 1.5});
    } else if (i === 1) {
      el(tile, 'rect', {x: x + 2.4 * u, y: y0 + 3.2 * u, width: 5.2 * u, height: 3.6 * u, rx: 0.5 * u,
        fill: C.accentDim, stroke: C.accent, 'stroke-width': 1.5});
    } else {
      el(tile, 'rect', {x: x + 1.6 * u, y: y0 + 3.6 * u, width: 4.4 * u, height: 3 * u, rx: 0.5 * u,
        fill: C.accentDim, stroke: C.accent, 'stroke-width': 1.5});
      for (const r of [1.4, 2.4]) {
        el(tile, 'path', {d: `M${x + 7.2 * u + r * u * 0.5} ${y0 + 3.4 * u - r * u * 0.85} A${r * u} ${r * u} 0 0 1 ${x + 7.2 * u + r * u * 0.5} ${y0 + 3.4 * u + r * u * 0.85}`,
          fill: 'none', stroke: C.muted, 'stroke-width': 1.2});
      }
    }
    text(tile, x + size / 2, y0 + size * 1.32, name, {'text-anchor': 'middle', 'font-size': Math.min(10.5, size * 0.24), fill: C.muted});
    return {tile, ring, cx: x + size / 2, cy: y0 + size / 2};
  });
  return {g, tiles};
}
