import {createSceneEvaluator} from './scene-evaluate.mjs';
import {isActive} from './cutout-timeline.mjs';

export async function loadCutoutScene(canvas, source) {
  const url = new URL(source, document.baseURI);
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Could not load scene: ${response.status}`);
  const spec = await response.json();
  if (spec.schema !== 1) throw new Error("Unsupported cutout scene version");
  if (spec.layers.some(node => node.scene_id))
    throw new Error("Scene sources require the Three render backend");
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
  const nodes = new Map([...spec.groups, ...spec.layers].map(node => [node.key, node]));
  const indices = new Map(spec.layers.map((node, i) => [node.key, i]));
  const groups = new Map(spec.groups.map(node => [node.key, node]));
  const isolated = new Set(spec.groups.filter(node =>
    node.opacity?.length || (node.blend_mode ?? "normal") !== "normal" ||
    (node.plane_depth ?? 0) !== 0).map(node => node.key));
  function groupScope(node) {
    let parent = node.parent;
    while (parent != null) {
      if (isolated.has(parent)) return parent;
      parent = groups.get(parent)?.parent;
    }
    return null;
  }
  function layerScope(node) {
    let parent = node.parent;
    while (parent != null) {
      if (isolated.has(parent)) return parent;
      parent = groups.get(parent)?.parent;
    }
    return null;
  }
  const scopeOfLayer = new Map(spec.layers.map(node => [node.key, layerScope(node)]));
  const descendants = new Map([...isolated].map(key => [key, []]));
  for (const [i, layer] of spec.layers.entries()) {
    let parent = layer.parent;
    while (parent != null) {
      if (isolated.has(parent)) descendants.get(parent).push(i);
      parent = groups.get(parent)?.parent;
    }
  }
  const unitsByScope = new Map();
  const addUnit = (scope, unit) => {
    if (!unitsByScope.has(scope)) unitsByScope.set(scope, []);
    unitsByScope.get(scope).push(unit);
  };
  for (const [i, node] of spec.layers.entries()) {
    addUnit(scopeOfLayer.get(node.key), {kind:"layer", key:node.key, index:i, order:i, node});
  }
  for (const key of isolated) {
    const node = groups.get(key);
    addUnit(groupScope(node), {
      kind:"group", key, order:Math.min(...descendants.get(key)), node,
    });
  }
  for (const units of unitsByScope.values()) units.sort((a, b) => a.order - b.order);
  function orderedUnits(scope, frame) {
    const units = unitsByScope.get(scope) ?? [];
    if (!frame.moving || !units.some(unit => unit.kind === "layer" && unit.node.depth?.length)) return units;
    return units.slice().sort((a, b) =>
      (a.kind === "layer" ? frame.depth(a.key, a.order) : a.order) -
      (b.kind === "layer" ? frame.depth(b.key, b.order) : b.order) || a.order - b.order);
  }
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
  function resetBuffer(buffer) {
    buffer.resetTransform();
    buffer.globalCompositeOperation = "source-over";
    buffer.globalAlpha = 1;
    buffer.clearRect(0, 0, spec.width, spec.height);
  }
  const matteTarget = mattesByTarget.size ? scratch() : null;
  const matteSource = mattesByTarget.size ? scratch() : null;
  const groupBuffers = new Map([...isolated].map(key => [key, scratch()]));
  const cameraSource = spec.camera ? scratch() : null;
  const identity = new DOMMatrix();
  let clock = 0;
  let showBacking = Boolean(backing);
  let lastSeams = [];
  const blendMode = blend => blend === "normal" ? "source-over" : blend === "add" ? "lighter" : blend;
  const opacityAt = (node, frame) => frame.opacity(node.key);
  function compositeNode(target, node, frame, paint) {
    target.save();
    target.globalAlpha *= opacityAt(node, frame);
    target.globalCompositeOperation = blendMode(node.blend_mode ?? "normal");
    paint();
    target.restore();
  }
  function projection(frame) {
    const state = frame.camera;
    const pivot = spec.camera?.pivot ?? [spec.width / 2, spec.height / 2];
    const focal = spec.camera?.focal ?? 2 * Math.max(spec.width, spec.height);
    return {state, pivot, focal};
  }
  function cameraMatrix(frame) {
    if (!spec.camera) return identity;
    const {state, pivot:[x, y], focal} = projection(frame);
    const perspective = focal / (focal - state.dolly);
    return new DOMMatrix()
      .translate(x + state.move[0], y + state.move[1])
      .rotate(state.rotate)
      .scale(state.zoom * perspective)
      .translate(-x - state.truck[0], -y - state.truck[1]);
  }
  function depthMatrix(node, frame) {
    const depth = frame.moving ? node.plane_depth ?? 0 : 0;
    if (!depth) return identity;
    const {state, pivot:[x, y], focal} = projection(frame);
    const distance = focal - state.dolly;
    const relative = distance / (distance - depth);
    return new DOMMatrix()
      .translate(x + state.truck[0], y + state.truck[1])
      .scale(relative)
      .translate(-x - state.truck[0], -y - state.truck[1]);
  }
  function compositeDepthMatrix(node, frame, includeSelf = false) {
    let result = includeSelf && isolated.has(node.key)
      ? depthMatrix(node, frame) : identity;
    let parent = node.parent;
    while (parent != null) {
      const group = groups.get(parent);
      if (isolated.has(parent)) result = depthMatrix(group, frame).multiply(result);
      parent = group?.parent;
    }
    return result;
  }
  function frameCamera(frame) {
    cameraSource.resetTransform();
    cameraSource.globalCompositeOperation = "source-over";
    cameraSource.clearRect(0, 0, spec.width, spec.height);
    cameraSource.drawImage(ctx.canvas, 0, 0);
    ctx.resetTransform();
    ctx.clearRect(0, 0, spec.width, spec.height);
    ctx.setTransform(cameraMatrix(frame));
    ctx.drawImage(cameraSource.canvas, 0, 0);
    ctx.resetTransform();
  }
  const evaluator = createSceneEvaluator(spec, {
    surfacePoint(key, [x, y], frame) {
      const i = indices.get(key), node = spec.layers[i];
      let xy = [x + node.box[0] - node.source_box[0], y + node.box[1] - node.source_box[1]];
      if (node.mesh) xy = meshes[i].point(xy, frame.bones(node));
      const point = frame.world(key).transformPoint({x:xy[0], y:xy[1]});
      return depthMatrix(node, frame).transformPoint(point);
    },
  });
  function drawLayer(target, i, frame, outer = identity, shading = true) {
    const node = spec.layers[i];
    const matrix = outer.multiply(depthMatrix(node, frame)).multiply(frame.world(node.key));
    const bones = frame.bones(node);
    const light = shading ? frame.light(node.key) : null;
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
      compositeNode(target, node, frame, () => drawLayer(target, i, frame, outer, shading));
      return;
    }
    resetBuffer(matteTarget);
    drawLayer(matteTarget, i, frame, outer, shading);
    matteTarget.resetTransform();
    for (const matte of applicable) {
      resetBuffer(matteSource);
      // Read the mask's authored alpha after motion but prior to any other matte.
      const mask = spec.layers[indices.get(matte.mask)];
      matteSource.globalAlpha = opacityAt(mask, frame);
      drawLayer(matteSource, indices.get(matte.mask), frame, outer, false);
      matteTarget.globalCompositeOperation = matte.operation === "keep" ? "destination-in" : "destination-out";
      matteTarget.drawImage(matteSource.canvas, 0, 0);
    }
    compositeNode(target, node, frame, () => {
      target.resetTransform();
      target.drawImage(matteTarget.canvas, 0, 0);
    });
  }
  function render(time, fill, moving = true) {
    const frame = evaluator.evaluate(time, moving);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, spec.width, spec.height);
    if (fill && backing) ctx.drawImage(backing, 0, 0);
    ctx.drawImage(back, 0, 0);
    const drawReceiver = (target, key, receiver) => {
      const shadows = moving ? shadowSets.get(key) : null;
      if (!shadows) {
        receiver(target);
        return;
      }
      paintShadow(target, receiver, shadows.map(shadow => {
        const i = indices.get(shadow.caster);
        const [l,t,r,b] = spec.layers[i].source_box;
        const anchor = frame.point(shadow.caster, shadow.origin ?? [(l+r)/2,(t+b)/2]);
        return {shadow, anchor, caster:(paint, project) => drawVisible(paint, i, frame, project, false)};
      }));
    };
    drawReceiver(ctx, null, paint => {
      if (fill && backing) paint.drawImage(backing, 0, 0);
      paint.drawImage(back, 0, 0);
    });
    const orders = new Map([...unitsByScope.keys()].map(scope =>
      [scope, orderedUnits(scope, frame)]));
    const ranks = new Map([...orders].map(([scope, units]) =>
      [scope, new Map(units.map((unit, rank) => [unit.key, rank]))]));
    const connectors = new Map();
    if (moving) {
      lastSeams = seamLinks.map(link => {
        const {vertices, ...status} = link.evaluate(frame);
        if (vertices) {
          const scope = scopeOfLayer.get(link.spec.a.layer);
          const rank = Math.min(
            ranks.get(scope).get(link.spec.a.layer),
            ranks.get(scope).get(link.spec.b.layer));
          if (!connectors.has(scope)) connectors.set(scope, new Map());
          if (!connectors.get(scope).has(rank)) connectors.get(scope).set(rank, []);
          connectors.get(scope).get(rank).push([link, vertices]);
        }
        return {key:link.spec.key, ...status};
      });
    } else lastSeams = [];
    const drawConnector = (target, link, vertices) => {
        const a = spec.layers[indices.get(link.spec.a.layer)];
        const b = spec.layers[indices.get(link.spec.b.layer)];
        target.save();
        target.globalAlpha *= Math.min(opacityAt(a, frame), opacityAt(b, frame));
        target.globalCompositeOperation = a.blend_mode === b.blend_mode
          ? blendMode(a.blend_mode ?? "normal") : "source-over";
        link.draw(target, vertices);
        target.restore();
    };
    const drawGroup = (target, key) => {
      const buffer = groupBuffers.get(key);
      resetBuffer(buffer);
      drawScope(buffer, key);
      const node = groups.get(key);
      compositeNode(target, node, frame, () => {
        target.setTransform(depthMatrix(node, frame));
        target.drawImage(buffer.canvas, 0, 0);
      });
    };
    const drawScope = (target, scope) => {
      const order = orders.get(scope) ?? [];
      for (const [rank, unit] of order.entries()) {
        for (const [link, vertices] of connectors.get(scope)?.get(rank) ?? [])
          drawConnector(target, link, vertices);
        if (unit.kind === "group") drawGroup(target, unit.key);
        else drawReceiver(target, unit.key,
          paint => drawVisible(paint, unit.index, frame));
      }
    };
    drawScope(ctx, null);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    if (moving && cameraSource) frameCamera(frame);
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
      if (!nodes.has(key)) throw new Error("Unknown cutout or group");
      return structuredClone(evaluator.evaluate(clock).pose(key));
    },
    localTime(key) {
      if (!nodes.has(key)) throw new Error("Unknown cutout or group");
      return evaluator.evaluate(clock).localTime(key);
    },
    surfacePoint(key, xy) {
      if (!indices.has(key) || xy?.length !== 2 || !xy.every(Number.isFinite))
        throw new Error("Expected a cutout key and a source pixel point");
      const [l,t,r,b] = spec.layers[indices.get(key)].source_box;
      if (xy[0] < l || xy[0] > r || xy[1] < t || xy[1] > b)
        throw new Error("Point is outside the cutout raster");
      const node = spec.layers[indices.get(key)];
      const frame = evaluator.evaluate(clock);
      const point = frame.point(key, xy);
      const grouped = compositeDepthMatrix(node, frame).transformPoint(point);
      const {x,y} = cameraMatrix(frame).transformPoint(grouped);
      return [x,y];
    },
    setPose(key, update) {
      if (!nodes.has(key)) throw new Error("Unknown cutout or group");
      if (update === null) evaluator.setOverride(key, null);
      else {
        const {move, rotate, scale} = {...evaluator.evaluate(clock).pose(key), ...update};
        if (move?.length !== 2 || scale?.length !== 2 ||
            ![...move, rotate, ...scale].every(Number.isFinite) || scale.some(v => v <= 0))
          throw new Error("Expected finite movement, rotation and positive X/Y scale");
        if (nodes.get(key).mount && move.some(Boolean))
          throw new Error("A surface mount controls translation");
        evaluator.setOverride(key, {move:[...move], rotate, scale:[...scale]});
      }
      seek(clock);
    },
    moveBy(key, dx, dy) {
      const node = nodes.get(key);
      if (!node) throw new Error("Unknown cutout or group");
      const frame = evaluator.evaluate(clock);
      const parent = node.parent ? frame.world(node.parent) : identity;
      const own = indices.has(key) ? depthMatrix(node, frame) : identity;
      const stage = compositeDepthMatrix(node, frame, !indices.has(key)).multiply(own);
      const inverse = cameraMatrix(frame).multiply(stage).multiply(parent).inverse();
      const from = inverse.transformPoint({x:0, y:0});
      const to = inverse.transformPoint({x:dx, y:dy});
      const [x,y] = frame.pose(key).move;
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
      return evaluator.overrides();
    },
    exportScene() {
      const output = structuredClone(spec);
      const frame = evaluator.evaluate(clock);
      for (const node of [...output.groups, ...output.layers]) {
        const pose = evaluator.override(node.key);
        if (!pose) continue;
        const time = Number(Math.max(0, frame.localTime(node.key)).toFixed(5));
        node.motion = [...node.motion.filter(frame => Math.abs(frame.time - time) > 1e-5),
                       {time, ...structuredClone(pose)}].sort((a,b)=>a.time-b.time);
      }
      return output;
    },
  };
  seek(0);
  return api;
}
