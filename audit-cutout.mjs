import {readFile, realpath} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const [specFile, requestedFps, requestedMaxFrames, requestedPopRatio] = process.argv.slice(2);
const here = dirname(fileURLToPath(import.meta.url));
const deps = process.env.REFERENCE_BROWSER_DEPS;
const require = createRequire(deps ? join(resolve(deps), '_cutout_resolver.cjs') : import.meta.url);
let chromium;
try { ({chromium} = require('playwright-core')); }
catch { throw Error('Run npm install in the plugin directory to install Playwright'); }

const fps = Number(requestedFps);
const maxFrames = Number(requestedMaxFrames);
const popRatio = Number(requestedPopRatio);
if (!Number.isFinite(fps) || fps < 1 || fps > 60) throw Error('Audit fps must be 1-60');
if (!Number.isInteger(maxFrames) || maxFrames < 2 || maxFrames > 1200) throw Error('Audit maxFrames must be 2-1200');
if (!Number.isFinite(popRatio) || popRatio < 1.5 || popRatio > 20) throw Error('Audit popRatio must be 1.5-20');

const spec = JSON.parse(await readFile(specFile, 'utf8'));
const assets = new Map();
const add = (name, file, type) => assets.set('/' + name, {path:file, contentType:type});
add('scene.json', specFile, 'application/json');
for (const name of ['cutout-scene.mjs', 'cutout-timeline.mjs', 'cutout-mesh.mjs', 'cutout-shadows.mjs', 'cutout-seams.mjs'])
  add(name, join(here, name), 'text/javascript');
const installedSpec = await realpath(specFile);
for (const name of [spec.background, spec.backing, ...spec.layers.map(layer => layer.file), ...(spec.seams ?? []).map(seam => seam.file)].filter(Boolean)) {
  if (!/^[A-Za-z0-9_-]+\.png$/.test(name)) throw Error('Invalid scene PNG filename');
  const file = await realpath(join(dirname(installedSpec), name));
  if (dirname(file) !== dirname(installedSpec)) throw Error('Scene image leaves its bundle');
  add(name, file, 'image/png');
}

function eventTimes() {
  const events = new Map();
  const addEvent = (time, reason) => {
    if (!Number.isFinite(time) || time < 0 || time > spec.duration) return;
    const key = Number(time.toFixed(6));
    if (!events.has(key)) events.set(key, new Set());
    events.get(key).add(reason);
  };
  addEvent(0, 'scene:start');
  addEvent(spec.duration, 'scene:end');
  for (const node of [...spec.groups, ...spec.layers]) {
    for (const pose of node.motion ?? []) addEvent(pose.time, `motion:${node.key}`);
  }
  for (const layer of spec.layers) {
    for (const key of layer.depth ?? []) addEvent(key.time, `depth:${layer.key}`);
    for (const key of layer.appearance?.light ?? []) addEvent(key.time, `light:${layer.key}`);
  }
  for (const [kind, rows] of [['seam', spec.seams ?? []], ['matte', spec.mattes ?? []]]) {
    for (const row of rows) for (const [start, end] of row.windows ?? []) {
      const label = row.key ?? `${row.target}:${row.mask}`;
      addEvent(start, `${kind}:start:${label}`);
      addEvent(end, `${kind}:end:${label}`);
      addEvent(Math.max(start, end - 1 / fps), `${kind}:inside:${label}`);
    }
  }
  return [...events.entries()].sort((a, b) => a[0] - b[0]).map(([time, reasons]) => ({
    time, reasons:[...reasons].sort(),
  }));
}

