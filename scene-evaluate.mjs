import {cameraAt, depthAt, lightAt, localTimeAt, poseAt, restPose, scalarAt} from './cutout-timeline.mjs';

// Evaluate authored scene data without drawing it. A renderer supplies the
// geometry query used by surface mounts and can consume the returned matrices,
// clocks and scalar values without owning timeline semantics.
export function createSceneEvaluator(spec, {surfacePoint} = {}) {
  const nodes = new Map([...spec.groups, ...spec.layers].map(node => [node.key, node]));
  const overrides = new Map();
  const identity = new DOMMatrix();

  function evaluate(time, moving = true) {
    if (!Number.isFinite(time)) throw new Error('Invalid scene time');
    const worldCache = new Map();
    const timeCache = new Map();
    const frame = {
      time,
      moving,
      camera: cameraAt(spec.camera, time),
      localTime,
      pose,
      opacity,
      depth,
      light,
      world,
      bones,
      point,
    };

    function localTime(key) {
      if (timeCache.has(key)) return timeCache.get(key);
      const node = nodes.get(key);
      if (!node) throw new Error(`Unknown scene node: ${key}`);
      const parentTime = node.parent ? localTime(node.parent) : time;
      const mapped = localTimeAt(node.local_time, parentTime);
      timeCache.set(key, mapped);
      return mapped;
    }

    function pose(key) {
      const node = nodes.get(key);
      if (!node) throw new Error(`Unknown scene node: ${key}`);
      return overrides.get(key) ?? poseAt(node.motion ?? [], localTime(key));
    }

    function opacity(key) {
      const node = nodes.get(key);
      if (!node) throw new Error(`Unknown scene node: ${key}`);
      return moving ? scalarAt(node.opacity ?? [], localTime(key), 1) : 1;
    }

    function depth(key, fallback) {
      const node = nodes.get(key);
      if (!node) throw new Error(`Unknown scene node: ${key}`);
      return moving ? depthAt(node.depth, localTime(key), fallback) : fallback;
    }

    function light(key) {
      const node = nodes.get(key);
      if (!node) throw new Error(`Unknown scene node: ${key}`);
      return moving ? lightAt(node.appearance, localTime(key)) : null;
    }

    function bones(node) {
      return node.mesh?.bones.map(world) ?? [identity];
    }

    function point(key, xy) {
      if (!surfacePoint) throw new Error('This renderer does not provide surface geometry');
      return surfacePoint(key, xy, frame);
    }

    function world(key) {
      if (worldCache.has(key)) return worldCache.get(key);
      const node = nodes.get(key);
      if (!node) throw new Error(`Unknown scene node: ${key}`);
      const state = moving ? pose(key) : restPose();
      const [x, y] = node.pivot;
      const local = new DOMMatrix()
        .translate(state.move[0] + x, state.move[1] + y)
        .rotate(state.rotate)
        .scale(state.scale[0], state.scale[1])
        .translate(-x, -y);
      let result = node.parent ? world(node.parent).multiply(local) : local;
      if (node.mount) {
        const {layer, point:anchor, towards} = node.mount;
        const a = point(layer, anchor), b = point(layer, towards);
        const original = Math.atan2(towards[1] - anchor[1], towards[0] - anchor[0]);
        const angle = Math.hypot(b.x - a.x, b.y - a.y) > 1e-7
          ? (Math.atan2(b.y - a.y, b.x - a.x) - original) * 180 / Math.PI : 0;
        result = new DOMMatrix().translate(a.x, a.y).rotate(angle).translate(-x, -y).multiply(local);
      }
      worldCache.set(key, result);
      return result;
    }

    return frame;
  }

  return {
    has(key) { return nodes.has(key); },
    evaluate,
    override(key) { return overrides.get(key); },
    setOverride(key, pose) {
      if (!nodes.has(key)) throw new Error('Unknown cutout or group');
      if (pose == null) overrides.delete(key);
      else overrides.set(key, structuredClone(pose));
    },
    overrides() { return structuredClone(Object.fromEntries(overrides)); },
  };
}
