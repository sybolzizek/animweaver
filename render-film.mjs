import {readFile, realpath, writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const [jobFile, out] = process.argv.slice(2);
const here = dirname(fileURLToPath(import.meta.url));
const deps = process.env.REFERENCE_BROWSER_DEPS;
const require = createRequire(deps ? join(resolve(deps), '_cutout_resolver.cjs') : import.meta.url);
let chromium;
try { ({chromium} = require('playwright-core')); }
catch { throw Error('Run npm install in the plugin directory to install Playwright'); }

const job = JSON.parse(await readFile(jobFile, 'utf8'));
if (!Number.isInteger(job.fps) || job.fps < 1 || job.fps > 120) throw Error('Film fps must be 1-120');
if (!Number.isInteger(job.frames) || job.frames < 1) throw Error('Film must contain frames');
if (!Number.isInteger(job.width) || !Number.isInteger(job.height) || job.width < 1 || job.height < 1)
  throw Error('Film dimensions are invalid');
if (!Array.isArray(job.shots) || !job.shots.length) throw Error('Film has no shots');

const browser = await chromium.launch({
  executablePath:process.env.REFERENCE_BROWSER_EXECUTABLE || undefined,
  headless:true,
  args:['--enable-unsafe-swiftshader'],
});

const reports = [];
try {
  for (let si = 0; si < job.shots.length; si++) {
    const shot = job.shots[si];
    if (!Number.isInteger(shot.frames) || shot.frames < 1 || !Number.isInteger(shot.start_frame))
      throw Error(`Shot ${shot.key} has invalid frame bounds`);
    const specFile = await realpath(shot.spec_file);
    const bundle = dirname(specFile);
    const spec = JSON.parse(await readFile(specFile, 'utf8'));
    if (spec.width !== job.width || spec.height !== job.height)
      throw Error(`Shot ${shot.key} canvas differs from its film`);
    const end = Number(shot.source_start) + shot.frames / job.fps;
    if (!Number.isFinite(shot.source_start) || shot.source_start < 0 || end > spec.duration + 1e-9)
      throw Error(`Shot ${shot.key} source range is invalid`);

    const prefix = `shot-${si}`;
    const assets = new Map();
    const add = (name, file, type) => assets.set(`/${prefix}/${name}`, {path:file, contentType:type});
    add('scene.json', specFile, 'application/json');
    for (const name of ['cutout-scene.mjs', 'cutout-timeline.mjs', 'scene-evaluate.mjs', 'cutout-mesh.mjs', 'cutout-shadows.mjs', 'cutout-seams.mjs'])
      add(name, join(here, name), 'text/javascript');
    for (const name of [spec.background, spec.backing, ...spec.layers.map(layer => layer.file), ...(spec.seams ?? []).map(seam => seam.file)].filter(Boolean)) {
      if (!/^[A-Za-z0-9_-]+\.png$/.test(name)) throw Error(`Shot ${shot.key} has an invalid PNG filename`);
      const file = await realpath(join(bundle, name));
      if (dirname(file) !== bundle) throw Error(`Shot ${shot.key} image leaves its bundle`);
      add(name, file, 'image/png');
    }

    const host = 'http://cutout.invalid';
    const page = await browser.newPage();
    try {
      await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.origin !== host) return route.abort();
        if (url.pathname === `/${prefix}/`) return route.fulfill({
          contentType:'text/html', body:'<!doctype html><canvas></canvas>',
        });
        const asset = assets.get(url.pathname);
        return asset ? route.fulfill(asset) : route.abort();
      });
      await page.goto(`${host}/${prefix}/`);
      await page.evaluate(async prefix_ => {
        const {loadCutoutScene} = await import(`/${prefix_}/cutout-scene.mjs`);
        const canvas = document.querySelector('canvas');
        const scene = await loadCutoutScene(canvas, `/${prefix_}/scene.json`);
        window.renderFilmFrame = time => {
          scene.seek(time);
          const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
          let empty = 0;
          for (let i = 3; i < pixels.length; i += 4) if (pixels[i] < 255) empty++;
          if (empty) throw Error(`Frame at ${time.toFixed(3)}s has ${empty} non-opaque pixels. Add backing before film export.`);
          return {data:canvas.toDataURL().split(',')[1], seams:scene.seamStatus()};
        };
      }, prefix);

      const seamReport = new Map();
      for (let i = 0; i < shot.frames; i++) {
        const time = Number(shot.source_start) + i / job.fps;
        const {data, seams} = await page.evaluate(t => window.renderFilmFrame(t), time);
        for (const seam of seams) {
          const row = seamReport.get(seam.key) ?? {key:seam.key, max_span:0, counts:{}};
          row.max_span = Math.max(row.max_span, seam.span ?? 0);
          row.counts[seam.status] = (row.counts[seam.status] ?? 0) + 1;
          if (seam.crossings) row.crossing_frames = (row.crossing_frames ?? 0) + 1;
          if (seam.status === 'too_wide' && row.first_problem_frame === undefined)
            row.first_problem_frame = i;
          seamReport.set(seam.key, row);
        }
        const frame = shot.start_frame + i;
        await writeFile(join(out, `frame-${String(frame).padStart(6, '0')}.png`), Buffer.from(data, 'base64'), {flag:'wx'});
      }
      reports.push({
        key:shot.key,
        scene:shot.scene,
        start_frame:shot.start_frame,
        frames:shot.frames,
        source_start:shot.source_start,
        seam_report:[...seamReport.values()],
      });
    } finally {
      await page.close();
    }
  }
} finally {
  await browser.close();
}

const written = reports.reduce((sum, shot) => sum + shot.frames, 0);
if (written !== job.frames) throw Error(`Film rendered ${written} frames instead of ${job.frames}`);
console.log(JSON.stringify({
  frames:job.frames,
  fps:job.fps,
  duration:job.frames / job.fps,
  shots:reports,
}));
