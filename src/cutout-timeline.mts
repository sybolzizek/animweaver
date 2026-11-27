export type Point = readonly [number, number];
export type Timed = {time: number};
export type Ease = 'linear' | 'smooth' | 'ease_in' | 'ease_out' | 'hold';
export type Pose = {move: Point; rotate: number; scale: Point; ease?: Ease};
export type LightKey = Timed & {exposure: number; gradient: number};
export type DepthKey = Timed & {z: number};
export type Window = readonly [number, number];
export type ScalarKey = Timed & {value: number; ease?: Ease};
export type LocalTime = {
  start?: number; offset?: number; scale?: number; duration?: number | null;
  mode?: 'free' | 'clamp' | 'repeat' | 'pingpong';
};
export type CameraKey = Timed & {
  move: Point; truck: Point; dolly: number; zoom: number; rotate: number; ease?: Ease;
};
export type CameraShake = Timed & {
  duration: number; amplitude: Point; rotate: number; frequency: number; decay: number; seed: number;
};
export type CameraTrack = {
  pivot?: Point | null; focal?: number | null; motion?: CameraKey[]; shakes?: CameraShake[];
};
export type CameraPose = {move: Point; truck: Point; dolly: number; zoom: number; rotate: number};

export const restPose = (): Pose => ({move: [0, 0], rotate: 0, scale: [1, 1]});
const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

export function easeAt(ease: Ease | undefined, t: number) {
  if (ease === 'linear') return t;
  if (ease === 'ease_in') return t * t;
  if (ease === 'ease_out') return 1 - (1 - t) * (1 - t);
  if (ease === 'hold') return 0;
  return t * t * (3 - 2 * t);
}

