export type Point = readonly [number, number];
export type Timed = {time: number};
export type Pose = {move: Point; rotate: number; scale: Point};
export type LightKey = Timed & {exposure: number; gradient: number};
export type DepthKey = Timed & {z: number};
export type Window = readonly [number, number];

export const restPose = (): Pose => ({move: [0, 0], rotate: 0, scale: [1, 1]});
const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

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

export function assertTrack(track: readonly Timed[], duration: number, label: string) {
  let prior = -Infinity;
  for (const {time} of track) {
    if (!Number.isFinite(time) || time < 0 || time <= prior || time > duration) {
      throw new Error(`${label} times must be strictly increasing and fit the scene duration`);
    }
    prior = time;
  }
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
  const t = linear * linear * (3 - 2 * linear);
  const point = (p: Point, q: Point): Point => [lerp(p[0], q[0], t), lerp(p[1], q[1], t)];
  return {move: point(a.move, b.move), scale: point(a.scale, b.scale), rotate: lerp(a.rotate, b.rotate, t)};
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

export function makeDrawOrder(layers: readonly {depth?: readonly DepthKey[]}[]) {
  const original = layers.map((_, index) => index);
  if (!layers.some(node => node.depth?.length)) return (_time: number) => original;
  return (time: number) => {
    const z = layers.map(({depth}, i) => depth?.[upperBound(depth, time) - 1]?.z ?? i);
    return original.slice().sort((a, b) => z[a] - z[b] || a - b);
  };
}
