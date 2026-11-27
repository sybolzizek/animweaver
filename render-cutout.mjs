import { readFile, realpath, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const [specFile, out, format, time, fps, backend = 'canvas2d'] = process.argv.slice(2);
if (!['canvas2d', 'three'].includes(backend)) throw Error('Unknown scene render backend');
const here = dirname(fileURLToPath(import.meta.url));
const deps = process.env.REFERENCE_BROWSER_DEPS;
const require = createRequire(deps ? join(resolve(deps), '_cutout_resolver.cjs') : import.meta.url);
let chromium;
try { ({chromium} = require('playwright-core')); }
catch { throw Error('Run npm install in the plugin directory to install Playwright'); }
const spec = JSON.parse(await readFile(specFile, 'utf8'));
const assets = new Map();
const add = (name, file, type) => assets.set('/'+name, {path: file, contentType: type});
add('scene.json', specFile, 'application/json');
for (const name of ['cutout-scene.mjs', 'cutout-timeline.mjs', 'scene-evaluate.mjs', 'cutout-mesh.mjs', 'cutout-shadows.mjs', 'cutout-seams.mjs']) add(name, join(here, name), 'text/javascript');
if (backend === 'three') {
  const threeBuild = dirname(require.resolve('three'));
  add('three-scene.mjs', join(here, 'three-scene.mjs'), 'text/javascript');
  add('three.module.js', join(threeBuild, 'three.module.js'), 'text/javascript');
  add('three.core.js', join(threeBuild, 'three.core.js'), 'text/javascript');
}
const installedSpec = await realpath(specFile);
for (const name of [spec.background, spec.backing, ...spec.layers.map(layer => layer.file), ...(spec.seams ?? []).map(seam => seam.file)].filter(Boolean)) {
  if (!/^[A-Za-z0-9_-]+\.png$/.test(name)) throw Error('Invalid scene PNG filename');
  const file = await realpath(join(dirname(installedSpec), name));
  if (dirname(file) !== dirname(installedSpec)) throw Error('Scene image leaves its bundle');
  add(name, file, 'image/png');
}
if (backend === 'three') {
  const materialStore = dirname(dirname(installedSpec));
  const visited = new Set();
  async function addScene(id, stack = []) {
    if (!/^[0-9a-f]{24}$/.test(id)) throw Error('Invalid scene source ID');
    if (stack.includes(id)) throw Error(`Cyclic scene source dependency at ${id}`);
    if (visited.has(id)) return;
    const directory = await realpath(join(materialStore, id));
    if (dirname(directory) !== materialStore) throw Error('Scene source leaves the material store');
    const record = JSON.parse(await readFile(join(directory, 'record.json'), 'utf8'));
    if (record.kind !== 'cutout_scene') throw Error('Scene source is not an editable cutout scene');
    const childSpecFile = await realpath(join(directory, 'scene.json'));
    const childSpec = JSON.parse(await readFile(childSpecFile, 'utf8'));
    add(`scenes/${id}/scene.json`, childSpecFile, 'application/json');
    for (const name of [childSpec.background, childSpec.backing,
      ...childSpec.layers.map(layer => layer.file),
      ...(childSpec.seams ?? []).map(seam => seam.file)].filter(Boolean)) {
      if (!/^[A-Za-z0-9_-]+\.png$/.test(name)) throw Error('Invalid scene source PNG filename');
      const file = await realpath(join(directory, name));
      if (dirname(file) !== directory) throw Error('Scene source image leaves its bundle');
      add(`scenes/${id}/${name}`, file, 'image/png');
    }
    visited.add(id);
    for (const layer of childSpec.layers)
      if (layer.scene_id) await addScene(layer.scene_id, [...stack, id]);
  }
  for (const layer of spec.layers) if (layer.scene_id) await addScene(layer.scene_id);
}
const host = 'http://cutout.invalid';
const browser = await chromium.launch({executablePath:process.env.REFERENCE_BROWSER_EXECUTABLE || undefined,
  headless:true,args:['--enable-unsafe-swiftshader']});
try {
  const page = await browser.newPage();
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== host) return route.abort();
    if (url.pathname === '/') return route.fulfill({contentType:'text/html',body:'<!doctype html><canvas></canvas>'});
    const asset = assets.get(url.pathname);
    return asset ? route.fulfill(asset) : route.abort();
  });
  await page.goto(host);
  await page.evaluate(async backend => {
    const load = backend === 'three'
      ? (await import('/three-scene.mjs')).loadThreeScene
      : (await import('/cutout-scene.mjs')).loadCutoutScene;
    const canvas = document.querySelector('canvas');
    const scene = await load(canvas, '/scene.json');
    const inspect = document.createElement('canvas');
    inspect.width = canvas.width;
    inspect.height = canvas.height;
    const inspectContext = inspect.getContext('2d', {willReadFrequently:true});
    window.renderFrame = (time, video) => {
      scene.seek(time);
      if (video) {
        inspectContext.clearRect(0, 0, inspect.width, inspect.height);
        inspectContext.drawImage(canvas, 0, 0);
        const pixels = inspectContext.getImageData(0,0,canvas.width,canvas.height).data;
        let empty = 0;
        for (let i=3; i<pixels.length; i+=4) if (pixels[i] < 255) empty++;
        if (empty) throw Error(`Frame at ${time.toFixed(3)}s has ${empty} non-opaque pixels. Add backing before MP4 export.`);
      }
      return {data:canvas.toDataURL().split(',')[1], seams:scene.seamStatus()};
    };
  }, backend);
  const video = format === '.mp4';
  const frames = video ? Math.ceil(spec.duration * Number(fps)) : 1;
  const seamReport = new Map();
  let frameSeams = [];
  for (let i=0; i<frames; i++) {
    const seconds = video ? i / Number(fps) : Number(time);
    const {data, seams} = await page.evaluate(([t, video]) => window.renderFrame(t, video), [seconds, video]);
    frameSeams = seams;
    for (const seam of seams) {
      const row = seamReport.get(seam.key) ?? {key:seam.key, max_span:0, counts:{}};
      row.max_span = Math.max(row.max_span, seam.span ?? 0);
      row.counts[seam.status] = (row.counts[seam.status] ?? 0) + 1;
      if (seam.crossings) row.crossing_frames = (row.crossing_frames ?? 0) + 1;
      if (seam.status === 'too_wide' && row.first_problem_at === undefined)
        row.first_problem_at = seconds;
      seamReport.set(seam.key, row);
    }
    await writeFile(join(out, `frame-${String(i).padStart(6,'0')}.png`), Buffer.from(data,'base64'));
  }
  const result = video ? {frames, fps:Number(fps), duration:frames/Number(fps)}
    : {time:Number(time), frames:1};
  if (seamReport.size) result.seam_report = video ? [...seamReport.values()] : frameSeams;
  console.log(JSON.stringify(result));
} finally { await browser.close(); }
