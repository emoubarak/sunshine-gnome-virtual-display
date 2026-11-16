// Renders an animated scene (an HTML page that defines SCENE and render(t)) into a GIF.
//
//   node docs/images/src/render.mjs docs/images/src/extend.html docs/images/extend.gif
//   node docs/images/src/render.mjs scene.html out.png 3.5    # one frame, at t = 3.5 s
//
// Needs Node 22+ (built-in WebSocket), Google Chrome or Chromium, ffmpeg. Chrome runs headless
// with its own throwaway profile; FRAMES=DIR keeps the PNG frames there.
import {spawn, execFileSync} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const [scenePath, out, at] = process.argv.slice(2);
if (!scenePath || !out) {
  console.error('usage: render.mjs SCENE.html OUT.gif|OUT.png [SECONDS]');
  process.exit(2);
}
const work = fs.mkdtempSync(path.join(os.tmpdir(), 'gvd-render-'));
const frames = process.env.FRAMES || path.join(work, 'frames');
fs.mkdirSync(frames, {recursive: true});
for (const f of fs.readdirSync(frames)) if (f.endsWith('.png')) fs.rmSync(path.join(frames, f));

const chromeBin = process.env.CHROME ||
  ['google-chrome', 'chromium', 'chromium-browser'].find(b => {
    try { execFileSync('which', [b], {stdio: 'ignore'}); return true; } catch { return false; }
  });
const chrome = spawn(chromeBin, [
  '--headless=new', '--remote-debugging-port=0', `--user-data-dir=${path.join(work, 'profile')}`,
  '--no-first-run', '--no-default-browser-check', '--hide-scrollbars', '--mute-audio',
  '--force-device-scale-factor=1', '--font-render-hinting=none', 'about:blank',
], {stdio: ['ignore', 'ignore', 'pipe']});

const exited = new Promise(r => chrome.on('exit', r));
async function cleanup() {
  try { chrome.kill('SIGTERM'); } catch {}
  await Promise.race([exited, new Promise(r => setTimeout(r, 5000))]);
  fs.rmSync(work, {recursive: true, force: true});
}

const wsUrl = await new Promise((resolve, reject) => {
  let buf = '';
  chrome.stderr.on('data', d => {
    buf += d;
    const m = buf.match(/DevTools listening on (ws:\/\/\S+)/);
    if (m) resolve(m[1]);
  });
  chrome.on('exit', code => reject(new Error(`chrome exited (${code}): ${buf}`)));
  setTimeout(() => reject(new Error('chrome did not start')), 20000);
});
const port = new URL(wsUrl).port;
const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r, {once: true}));

let nextId = 0;
const waiting = new Map();
ws.addEventListener('message', e => {
  const msg = JSON.parse(e.data);
  if (msg.id && waiting.has(msg.id)) {
    const {resolve, reject} = waiting.get(msg.id);
    waiting.delete(msg.id);
    msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
  }
});
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++nextId;
  waiting.set(id, {resolve, reject});
  ws.send(JSON.stringify({id, method, params}));
});
const evaluate = async expression => {
  const r = await send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || expression);
  return r.result.value;
};

try {
  await send('Page.enable');
  const loaded = new Promise(r => ws.addEventListener('message', e => {
    if (JSON.parse(e.data).method === 'Page.loadEventFired') r();
  }));
  await send('Page.navigate', {url: pathToFileURL(path.resolve(scenePath)).href});
  await loaded;
  const scene = await evaluate('document.fonts.ready.then(() => SCENE)');
  await send('Emulation.setDeviceMetricsOverride',
    {width: scene.width, height: scene.height, deviceScaleFactor: 1, mobile: false});
  const shoot = async (t, file) => {
    await evaluate(`render(${t}); new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))`);
    const {data} = await send('Page.captureScreenshot', {format: 'png'});
    fs.writeFileSync(file, Buffer.from(data, 'base64'));
  };
  if (out.endsWith('.png')) {
    await shoot(Number(at || 0), out);
  } else {
    const n = Math.round(scene.duration * scene.fps);
    for (let i = 0; i < n; i++) {
      await shoot(i / scene.fps, path.join(frames, `${String(i).padStart(4, '0')}.png`));
    }
    // One palette for the whole loop; flat colors, so no dithering.
    execFileSync('ffmpeg', ['-v', 'error', '-y', '-framerate', String(scene.fps),
      '-i', path.join(frames, '%04d.png'), '-vf',
      'split[a][b];[a]palettegen=max_colors=192:stats_mode=full[p];[b][p]paletteuse=dither=none',
      '-loop', '0', out], {stdio: 'inherit'});
    console.log(`${out}: ${n} frames, ${(fs.statSync(out).size / 1024).toFixed(0)} KiB`);
  }
} finally {
  ws.close();
  await cleanup();
}