const host = 'http://cutout.invalid';
const browser = await chromium.launch({
  executablePath:process.env.REFERENCE_BROWSER_EXECUTABLE || undefined,
  headless:true,
  args:['--enable-unsafe-swiftshader'],
});
try {
  const page = await browser.newPage();
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== host) return route.abort();
    if (url.pathname === '/') return route.fulfill({
      contentType:'text/html', body:'<!doctype html><canvas id="scene"></canvas><canvas id="audit"></canvas>',
    });
    const asset = assets.get(url.pathname);
    return asset ? route.fulfill(asset) : route.abort();
  });
  await page.goto(host);
  await page.evaluate(async () => {
    const {loadCutoutScene} = await import('/cutout-scene.mjs');
    const canvas = document.querySelector('#scene');
    const scene = await loadCutoutScene(canvas, '/scene.json');
    const audit = document.querySelector('#audit');
    const width = Math.min(320, canvas.width);
    audit.width = width;
    audit.height = Math.max(1, Math.round(canvas.height * width / canvas.width));
    const context = audit.getContext('2d', {willReadFrequently:true});
    window.auditScene = {
      sample(time) {
        scene.seek(time);
        context.clearRect(0, 0, audit.width, audit.height);
        context.drawImage(canvas, 0, 0, audit.width, audit.height);
        return {
          pixels:Array.from(context.getImageData(0, 0, audit.width, audit.height).data),
          seams:scene.seamStatus(),
        };
      },
      event(time) {
        scene.seek(time);
        return {coverage:scene.coverage(time), seams:scene.seamStatus()};
      },
      size:[audit.width, audit.height],
    };
  });

  const nominalFrames = Math.max(2, Math.ceil(spec.duration * fps) + 1);
  const frames = Math.min(maxFrames, nominalFrames);
  const effectiveFps = (frames - 1) / spec.duration;
  const differences = [];
  const alphaRatios = [];
  const seamReport = new Map();
  let previous = null;
  for (let i = 0; i < frames; i++) {
    const time = i * spec.duration / (frames - 1);
    const sample = await page.evaluate(time => window.auditScene.sample(time), time);
    const pixels = sample.pixels;
    let translucent = 0;
    for (let p = 3; p < pixels.length; p += 4) if (pixels[p] < 255) translucent++;
    alphaRatios.push(translucent / (pixels.length / 4));
    if (previous) {
      let total = 0;
      for (let p = 0; p < pixels.length; p++) total += Math.abs(pixels[p] - previous[p]);
      differences.push({time, mean_absolute_delta:total / pixels.length});
    }
    previous = pixels;
    for (const seam of sample.seams) {
      const row = seamReport.get(seam.key) ?? {key:seam.key, max_span:0, counts:{}};
      row.max_span = Math.max(row.max_span, seam.span ?? 0);
      row.counts[seam.status] = (row.counts[seam.status] ?? 0) + 1;
      if (seam.crossings) row.crossing_samples = (row.crossing_samples ?? 0) + 1;
      if (seam.status === 'too_wide' && row.first_problem_at === undefined) row.first_problem_at = time;
      seamReport.set(seam.key, row);
    }
  }

  const popCandidates = [];
  for (let i = 1; i < differences.length - 1; i++) {
    const neighbour = (differences[i - 1].mean_absolute_delta + differences[i + 1].mean_absolute_delta) / 2;
    const ratio = differences[i].mean_absolute_delta / Math.max(neighbour, 1e-6);
    if (ratio >= popRatio && differences[i].mean_absolute_delta >= 0.25) {
      popCandidates.push({
        kind:'isolated_delta',
        time:Number(differences[i].time.toFixed(6)),
        mean_absolute_delta:Number(differences[i].mean_absolute_delta.toFixed(6)),
        neighbour_ratio:Number(ratio.toFixed(3)),
      });
    }
  }
  // A one-frame wrong pose often produces two adjacent deltas: entering the bad
  // frame and returning immediately. Compare that pair with its outer neighbours.
  for (let i = 1; i < differences.length - 2; i++) {
    const outer = (differences[i - 1].mean_absolute_delta + differences[i + 2].mean_absolute_delta) / 2;
    const pair = Math.min(differences[i].mean_absolute_delta, differences[i + 1].mean_absolute_delta);
    const ratio = pair / Math.max(outer, 1e-6);
    if (ratio >= popRatio && pair >= 0.25) {
      popCandidates.push({
        kind:'return_pair',
        time:Number(differences[i].time.toFixed(6)),
        end_time:Number(differences[i + 1].time.toFixed(6)),
        mean_absolute_delta:Number(pair.toFixed(6)),
        neighbour_ratio:Number(ratio.toFixed(3)),
      });
    }
  }

  const events = [];
  for (const event of eventTimes()) {
    const result = await page.evaluate(time => window.auditScene.event(time), event.time);
    events.push({...event, ...result});
  }
  const maxBy = (rows, field) => rows.reduce((best, row) =>
    !best || row[field] > best[field] ? row : best, null);
  const maxExposed = maxBy(events.map(row => ({time:row.time, pixels:row.coverage.exposed})), 'pixels');
  const maxUnfilled = maxBy(events.map(row => ({time:row.time, pixels:row.coverage.unfilled})), 'pixels');
  const maxAlpha = Math.max(...alphaRatios);
  const warnings = [];
  if (maxUnfilled?.pixels) warnings.push('unfilled_pixels_at_key_event');
  if ([...seamReport.values()].some(row => row.counts.too_wide)) warnings.push('seam_span_exceeded');
  if (popCandidates.length) warnings.push('single_frame_jump_candidates');
  if (maxAlpha > 0 && !spec.backing) warnings.push('transparent_output_without_backing');

  console.log(JSON.stringify({
    scene:specFile,
    duration:spec.duration,
    requested_fps:fps,
    effective_fps:effectiveFps,
    frames_sampled:frames,
    downsample_size:await page.evaluate(() => window.auditScene.size),
    max_translucent_ratio:Number(maxAlpha.toFixed(8)),
    max_exposed:maxExposed,
    max_unfilled:maxUnfilled,
    seam_report:[...seamReport.values()],
    pop_candidates:popCandidates,
    events,
    warnings,
  }));
} finally {
  await browser.close();
}