// Both ordered step tracks and interpolated tracks use the same time boundary.
function upperBound<T extends Timed>(track: readonly T[], time: number) {
  let lo = 0, hi = track.length;
  while (lo < hi) {
    const mid = lo + Math.floor((hi - lo) / 2);
    if (track[mid].time <= time) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

export function assertTrack(track: readonly Timed[], duration: number | null, label: string) {
  let prior = -Infinity;
  for (const {time} of track) {
    if (!Number.isFinite(time) || time < 0 || time <= prior ||
        (duration != null && time > duration)) {
      throw new Error(`${label} times must be strictly increasing and fit the available timeline`);
    }
    prior = time;
  }
}

export function assertLocalTime(clock: LocalTime | null | undefined, label: string) {
  if (!clock) return;
  const {start = 0, offset = 0, scale = 1, duration = null, mode = 'free'} = clock;
  if (![start, offset, scale].every(Number.isFinite))
    throw new Error(`${label} local time values must be finite`);
  if (!['free', 'clamp', 'repeat', 'pingpong'].includes(mode))
    throw new Error(`${label} has an unknown local time mode`);
  if (mode === 'free' && duration != null)
    throw new Error(`${label} free local time must not declare a duration`);
  if (mode !== 'free' && (!Number.isFinite(duration) || duration! <= 0))
    throw new Error(`${label} bounded local time needs a positive duration`);
}

const modulo = (value: number, modulus: number) => ((value % modulus) + modulus) % modulus;

export function localTimeAt(clock: LocalTime | null | undefined, parentTime: number) {
  if (!clock) return parentTime;
  const raw = (clock.offset ?? 0) + (parentTime - (clock.start ?? 0)) * (clock.scale ?? 1);
  const mode = clock.mode ?? 'free';
  if (mode === 'free') return raw;
  const duration = clock.duration!;
  if (mode === 'clamp') return Math.max(0, Math.min(duration, raw));
  if (mode === 'repeat') return modulo(raw, duration);
  const phase = modulo(raw, duration * 2);
  return phase <= duration ? phase : duration * 2 - phase;
}

export function assertWindows(windows: readonly Window[], duration: number, label: string) {
  if (windows.some(([, end]) => end > duration)) {
    throw new Error(`${label} window exceeds the scene duration`);
  }
}

export const isActive = (windows: readonly Window[], time: number) =>
  !windows.length || windows.some(([from, until]) => from <= time && time < until);

export function poseAt(track: readonly (Timed & Pose)[], time: number): Pose {
  if (!track.length) return restPose();
  const next = upperBound(track, time);
  if (time <= track[0].time) return track[0];
  if (next === track.length) return track[next - 1];
  const a = track[next - 1], b = track[next];
  const linear = (time - a.time) / (b.time - a.time);
  const t = easeAt(a.ease, linear);
  const point = (p: Point, q: Point): Point => [lerp(p[0], q[0], t), lerp(p[1], q[1], t)];
  return {move: point(a.move, b.move), scale: point(a.scale, b.scale), rotate: lerp(a.rotate, b.rotate, t)};
}

export function scalarAt(track: readonly ScalarKey[], time: number, fallback: number) {
  if (!track.length) return fallback;
  const next = upperBound(track, time);
  if (time <= track[0].time) return track[0].value;
  if (next === track.length) return track[next - 1].value;
  const a = track[next - 1], b = track[next];
  const t = easeAt(a.ease, (time - a.time) / (b.time - a.time));
  return lerp(a.value, b.value, t);
}

const normalizedCamera = (key?: Partial<CameraKey>): CameraPose => ({
  move:key?.move ?? [0, 0], truck:key?.truck ?? [0, 0], dolly:key?.dolly ?? 0,
  zoom:key?.zoom ?? 1, rotate:key?.rotate ?? 0,
});

function baseCameraAt(track: readonly CameraKey[], time: number): CameraPose {
  if (!track.length) return normalizedCamera();
  const next = upperBound(track, time);
  if (time <= track[0].time) return normalizedCamera(track[0]);
  if (next === track.length) return normalizedCamera(track[next - 1]);
  const from = track[next - 1], to = track[next];
  const a = normalizedCamera(from), b = normalizedCamera(to);
  const t = easeAt(from.ease, (time - from.time) / (to.time - from.time));
  return {
    move:[lerp(a.move[0], b.move[0], t), lerp(a.move[1], b.move[1], t)],
    truck:[lerp(a.truck[0], b.truck[0], t), lerp(a.truck[1], b.truck[1], t)],
    dolly:lerp(a.dolly, b.dolly, t),
    zoom:lerp(a.zoom, b.zoom, t), rotate:lerp(a.rotate, b.rotate, t),
  };
}

export function cameraAt(camera: CameraTrack | null | undefined, time: number): CameraPose {
  const base = baseCameraAt(camera?.motion ?? [], time);
  let x = base.move[0], y = base.move[1], rotate = base.rotate;
  for (const shake of camera?.shakes ?? []) {
    const local = time - shake.time;
    if (local < 0 || local >= shake.duration) continue;
    const progress = local / shake.duration;
    const envelope = Math.pow(1 - progress, shake.decay);
    const phase = shake.seed * 2.399963229728653;
    const cycle = local * shake.frequency * Math.PI * 2;
    x += Math.sin(cycle + phase) * shake.amplitude[0] * envelope;
    y += Math.sin(cycle * 1.173 + phase + 2.094) * shake.amplitude[1] * envelope;
    rotate += Math.sin(cycle * 0.913 + phase + 4.189) * shake.rotate * envelope;
  }
  return {...base, move:[x, y], rotate};
}

export function lightAt(appearance: {light?: LightKey[]; light_axis: Point} | null, time: number) {
  if (!appearance?.light?.length) return null;
  const track = appearance.light;
  const next = upperBound(track, time);
  const a = track[next === track.length ? next - 1 : Math.max(0, next - 1)];
  const b = track[Math.min(next, track.length - 1)];
  const t = a === b ? 0 : (time - a.time) / (b.time - a.time);
  const [x, y] = appearance.light_axis;
  const norm = Math.abs(x) + Math.abs(y);
  return [x / norm, y / norm, lerp(a.exposure, b.exposure, t), lerp(a.gradient, b.gradient, t)];
}

export function depthAt(track: readonly DepthKey[] | undefined, time: number, fallback: number) {
  return track?.[upperBound(track, time) - 1]?.z ?? fallback;
}

export function makeDrawOrder(layers: readonly {depth?: readonly DepthKey[]}[]) {
  const original = layers.map((_, index) => index);
  if (!layers.some(node => node.depth?.length)) return (_time: number) => original;
  return (time: number) => {
    const z = layers.map(({depth}, i) => depthAt(depth, time, i));
    return original.slice().sort((a, b) => z[a] - z[b] || a - b);
  };
}
