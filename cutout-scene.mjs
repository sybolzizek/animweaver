import {isActive, lightAt, makeDrawOrder, poseAt, restPose} from './cutout-timeline.mjs';

export async function loadCutoutScene(canvas, source) {
  const url = new URL(source, document.baseURI);
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Could not load scene: ${response.status}`);
  const spec = await response.json();
  if (spec.schema !== 1) throw new Error("Unsupported cutout scene version");
  canvas.width = spec.width;
  canvas.height = spec.height;
  const ctx = canvas.getContext("2d", {willReadFrequently:true});
  const load = async file => {
    if (!file) return null;
    const image = new Image();
    image.src = new URL(file, url);
    await image.decode();
    return image;
  };
  const [back, backing, ...cutouts] = await Promise.all(
    [spec.background, spec.backing, ...spec.layers.map(node => node.file)].map(load)
  );
  const seams = spec.seams ?? [];
  const makeMesh = (seams.length || spec.layers.some(node => node.mesh || node.appearance?.light?.length))
    ? (await import("./cutout-mesh.mjs")).createMeshRenderer(spec.width, spec.height)
    : null;
  const meshes = spec.layers.map((node, i) => {
    if (node.mesh) return makeMesh(node, cutouts[i]);
    if (node.appearance?.light?.length) return makeMesh(
      {...node, mesh:{grid:[1,1], weights:[[1],[1],[1],[1]]}}, cutouts[i]);
    return null;
  });
  const seamLinks = seams.length
    ? await Promise.all(seams.map(async seam =>
        (await import("./cutout-seams.mjs")).linkSeam(seam, await load(seam.file), makeMesh)))
    : [];
  const shadowSets = new Map();
  for (const shadow of spec.shadows ?? []) {
    if (!shadowSets.has(shadow.receiver)) shadowSets.set(shadow.receiver, []);
    shadowSets.get(shadow.receiver).push(shadow);
  }
  const paintShadow = shadowSets.size
    ? (await import("./cutout-shadows.mjs")).createShadowPainter(spec.width, spec.height)
    : null;
  const baseOrder = spec.layers.map((_, i) => i);
  const drawOrder = makeDrawOrder(spec.layers);
  const nodes = new Map([...spec.groups, ...spec.layers].map(node => [node.key, node]));
  const indices = new Map(spec.layers.map((node, i) => [node.key, i]));
  const mattesByTarget = new Map();
  for (const matte of spec.mattes ?? []) {
    if (!mattesByTarget.has(matte.target)) mattesByTarget.set(matte.target, []);
    mattesByTarget.get(matte.target).push(matte);
  }
  function scratch() {
    const buffer = document.createElement("canvas");
    buffer.width = spec.width;
    buffer.height = spec.height;
    return buffer.getContext("2d");
  }
  const matteTarget = mattesByTarget.size ? scratch() : null;
  const matteSource = mattesByTarget.size ? scratch() : null;
  const identity = new DOMMatrix();
  const edits = new Map();
  let clock = 0;
  let showBacking = Boolean(backing);
  let lastSeams = [];
  const resolve = (key, time) => edits.get(key) ?? poseAt(nodes.get(key).motion ?? [], time);
  function makeFrame(time, moving) {
    const cache = new Map();
    const bones = node => node.mesh?.bones.map(world) ?? [identity];
    function point(key, [x, y]) {
      const i = indices.get(key), node = spec.layers[i];
      let xy = [x + node.box[0] - node.source_box[0], y + node.box[1] - node.source_box[1]];
      if (node.mesh) xy = meshes[i].point(xy, bones(node));
      return world(key).transformPoint({x:xy[0], y:xy[1]});
    }
    function world(key) {
      if (cache.has(key)) return cache.get(key);
      const node = nodes.get(key), pose = moving ? resolve(key, time) : restPose();
      const [x, y] = node.pivot;
      const local = new DOMMatrix()
        .translate(pose.move[0] + x, pose.move[1] + y)
        .rotate(pose.rotate)
        .scale(pose.scale[0], pose.scale[1])
        .translate(-x, -y);
      let result = node.parent ? world(node.parent).multiply(local) : local;
      if (node.mount) {
        const {layer, point:anchor, towards} = node.mount;
        const a = point(layer, anchor), b = point(layer, towards);
        const original = Math.atan2(towards[1] - anchor[1], towards[0] - anchor[0]);
        const angle = Math.hypot(b.x-a.x, b.y-a.y) > 1e-7
          ? (Math.atan2(b.y-a.y, b.x-a.x) - original) * 180 / Math.PI : 0;
        result = new DOMMatrix().translate(a.x, a.y).rotate(angle).translate(-x, -y).multiply(local);
      }
      cache.set(key, result);
      return result;
    }
    return {world, bones, point, time, moving};
  }
  function drawLayer(target, i, frame, outer = identity, shading = true) {
    const node = spec.layers[i];
    const matrix = outer.multiply(frame.world(node.key));
    const bones = frame.bones(node);
    const light = shading && frame.moving ? lightAt(node.appearance, frame.time) : null;
    if (meshes[i] && (bones.some(bone => !bone.isIdentity) || light?.slice(2).some(Boolean))) {
      target.setTransform(identity);
      target.drawImage(meshes[i](bones.map(bone => matrix.multiply(bone)), light ?? undefined), 0, 0);
    } else {
      target.setTransform(matrix);
      target.drawImage(cutouts[i], node.box[0], node.box[1]);
    }
  }
  function drawVisible(target, i, frame, outer = identity, shading = true) {
    const node = spec.layers[i];
    if (node.matte_only) return;
    const applicable = mattesByTarget.get(node.key)?.filter(matte => isActive(matte.windows, frame.time));
    if (!applicable?.length) {
      drawLayer(target, i, frame, outer, shading);
      return;
    }
    function reset(buffer) {
      buffer.resetTransform();
      buffer.globalCompositeOperation = "source-over";
      buffer.clearRect(0, 0, spec.width, spec.height);
    }
    reset(matteTarget);
    drawLayer(matteTarget, i, frame, outer, shading);
    matteTarget.resetTransform();
    for (const matte of applicable) {
      reset(matteSource);
      // Read the mask's authored alpha after motion but prior to any other matte.
      drawLayer(matteSource, indices.get(matte.mask), frame, outer, false);
      matteTarget.globalCompositeOperation = matte.operation === "keep" ? "destination-in" : "destination-out";
      matteTarget.drawImage(matteSource.canvas, 0, 0);
    }
    target.resetTransform();
    target.drawImage(matteTarget.canvas, 0, 0);
  }
  function render(time, fill, moving = true) {
    const frame = makeFrame(time, moving);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, spec.width, spec.height);
    if (fill && backing) ctx.drawImage(backing, 0, 0);
    ctx.drawImage(back, 0, 0);
    const drawReceiver = (key, receiver) => {
      const shadows = moving ? shadowSets.get(key) : null;
      if (!shadows) {
        receiver(ctx);
        return;
      }
      paintShadow(ctx, receiver, shadows.map(shadow => {
        const i = indices.get(shadow.caster);
        const [l,t,r,b] = spec.layers[i].source_box;
        const anchor = frame.point(shadow.caster, shadow.origin ?? [(l+r)/2,(t+b)/2]);
        return {shadow, anchor, caster:(paint, project) => drawVisible(paint, i, frame, project, false)};
      }));
    };
    drawReceiver(null, paint => {
      if (fill && backing) paint.drawImage(backing, 0, 0);
      paint.drawImage(back, 0, 0);
    });
    // Insert each seam beneath both of its incident surfaces, never above an occluder.
    const order = moving ? drawOrder(time) : baseOrder;
    const connectors = new Map();
    if (moving) {
      const ranks = new Map(order.map((i, rank) => [spec.layers[i].key, rank]));
      lastSeams = seamLinks.map(link => {
        const {vertices, ...status} = link.evaluate(frame);
        if (vertices) {
          const rank = Math.min(ranks.get(link.spec.a.layer), ranks.get(link.spec.b.layer));
          if (!connectors.has(rank)) connectors.set(rank, []);
          connectors.get(rank).push([link, vertices]);
        }
        return {key:link.spec.key, ...status};
      });
    }
    // Paint order changes independently of pixel ownership and bone parenting.
    for (const [rank, i] of order.entries()) {
      for (const [link, vertices] of connectors.get(rank) ?? []) link.draw(ctx, vertices);
      drawReceiver(spec.layers[i].key, paint => drawVisible(paint, i, frame));
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
  }
  render(0, false, false);
  const basis = ctx.getImageData(0, 0, spec.width, spec.height).data;
  function missing() {
    const pixels = ctx.getImageData(0, 0, spec.width, spec.height).data;
    let count = 0;
    for (let i = 3; i < basis.length; i += 4)
      if (pixels[i] + 2 < basis[i]) count++;
    return count;
  }
  function seek(time) {
    if (!Number.isFinite(time)) throw new Error("Invalid seek time");
    clock = Math.max(0, Math.min(spec.duration, time));
    render(clock, showBacking);
  }
  const api = {
    spec,
    seek,
    seamStatus() { return structuredClone(lastSeams); },
    pose(key) {
      return structuredClone(resolve(key, clock));
    },
    surfacePoint(key, xy) {
      if (!indices.has(key) || xy?.length !== 2 || !xy.every(Number.isFinite))
        throw new Error("Expected a cutout key and a source pixel point");
      const [l,t,r,b] = spec.layers[indices.get(key)].source_box;
      if (xy[0] < l || xy[0] > r || xy[1] < t || xy[1] > b)
        throw new Error("Point is outside the cutout raster");
      const {x,y} = makeFrame(clock, true).point(key, xy);
      return [x,y];
    },
    setPose(key, update) {
      if (!nodes.has(key)) throw new Error("Unknown cutout or group");
      if (update === null) edits.delete(key);
      else {
        const {move, rotate, scale} = {...resolve(key, clock), ...update};
        if (move?.length !== 2 || scale?.length !== 2 ||
            ![...move, rotate, ...scale].every(Number.isFinite) || scale.some(v => v <= 0))
          throw new Error("Expected finite movement, rotation and positive X/Y scale");
        if (nodes.get(key).mount && move.some(Boolean))
          throw new Error("A surface mount controls translation");
        edits.set(key, {move:[...move], rotate, scale:[...scale]});
      }
      seek(clock);
    },
    moveBy(key, dx, dy) {
      const node = nodes.get(key);
      if (!node) throw new Error("Unknown cutout or group");
      const inverse = node.parent ? makeFrame(clock, true).world(node.parent).inverse() : identity;
      const from = inverse.transformPoint({x:0, y:0});
      const to = inverse.transformPoint({x:dx, y:dy});
      const [x,y] = resolve(key, clock).move;
      api.setPose(key,{move:[x + to.x - from.x, y + to.y - from.y]});
    },
    backing(visible) {
      showBacking = Boolean(visible);
      seek(clock);
    },
    coverage(time = clock) {
      render(time, false);
      const exposed = missing();
      render(time, true);
      const unfilled = missing();
      seek(clock);
      return {exposed, unfilled};
    },
    overrides() {
      return structuredClone(Object.fromEntries(edits));
    },
    exportScene() {
      const output = structuredClone(spec);
      for (const node of [...output.groups, ...output.layers]) {
        const pose = edits.get(node.key);
        if (!pose) continue;
        const time = Number(clock.toFixed(5));
        node.motion = [...node.motion.filter(frame => Math.abs(frame.time - time) > 1e-5),
                       {time, ...structuredClone(pose)}].sort((a,b)=>a.time-b.time);
      }
      return output;
    },
  };
  seek(0);
  return api;
}
