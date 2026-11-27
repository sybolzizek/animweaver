import * as THREE from '/three.module.js';
import {createSceneEvaluator} from './scene-evaluate.mjs';
import {evaluateSeam} from './cutout-seams.mjs';
import {isActive} from './cutout-timeline.mjs';

function assertCompatible(spec) {
  const unsupported = [];
  const blends = new Set(['normal', 'add', 'multiply', 'screen']);
  for (const group of spec.groups) {
    if (!blends.has(group.blend_mode ?? 'normal')) unsupported.push(`blend mode ${group.key}`);
  }
  for (const layer of spec.layers) {
    if (!blends.has(layer.blend_mode ?? 'normal')) unsupported.push(`blend mode ${layer.key}`);
  }
  if (unsupported.length)
    throw new Error(`Three backend does not support: ${[...new Set(unsupported)].join(', ')}`);
}

function matrixFromDOM(source) {
  return new THREE.Matrix4().set(
    source.a, source.c, 0, source.e,
    source.b, source.d, 0, source.f,
    0, 0, 1, 0,
    0, 0, 0, 1,
  );
}

async function fetchSpec(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Could not load scene: ${response.status}`);
  const spec = await response.json();
  if (spec.schema !== 1) throw new Error('Unsupported cutout scene version');
  assertCompatible(spec);
  return spec;
}

function makeTarget(width, height, resources) {
  const target = new THREE.WebGLRenderTarget(width, height, {
    depthBuffer:false,
    stencilBuffer:false,
    minFilter:THREE.LinearFilter,
    magFilter:THREE.LinearFilter,
    format:THREE.RGBAFormat,
    type:THREE.UnsignedByteType,
  });
  target.texture.colorSpace = THREE.NoColorSpace;
  target.texture.generateMipmaps = false;
  resources.push(target);
  return target;
}

function makeCamera() {
  const camera = new THREE.PerspectiveCamera();
  camera.up.set(0, 1, 0);
  return camera;
}

function configureCamera(camera, width, height, {
  center, principal, distance, focal, rotate = 0, far,
}) {
  const near = 0.1;
  camera.position.set(center[0], center[1], distance);
  camera.up.set(0, 1, 0);
  camera.lookAt(center[0], center[1], 0);
  if (rotate) camera.rotateZ(THREE.MathUtils.degToRad(-rotate));
  const left = -principal[0] * near / focal;
  const right = (width - principal[0]) * near / focal;
  // Keep Three's camera right vector positive-X and invert only projection Y
  // so authored canvas coordinates continue increasing down the screen.
  const top = -principal[1] * near / focal;
  const bottom = (height - principal[1]) * near / focal;
  camera.near = near;
  camera.far = far;
  camera.projectionMatrix.makePerspective(
    left, right, top, bottom, near, far, THREE.WebGLCoordinateSystem,
  );
  camera.projectionMatrixInverse.copy(camera.projectionMatrix).invert();
  camera.updateMatrixWorld(true);
}

function configureCameras(spec, frame, finalCamera, stageCamera, far) {
  const pivot = spec.camera?.pivot ?? [spec.width / 2, spec.height / 2];
  const focal = spec.camera?.focal ?? 2 * Math.max(spec.width, spec.height);
  const state = frame.camera;
  const distance = focal - state.dolly;
  const center = [pivot[0] + state.truck[0], pivot[1] + state.truck[1]];
  configureCamera(finalCamera, spec.width, spec.height, {
    center,
    principal:[pivot[0] + state.move[0], pivot[1] + state.move[1]],
    distance,
    focal:focal * state.zoom,
    rotate:state.rotate,
    far,
  });
  // Offscreen group targets stay in stage coordinates. At depth zero this
  // camera is exactly 1:1; nonzero direct-child depths receive only their
  // relative perspective before the group surface is projected by its parent.
  configureCamera(stageCamera, spec.width, spec.height, {
    center,
    principal:center,
    distance,
    focal:distance,
    far,
  });
}

function makePlane(_scene, map, width, height, order, resources, premultiplied = false) {
  const geometry = new THREE.PlaneGeometry(width, height);
  // Authored stage coordinates increase downward. Source images and framebuffer
  // textures both need the plane UVs inverted to remain upright in that space.
  const uv = geometry.getAttribute('uv');
  for (let index = 0; index < uv.count; index++) uv.setY(index, 1 - uv.getY(index));
  uv.needsUpdate = true;
  const material = new THREE.MeshBasicMaterial({
    map,
    transparent:true,
    depthTest:false,
    depthWrite:false,
    toneMapped:false,
    side:THREE.DoubleSide,
  });
  if (premultiplied) {
    material.blending = THREE.CustomBlending;
    material.blendEquation = THREE.AddEquation;
    material.blendSrc = THREE.OneFactor;
    material.blendDst = THREE.OneMinusSrcAlphaFactor;
    material.blendEquationAlpha = THREE.AddEquation;
    material.blendSrcAlpha = THREE.OneFactor;
    material.blendDstAlpha = THREE.OneMinusSrcAlphaFactor;
  }
  const mesh = new THREE.Mesh(geometry, material);
  mesh.matrixAutoUpdate = false;
  mesh.frustumCulled = false;
  mesh.renderOrder = order;
  const surfaceScene = new THREE.Scene();
  surfaceScene.add(mesh);
  mesh.userData.surfaceScene = surfaceScene;
  resources.push(geometry, material);
  return mesh;
}

const localLightFragment = `
  vec3 localLight(vec3 rgb, vec2 lightUv, vec4 light) {
    if (light.z == 0.0 && light.w == 0.0) return rgb;
    vec3 linear = mix(
      rgb / 12.92,
      pow((rgb + 0.055) / 1.055, vec3(2.4)),
      step(vec3(0.04045), rgb)
    );
    linear = clamp(
      linear * exp2(light.z + light.w * dot(lightUv - 0.5, light.xy)),
      0.0,
      1.0
    );
    return mix(
      linear * 12.92,
      1.055 * pow(linear, vec3(1.0 / 2.4)) - 0.055,
      step(vec3(0.0031308), linear)
    );
  }
`;

function makeLitPlane(map, width, height, order, resources) {
  const geometry = new THREE.PlaneGeometry(width, height);
  const uv = geometry.getAttribute('uv');
  const position = geometry.getAttribute('position');
  const lightUvs = new Float32Array(uv.count * 2);
  for (let index = 0; index < uv.count; index++) {
    uv.setY(index, 1 - uv.getY(index));
    lightUvs[index * 2] = position.getX(index) / width + 0.5;
    lightUvs[index * 2 + 1] = 0.5 - position.getY(index) / height;
  }
  uv.needsUpdate = true;
  geometry.setAttribute('lightUv', new THREE.BufferAttribute(lightUvs, 2));
  const material = new THREE.RawShaderMaterial({
    glslVersion:THREE.GLSL3,
    uniforms:{
      map:{value:map},
      opacity:{value:1},
      light:{value:new THREE.Vector4()},
    },
    vertexShader:`
      precision highp float;
      in vec3 position;
      in vec2 uv;
      in vec2 lightUv;
      uniform mat4 modelViewMatrix;
      uniform mat4 projectionMatrix;
      out vec2 vUv;
      out vec2 vLightUv;
      void main() {
        vUv = uv;
        vLightUv = lightUv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader:`
      precision highp float;
      precision highp sampler2D;
      uniform sampler2D map;
      uniform float opacity;
      uniform vec4 light;
      in vec2 vUv;
      in vec2 vLightUv;
      out vec4 color;
      ${localLightFragment}
      void main() {
        color = texture(map, vUv);
        if (color.a <= 0.0) discard;
        color.rgb = localLight(clamp(color.rgb, 0.0, 1.0), vLightUv, light);
        color.a *= opacity;
      }
    `,
    transparent:true,
    depthTest:false,
    depthWrite:false,
    toneMapped:false,
    side:THREE.DoubleSide,
  });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.matrixAutoUpdate = false;
  mesh.frustumCulled = false;
  mesh.renderOrder = order;
  const surfaceScene = new THREE.Scene();
  surfaceScene.add(mesh);
  mesh.userData.surfaceScene = surfaceScene;
  resources.push(geometry, material);
  return {mesh, material};
}

function dataTexture(values, count, renderer, resources) {
  const limit = renderer.capabilities.maxTextureSize;
  const width = Math.min(limit, Math.max(1, count));
  const height = Math.ceil(Math.max(1, count) / width);
  if (height > limit) throw new Error('Mesh skin data exceeds the GPU texture limit');
  const data = new Float32Array(width * height);
  if (values) data.set(values);
  const texture = new THREE.DataTexture(data, width, height, THREE.RedFormat, THREE.FloatType);
  texture.colorSpace = THREE.NoColorSpace;
  texture.minFilter = THREE.NearestFilter;
  texture.magFilter = THREE.NearestFilter;
  texture.wrapS = texture.wrapT = THREE.ClampToEdgeWrapping;
  texture.generateMipmaps = false;
  texture.needsUpdate = true;
  resources.push(texture);
  return {texture, data, width};
}

function makeSkinnedGrid(node, map, order, renderer, resources) {
  const [cols, rows] = node.mesh.grid;
  const boneCount = node.mesh.bones.length;
  const vertexCount = (cols + 1) * (rows + 1);
  if (cols < 1 || rows < 1 || boneCount < 1 || node.mesh.weights.length !== vertexCount ||
      node.mesh.weights.some(row => row.length !== boneCount))
    throw new Error(`Mesh ${node.key} has invalid grid weights`);
  const positions = new Float32Array(vertexCount * 3);
  const uvs = new Float32Array(vertexCount * 2);
  const lightUvs = new Float32Array(vertexCount * 2);
  const vertexIds = new Float32Array(vertexCount);
  const [left, top, right, bottom] = node.box;
  for (let y = 0; y <= rows; y++) for (let x = 0; x <= cols; x++) {
    const index = y * (cols + 1) + x;
    positions[index * 3] = left + x / cols * (right - left);
    positions[index * 3 + 1] = top + y / rows * (bottom - top);
    positions[index * 3 + 2] = node.plane_depth ?? 0;
    uvs[index * 2] = x / cols;
    uvs[index * 2 + 1] = 1 - y / rows;
    lightUvs[index * 2] = x / cols;
    lightUvs[index * 2 + 1] = y / rows;
    vertexIds[index] = index;
  }
  const indices = new Uint32Array(cols * rows * 6);
  for (let y = 0; y < rows; y++) for (let x = 0; x < cols; x++) {
    const index = y * (cols + 1) + x;
    indices.set(
      [index, index + 1, index + cols + 1,
       index + 1, index + cols + 2, index + cols + 1],
      (y * cols + x) * 6,
    );
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
  geometry.setAttribute('lightUv', new THREE.BufferAttribute(lightUvs, 2));
  geometry.setAttribute('vertexId', new THREE.BufferAttribute(vertexIds, 1));
  geometry.setIndex(new THREE.BufferAttribute(indices, 1));
  geometry.computeBoundingSphere();

  const weights = Float32Array.from(node.mesh.weights.flat());
  const weightStore = dataTexture(weights, weights.length, renderer, resources);
  const boneStore = dataTexture(null, boneCount * 6, renderer, resources);
  const material = new THREE.RawShaderMaterial({
    glslVersion:THREE.GLSL3,
    uniforms:{
      map:{value:map},
      opacity:{value:1},
      light:{value:new THREE.Vector4()},
      weightMap:{value:weightStore.texture},
      boneMap:{value:boneStore.texture},
    },
    vertexShader:`
      precision highp float;
      precision highp sampler2D;
      in vec3 position;
      in vec2 uv;
      in vec2 lightUv;
      in float vertexId;
      uniform mat4 modelViewMatrix;
      uniform mat4 projectionMatrix;
      uniform sampler2D weightMap;
      uniform sampler2D boneMap;
      out vec2 vUv;
      out vec2 vLightUv;
      const int BONE_COUNT = ${boneCount};
      const int WEIGHT_WIDTH = ${weightStore.width};
      const int BONE_WIDTH = ${boneStore.width};

      ivec2 address(int index, int width) {
        return ivec2(index - index / width * width, index / width);
      }

      void main() {
        vec2 skinned = vec2(0.0);
        int vertex = int(vertexId + 0.5);
        for (int bone = 0; bone < BONE_COUNT; bone++) {
          float weight = texelFetch(weightMap, address(vertex * BONE_COUNT + bone, WEIGHT_WIDTH), 0).r;
          int offset = bone * 6;
          float a = texelFetch(boneMap, address(offset, BONE_WIDTH), 0).r;
          float c = texelFetch(boneMap, address(offset + 1, BONE_WIDTH), 0).r;
          float e = texelFetch(boneMap, address(offset + 2, BONE_WIDTH), 0).r;
          float b = texelFetch(boneMap, address(offset + 3, BONE_WIDTH), 0).r;
          float d = texelFetch(boneMap, address(offset + 4, BONE_WIDTH), 0).r;
          float f = texelFetch(boneMap, address(offset + 5, BONE_WIDTH), 0).r;
          skinned += weight * vec2(
            a * position.x + c * position.y + e,
            b * position.x + d * position.y + f
          );
        }
        vUv = uv;
        vLightUv = lightUv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(skinned, position.z, 1.0);
      }
    `,
    fragmentShader:`
      precision highp float;
      precision highp sampler2D;
      uniform sampler2D map;
      uniform float opacity;
      uniform vec4 light;
      in vec2 vUv;
      in vec2 vLightUv;
      out vec4 color;
      ${localLightFragment}
      void main() {
        color = texture(map, vUv);
        if (color.a <= 0.0) discard;
        color.rgb = localLight(clamp(color.rgb, 0.0, 1.0), vLightUv, light);
        color.a *= opacity;
      }
    `,
    transparent:true,
    depthTest:false,
    depthWrite:false,
    toneMapped:false,
    side:THREE.DoubleSide,
  });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.matrixAutoUpdate = false;
  mesh.frustumCulled = false;
  mesh.renderOrder = order;
  const surfaceScene = new THREE.Scene();
  surfaceScene.add(mesh);
  mesh.userData.surfaceScene = surfaceScene;
  resources.push(geometry, material);

  function updateBones(matrices) {
    if (matrices.length !== boneCount) throw new Error(`Mesh ${node.key} has invalid bone matrices`);
    boneStore.data.fill(0);
    for (const [index, matrix] of matrices.entries()) {
      const offset = index * 6;
      boneStore.data[offset] = matrix.a;
      boneStore.data[offset + 1] = matrix.c;
      boneStore.data[offset + 2] = matrix.e;
      boneStore.data[offset + 3] = matrix.b;
      boneStore.data[offset + 4] = matrix.d;
      boneStore.data[offset + 5] = matrix.f;
    }
    boneStore.texture.needsUpdate = true;
  }

  function vertex(index, matrices) {
    const x = positions[index * 3], y = positions[index * 3 + 1];
    let px = 0, py = 0;
    for (let bone = 0; bone < boneCount; bone++) {
      const weight = node.mesh.weights[index][bone], matrix = matrices[bone];
      px += weight * (matrix.a * x + matrix.c * y + matrix.e);
      py += weight * (matrix.b * x + matrix.d * y + matrix.f);
    }
    return [px, py];
  }

  function point([x, y], matrices) {
    const u = Math.max(0, Math.min(cols, (x - left) * cols / (right - left)));
    const v = Math.max(0, Math.min(rows, (y - top) * rows / (bottom - top)));
    const col = Math.min(cols - 1, Math.floor(u)), row = Math.min(rows - 1, Math.floor(v));
    const a = u - col, b = v - row, index = row * (cols + 1) + col;
    const triangle = a + b <= 1
      ? [[index, 1 - a - b], [index + 1, a], [index + cols + 1, b]]
      : [[index + 1, 1 - b], [index + cols + 2, a + b - 1],
         [index + cols + 1, 1 - a]];
    let px = 0, py = 0;
    for (const [vertexId, weight] of triangle) {
      const [vx, vy] = vertex(vertexId, matrices);
      px += weight * vx;
      py += weight * vy;
    }
    return [px, py];
  }

  return {mesh, material, updateBones, point};
}

function projectPoint(camera, width, height, [x, y], depth = 0) {
  const point = new THREE.Vector3(x, y, depth).project(camera);
  return {x:(point.x + 1) * width / 2, y:(1 - point.y) * height / 2};
}

function makeSeamSurface(spec, map, order, resources) {
  const capacity = Math.max(1, spec.rows * 6);
  const positions = new Float32Array(capacity * 3);
  const uvs = new Float32Array(capacity * 2);
  const geometry = new THREE.BufferGeometry();
  const position = new THREE.BufferAttribute(positions, 3);
  const uv = new THREE.BufferAttribute(uvs, 2);
  position.setUsage(THREE.DynamicDrawUsage);
  uv.setUsage(THREE.DynamicDrawUsage);
  geometry.setAttribute('position', position);
  geometry.setAttribute('uv', uv);
  geometry.setDrawRange(0, 0);
  const material = new THREE.MeshBasicMaterial({
    map,
    transparent:true,
    depthTest:false,
    depthWrite:false,
    toneMapped:false,
    side:THREE.DoubleSide,
  });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.matrixAutoUpdate = false;
  mesh.frustumCulled = false;
  mesh.renderOrder = order;
  mesh.visible = false;
  const surfaceScene = new THREE.Scene();
  surfaceScene.add(mesh);
  mesh.userData.surfaceScene = surfaceScene;
  resources.push(geometry, material);

  function update(vertices) {
    const count = vertices ? vertices.length / 4 : 0;
    if (count > capacity) throw new Error(`Seam ${spec.key} exceeds its compiled topology`);
    for (let index = 0; index < count; index++) {
      positions[index * 3] = vertices[index * 4];
      positions[index * 3 + 1] = vertices[index * 4 + 1];
      positions[index * 3 + 2] = 0;
      uvs[index * 2] = vertices[index * 4 + 2];
      uvs[index * 2 + 1] = 1 - vertices[index * 4 + 3];
    }
    position.needsUpdate = true;
    uv.needsUpdate = true;
    geometry.setDrawRange(0, count);
    mesh.visible = count > 0;
  }

  return {mesh, material, update};
}

async function loadTexture(loader, file, url, resources) {
  if (!file) return null;
  const texture = await loader.loadAsync(new URL(file, url).href);
  texture.colorSpace = THREE.NoColorSpace;
  texture.generateMipmaps = false;
  texture.minFilter = THREE.LinearFilter;
  texture.magFilter = THREE.LinearFilter;
  texture.wrapS = texture.wrapT = THREE.ClampToEdgeWrapping;
  resources.push(texture);
  return texture;
}

function createCompositor(renderer, width, height, resources) {
  const sourceTarget = makeTarget(width, height, resources);
  const casterTarget = makeTarget(width, height, resources);
  const maskTarget = makeTarget(width, height, resources);
  const matteTargets = [makeTarget(width, height, resources), makeTarget(width, height, resources)];
  const accumulators = [makeTarget(width, height, resources), makeTarget(width, height, resources)];
  const shadowTarget = makeTarget(width, height, resources);
  const blurTargets = [makeTarget(width, height, resources), makeTarget(width, height, resources)];
  const shadowReceiverTargets = [
    makeTarget(width, height, resources),
    makeTarget(width, height, resources),
  ];
  const geometry = new THREE.PlaneGeometry(2, 2);
  const camera = new THREE.Camera();
  const scene = new THREE.Scene();
  const material = new THREE.RawShaderMaterial({
    uniforms:{
      backdrop:{value:null},
      source:{value:null},
      mode:{value:0},
    },
    vertexShader:`
      precision highp float;
      attribute vec3 position;
      attribute vec2 uv;
      varying vec2 vUv;
      void main() {
        vUv = uv;
        gl_Position = vec4(position.xy, 0.0, 1.0);
      }
    `,
    fragmentShader:`
      precision highp float;
      uniform sampler2D backdrop;
      uniform sampler2D source;
      uniform int mode;
      varying vec2 vUv;

      vec3 straight(vec4 color) {
        return color.a > 0.000001 ? color.rgb / color.a : vec3(0.0);
      }

      void main() {
        vec4 below = texture2D(backdrop, vUv);
        vec4 above = texture2D(source, vUv);
        if (mode == 0) {
          gl_FragColor = above + below * (1.0 - above.a);
          return;
        }
        if (mode == 1) {
          gl_FragColor = min(vec4(1.0), above + below);
          return;
        }
        vec3 cb = straight(below);
        vec3 cs = straight(above);
        vec3 blend = mode == 2 ? cb * cs : cb + cs - cb * cs;
        float alpha = above.a + below.a * (1.0 - above.a);
        vec3 color = below.rgb * (1.0 - above.a) +
          above.rgb * (1.0 - below.a) + below.a * above.a * blend;
        gl_FragColor = vec4(color, alpha);
      }
    `,
    depthTest:false,
    depthWrite:false,
    transparent:false,
    toneMapped:false,
  });
  const quad = new THREE.Mesh(geometry, material);
  quad.frustumCulled = false;
  scene.add(quad);
  const copyMaterial = new THREE.RawShaderMaterial({
    uniforms:{source:{value:null}},
    vertexShader:material.vertexShader,
    fragmentShader:`
      precision highp float;
      uniform sampler2D source;
      varying vec2 vUv;
      void main() { gl_FragColor = texture2D(source, vUv); }
    `,
    depthTest:false,
    depthWrite:false,
    transparent:false,
    toneMapped:false,
  });
  const matteMaterial = new THREE.RawShaderMaterial({
    uniforms:{
      source:{value:null},
      mask:{value:null},
      keep:{value:0},
    },
    vertexShader:material.vertexShader,
    fragmentShader:`
      precision highp float;
      uniform sampler2D source;
      uniform sampler2D mask;
      uniform int keep;
      varying vec2 vUv;
      void main() {
        vec4 surface = texture2D(source, vUv);
        float alpha = texture2D(mask, vUv).a;
        float factor = keep == 1 ? alpha : 1.0 - alpha;
        gl_FragColor = surface * factor;
      }
    `,
    depthTest:false,
    depthWrite:false,
    transparent:false,
    toneMapped:false,
  });
  const shadowMaterial = new THREE.RawShaderMaterial({
    uniforms:{
      source:{value:null},
      size:{value:new THREE.Vector2(width, height)},
      anchor:{value:new THREE.Vector2()},
      offset:{value:new THREE.Vector2()},
      scale:{value:new THREE.Vector2(1, 1)},
      color:{value:new THREE.Vector3()},
      opacity:{value:1},
    },
    vertexShader:material.vertexShader,
    fragmentShader:`
      precision highp float;
      uniform sampler2D source;
      uniform vec2 size;
      uniform vec2 anchor;
      uniform vec2 offset;
      uniform vec2 scale;
      uniform vec3 color;
      uniform float opacity;
      varying vec2 vUv;
      void main() {
        vec2 pixel = vec2(vUv.x * size.x, (1.0 - vUv.y) * size.y);
        vec2 point = anchor + (pixel - anchor - offset) / scale;
        if (point.x < 0.0 || point.y < 0.0 || point.x > size.x || point.y > size.y) {
          gl_FragColor = vec4(0.0);
          return;
        }
        vec2 sampleUv = vec2(point.x / size.x, 1.0 - point.y / size.y);
        float alpha = texture2D(source, sampleUv).a * opacity;
        gl_FragColor = vec4(color * alpha, alpha);
      }
    `,
    depthTest:false,
    depthWrite:false,
    transparent:false,
    toneMapped:false,
  });
  const blurMaterial = new THREE.RawShaderMaterial({
    uniforms:{
      source:{value:null},
      texel:{value:new THREE.Vector2(1 / width, 1 / height)},
      direction:{value:new THREE.Vector2()},
      radius:{value:0},
    },
    vertexShader:material.vertexShader,
    fragmentShader:`
      precision highp float;
      uniform sampler2D source;
      uniform vec2 texel;
      uniform vec2 direction;
      uniform float radius;
      varying vec2 vUv;
      void main() {
        if (radius <= 0.01) {
          gl_FragColor = texture2D(source, vUv);
          return;
        }
        vec4 sum = vec4(0.0);
        float total = 0.0;
        float stride = max(1.0, radius * 3.0 / 32.0);
        float extent = radius * 3.0 + 0.5;
        for (int tap = -32; tap <= 32; tap++) {
          float distance = float(tap) * stride;
          if (abs(distance) <= extent) {
            float weight = exp(-0.5 * distance * distance / (radius * radius));
            vec2 sampleUv = vUv + direction * texel * distance;
            if (sampleUv.x >= 0.0 && sampleUv.y >= 0.0 &&
                sampleUv.x <= 1.0 && sampleUv.y <= 1.0)
              sum += texture2D(source, sampleUv) * weight;
            total += weight;
          }
        }
        gl_FragColor = sum / total;
      }
    `,
    depthTest:false,
    depthWrite:false,
    transparent:false,
    toneMapped:false,
  });
  const shadowAtopMaterial = new THREE.RawShaderMaterial({
    uniforms:{
      receiver:{value:null},
      shadow:{value:null},
    },
    vertexShader:material.vertexShader,
    fragmentShader:`
      precision highp float;
      uniform sampler2D receiver;
      uniform sampler2D shadow;
      varying vec2 vUv;
      void main() {
        vec4 below = texture2D(receiver, vUv);
        vec4 above = texture2D(shadow, vUv);
        gl_FragColor = vec4(
          above.rgb * below.a + below.rgb * (1.0 - above.a),
          below.a
        );
      }
    `,
    depthTest:false,
    depthWrite:false,
    transparent:false,
    toneMapped:false,
  });
  const modes = new Map([['normal', 0], ['add', 1], ['multiply', 2], ['screen', 3]]);
  resources.push(
    geometry,
    material,
    copyMaterial,
    matteMaterial,
    shadowMaterial,
    blurMaterial,
    shadowAtopMaterial,
  );

  function renderQuad(quadMaterial, target) {
    const prior = quad.material;
    quad.material = quadMaterial;
    renderer.setRenderTarget(target);
    renderer.clear();
    renderer.render(scene, camera);
    quad.material = prior;
  }

  function copy(texture, target) {
    copyMaterial.uniforms.source.value = texture;
    renderQuad(copyMaterial, target);
  }

  function renderSurface(mesh, surfaceCamera, target, forceVisible = false) {
    const visible = mesh.visible;
    if (forceVisible) mesh.visible = true;
    renderer.setRenderTarget(target);
    renderer.clear();
    renderer.render(mesh.userData.surfaceScene, surfaceCamera);
    mesh.visible = visible;
  }

  function matteSurface(texture, mattes, surfaceCamera, time) {
    let result = texture, index = 0;
    for (const matte of mattes ?? []) {
      if (!isActive(matte.windows ?? [], time)) continue;
      renderSurface(matte.mask, surfaceCamera, maskTarget, true);
      matteMaterial.uniforms.source.value = result;
      matteMaterial.uniforms.mask.value = maskTarget.texture;
      matteMaterial.uniforms.keep.value = matte.operation === 'keep' ? 1 : 0;
      const target = matteTargets[index % matteTargets.length];
      renderQuad(matteMaterial, target);
      result = target.texture;
      index++;
    }
    return result;
  }

  function applyShadows(receiverTexture, projections, surfaceCamera, time) {
    copy(receiverTexture, shadowReceiverTargets[0]);
    let read = shadowReceiverTargets[0], write = shadowReceiverTargets[1];
    for (const projection of projections ?? []) {
      const shadow = projection.spec;
      if (!shadow.opacity) continue;
      renderSurface(projection.caster.mesh, surfaceCamera, casterTarget);
      const caster = matteSurface(
        casterTarget.texture,
        projection.caster.mattes,
        surfaceCamera,
        time,
      );
      shadowMaterial.uniforms.source.value = caster;
      shadowMaterial.uniforms.anchor.value.fromArray(projection.anchor);
      shadowMaterial.uniforms.offset.value.fromArray(shadow.offset);
      shadowMaterial.uniforms.scale.value.fromArray(shadow.scale);
      shadowMaterial.uniforms.color.value.fromArray(shadow.color).multiplyScalar(1 / 255);
      shadowMaterial.uniforms.opacity.value = shadow.opacity;
      renderQuad(shadowMaterial, shadowTarget);
      let projected = shadowTarget.texture;
      if (shadow.blur > 0) {
        blurMaterial.uniforms.source.value = projected;
        blurMaterial.uniforms.direction.value.set(1, 0);
        blurMaterial.uniforms.radius.value = shadow.blur;
        renderQuad(blurMaterial, blurTargets[0]);
        blurMaterial.uniforms.source.value = blurTargets[0].texture;
        blurMaterial.uniforms.direction.value.set(0, 1);
        renderQuad(blurMaterial, blurTargets[1]);
        projected = blurTargets[1].texture;
      }
      shadowAtopMaterial.uniforms.receiver.value = read.texture;
      shadowAtopMaterial.uniforms.shadow.value = projected;
      renderQuad(shadowAtopMaterial, write);
      [read, write] = [write, read];
    }
    return read;
  }

  function compose(units, surfaceCamera, destination = undefined, time = 0) {
    renderer.setRenderTarget(accumulators[0]);
    renderer.clear();
    let read = accumulators[0], write = accumulators[1];
    for (const unit of units) {
      if (!unit.mesh.visible) continue;
      renderSurface(unit.mesh, surfaceCamera, sourceTarget);
      let surface = matteSurface(sourceTarget.texture, unit.mattes, surfaceCamera, time);
      if (unit.shadows?.length)
        surface = applyShadows(surface, unit.shadows, surfaceCamera, time).texture;
      material.uniforms.backdrop.value = read.texture;
      material.uniforms.source.value = surface;
      material.uniforms.mode.value = modes.get(unit.blend ?? 'normal');
      renderer.setRenderTarget(write);
      renderer.clear();
      renderer.render(scene, camera);
      [read, write] = [write, read];
      if (unit.accumulatedShadows?.length) {
        const shaded = applyShadows(read.texture, unit.accumulatedShadows, surfaceCamera, time);
        copy(shaded.texture, write);
        [read, write] = [write, read];
      }
    }
    if (destination !== undefined) copy(read.texture, destination);
    return read.texture;
  }

  return {compose, copy};
}

async function buildGraph({spec, url, renderer, loader, resources, ancestry, output}) {
  const finalCamera = makeCamera();
  const stageCamera = makeCamera();
  const groups = new Map(spec.groups.map(node => [node.key, node]));
  const isolated = new Set(spec.groups.filter(node =>
    node.opacity?.length || (node.blend_mode ?? 'normal') !== 'normal' ||
    (node.plane_depth ?? 0) !== 0).map(node => node.key));
  const minimumDepth = Math.min(
    0,
    ...spec.groups.map(node => node.plane_depth ?? 0),
    ...spec.layers.map(node => node.plane_depth ?? 0),
  );
  function nearestScope(node) {
    let parent = node.parent;
    while (parent != null) {
      if (isolated.has(parent)) return parent;
      parent = groups.get(parent)?.parent;
    }
    return null;
  }
  const descendants = new Map([...isolated].map(key => [key, []]));
  for (const [index, layer] of spec.layers.entries()) {
    let parent = layer.parent;
    while (parent != null) {
      if (isolated.has(parent)) descendants.get(parent).push(index);
      parent = groups.get(parent)?.parent;
    }
  }
  const rootTarget = output ? makeTarget(spec.width, spec.height, resources) : null;
  const rootStageTarget = makeTarget(spec.width, spec.height, resources);
  const scopes = new Map([[null, {target:rootStageTarget}]]);
  for (const key of isolated)
    scopes.set(key, {target:makeTarget(spec.width, spec.height, resources)});
  const compositor = createCompositor(renderer, spec.width, spec.height, resources);

  const [backTexture, backingTexture] = await Promise.all([
    loadTexture(loader, spec.background, url, resources),
    loadTexture(loader, spec.backing, url, resources),
  ]);
  const seamTextures = await Promise.all(
    (spec.seams ?? []).map(seam => loadTexture(loader, seam.file, url, resources)),
  );
  const sourceSurfaces = await Promise.all(spec.layers.map(async node => {
    if (!node.scene_id) return {
      map:await loadTexture(loader, node.file, url, resources),
      child:null,
      premultiplied:false,
    };
    if (ancestry.includes(node.scene_id))
      throw new Error(`Cyclic scene source dependency at ${node.scene_id}`);
    const childUrl = new URL(`/scenes/${node.scene_id}/scene.json`, url);
    const childSpec = await fetchSpec(childUrl);
    const child = await buildGraph({
      spec:childSpec,
      url:childUrl,
      renderer,
      loader,
      resources,
      ancestry:[...ancestry, node.scene_id],
      output:true,
    });
    return {map:child.output.texture, child, premultiplied:true};
  }));

  const backing = backingTexture
    ? makePlane(null, backingTexture, spec.width, spec.height, -2, resources) : null;
  const background = makePlane(null, backTexture, spec.width, spec.height, -1, resources);
  const fullCanvas = new THREE.Matrix4().makeTranslation(spec.width / 2, spec.height / 2, 0);
  background.matrix.copy(fullCanvas);
  if (backing) backing.matrix.copy(fullCanvas);
  const finalSurface = makePlane(
    null,
    rootStageTarget.texture,
    spec.width,
    spec.height,
    0,
    resources,
    true,
  );
  finalSurface.matrix.copy(fullCanvas);

  const layers = spec.layers.map((node, index) => {
    const width = node.box[2] - node.box[0], height = node.box[3] - node.box[1];
    const scope = nearestScope(node);
    const surface = sourceSurfaces[index];
    const skin = node.mesh
      ? makeSkinnedGrid(node, surface.map, index, renderer, resources) : null;
    const lit = !skin && node.appearance?.light?.length
      ? makeLitPlane(surface.map, width, height, index, resources) : null;
    const mesh = skin?.mesh ?? lit?.mesh ?? makePlane(
      null,
      surface.map,
      width,
      height,
      index,
      resources,
      surface.premultiplied,
    );
    mesh.visible = !node.matte_only;
    return {
      node,
      mesh,
      skin,
      lit,
      scope,
      child:surface.child,
      premultiplied:surface.premultiplied,
      center:skin ? null : new THREE.Matrix4().makeTranslation(
          (node.box[0] + node.box[2]) / 2,
          (node.box[1] + node.box[3]) / 2,
          node.plane_depth ?? 0,
        ),
    };
  });
  const layerByKey = new Map(layers.map(layer => [layer.node.key, layer]));
  const seamLinks = (spec.seams ?? []).map((seam, index) => {
    const surface = makeSeamSurface(seam, seamTextures[index], index, resources);
    return {
      spec:seam,
      surface,
      scope:layerByKey.get(seam.a.layer).scope,
      unit:{
        kind:'seam', key:`@seam:${seam.key}`, order:index,
        mesh:surface.mesh, blend:'normal',
      },
      status:{key:seam.key, status:'inactive'},
    };
  });
  const mattesByTarget = new Map();
  for (const matte of spec.mattes ?? []) {
    if (!mattesByTarget.has(matte.target)) mattesByTarget.set(matte.target, []);
    mattesByTarget.get(matte.target).push({
      operation:matte.operation,
      windows:matte.windows,
      mask:layerByKey.get(matte.mask).mesh,
    });
  }
  const groupSurfaces = new Map();
  for (const key of isolated) {
    const node = groups.get(key);
    const scope = nearestScope(node);
    const mesh = makePlane(
      null,
      scopes.get(key).target.texture,
      spec.width,
      spec.height,
      Math.min(...descendants.get(key)),
      resources,
      true,
    );
    mesh.matrix.makeTranslation(
      spec.width / 2,
      spec.height / 2,
      node.plane_depth ?? 0,
    );
    groupSurfaces.set(key, {node, mesh, scope});
  }
  const unitsByScope = new Map();
  const unitByKey = new Map();
  const addUnit = (scope, unit) => {
    if (!unitsByScope.has(scope)) unitsByScope.set(scope, []);
    unitsByScope.get(scope).push(unit);
    unitByKey.set(unit.key, unit);
  };
  if (backing)
    addUnit(null, {kind:'backdrop', key:'@backing', order:-2, mesh:backing, blend:'normal'});
  const backgroundUnit = {
    kind:'backdrop', key:'@background', order:-1, mesh:background, blend:'normal',
  };
  addUnit(null, backgroundUnit);
  for (const [index, layer] of layers.entries())
    addUnit(layer.scope, {
      kind:'layer', key:layer.node.key, index, order:index, mesh:layer.mesh,
      blend:layer.node.blend_mode ?? 'normal', mattes:mattesByTarget.get(layer.node.key),
    });
  for (const [key, group] of groupSurfaces)
    addUnit(group.scope, {
      kind:'group', key, order:Math.min(...descendants.get(key)), mesh:group.mesh,
      blend:group.node.blend_mode ?? 'normal',
    });
  const shadowLinks = (spec.shadows ?? []).map(shadow => {
    const projection = {
      spec:shadow,
      caster:unitByKey.get(shadow.caster),
      anchor:[0, 0],
    };
    if (shadow.receiver == null) {
      if (!backgroundUnit.accumulatedShadows) backgroundUnit.accumulatedShadows = [];
      backgroundUnit.accumulatedShadows.push(projection);
    } else {
      const receiver = unitByKey.get(shadow.receiver);
      if (!receiver.shadows) receiver.shadows = [];
      receiver.shadows.push(projection);
    }
    return projection;
  });
  for (const units of unitsByScope.values()) units.sort((a, b) => a.order - b.order);
  const isolationDepth = key => {
    let depth = 0, parent = groups.get(key)?.parent;
    while (parent != null) {
      if (isolated.has(parent)) depth++;
      parent = groups.get(parent)?.parent;
    }
    return depth;
  };
  const renderGroups = [...isolated].sort((a, b) => isolationDepth(b) - isolationDepth(a));
  function stageSurfacePoint(key, [x, y], frame) {
    const layer = layerByKey.get(key), node = layer.node;
    let point = [
      x + node.box[0] - node.source_box[0],
      y + node.box[1] - node.source_box[1],
    ];
    if (layer.skin) point = layer.skin.point(point, frame.bones(node));
    const transformed = frame.world(key).transformPoint({x:point[0], y:point[1]});
    return projectPoint(
      stageCamera,
      spec.width,
      spec.height,
      [transformed.x, transformed.y],
      node.plane_depth ?? 0,
    );
  }
  const evaluator = createSceneEvaluator(spec, {surfacePoint:stageSurfacePoint});
  let showBacking = Boolean(backing);
  let lastFrame = null;
  let lastSeams = [];

  function displayedSurfacePoint(key, xy, frame) {
    const node = layerByKey.get(key).node;
    let point = stageSurfacePoint(key, xy, frame);
    let parent = node.parent;
    while (parent != null) {
      const group = groups.get(parent);
      if (isolated.has(parent)) point = projectPoint(
        stageCamera,
        spec.width,
        spec.height,
        [point.x, point.y],
        group.plane_depth ?? 0,
      );
      parent = group?.parent;
    }
    return projectPoint(finalCamera, spec.width, spec.height, [point.x, point.y]);
  }

  function render(time) {
    const clock = Math.max(0, Math.min(spec.duration, time));
    const frame = evaluator.evaluate(clock);
    const focal = spec.camera?.focal ?? 2 * Math.max(spec.width, spec.height);
    const distance = focal - frame.camera.dolly;
    const far = Math.max(1000, distance - minimumDepth + 1000);
    configureCameras(spec, frame, finalCamera, stageCamera, far);
    for (const {node, child} of layers) {
      if (child) child.render(frame.localTime(node.key));
    }
    for (const {node, mesh, center, premultiplied, skin, lit} of layers) {
      const opacity = frame.opacity(node.key);
      const light = frame.light(node.key) ?? [0, 0, 0, 0];
      if (skin) {
        skin.updateBones(frame.bones(node));
        mesh.matrix.copy(matrixFromDOM(frame.world(node.key)));
        skin.material.uniforms.opacity.value = opacity;
        skin.material.uniforms.light.value.fromArray(light);
      } else if (lit) {
        mesh.matrix.copy(matrixFromDOM(frame.world(node.key))).multiply(center);
        lit.material.uniforms.opacity.value = opacity;
        lit.material.uniforms.light.value.fromArray(light);
      } else {
        mesh.matrix.copy(matrixFromDOM(frame.world(node.key))).multiply(center);
        mesh.material.opacity = opacity;
        mesh.material.color.setScalar(premultiplied ? opacity : 1);
      }
    }
    for (const [key, {mesh}] of groupSurfaces) {
      const opacity = frame.opacity(key);
      mesh.material.opacity = opacity;
      mesh.material.color.setScalar(opacity);
    }
    for (const seam of seamLinks) {
      const {vertices, ...status} = evaluateSeam(seam.spec, frame);
      seam.status = {key:seam.spec.key, ...status};
      seam.surface.update(vertices);
      const a = layerByKey.get(seam.spec.a.layer).node;
      const b = layerByKey.get(seam.spec.b.layer).node;
      seam.surface.material.opacity = Math.min(frame.opacity(a.key), frame.opacity(b.key));
      seam.unit.blend = a.blend_mode === b.blend_mode ? a.blend_mode ?? 'normal' : 'normal';
    }
    for (const shadow of shadowLinks) {
      const node = layerByKey.get(shadow.spec.caster).node;
      const [left, top, right, bottom] = node.source_box;
      const origin = shadow.spec.origin ?? [(left + right) / 2, (top + bottom) / 2];
      const anchor = stageSurfacePoint(node.key, origin, frame);
      shadow.anchor = [anchor.x, anchor.y];
    }
    lastSeams = seamLinks.map(seam => seam.status);
    const orderedUnits = scope => {
      const base = (unitsByScope.get(scope) ?? []).slice().sort((a, b) =>
        (a.kind === 'layer' ? frame.depth(a.key, a.order) : a.order) -
        (b.kind === 'layer' ? frame.depth(b.key, b.order) : b.order) || a.order - b.order);
      const index = new Map(base.map((unit, rank) => [unit.key, rank]));
      const before = new Map();
      for (const seam of seamLinks) {
        if (seam.scope !== scope || !seam.surface.mesh.visible) continue;
        const rank = Math.min(index.get(seam.spec.a.layer), index.get(seam.spec.b.layer));
        if (!before.has(rank)) before.set(rank, []);
        before.get(rank).push(seam.unit);
      }
      return base.flatMap((unit, rank) => [...(before.get(rank) ?? []), unit]);
    };
    if (backing) backing.visible = showBacking;
    for (const key of renderGroups) {
      const scope = scopes.get(key);
      compositor.compose(orderedUnits(key), stageCamera, scope.target, frame.time);
    }
    compositor.compose(orderedUnits(null), stageCamera, rootStageTarget, frame.time);
    renderer.setRenderTarget(rootTarget);
    renderer.clear();
    renderer.render(finalSurface.userData.surfaceScene, finalCamera);
    lastFrame = frame;
    return frame;
  }

  return {
    spec,
    output:rootTarget,
    evaluator,
    render,
    seamStatus() { return structuredClone(lastSeams); },
    surfacePoint(key, xy) {
      if (!lastFrame || !layerByKey.has(key)) throw new Error('Unknown cutout');
      return displayedSurfacePoint(key, xy, lastFrame);
    },
    setBacking(visible) { showBacking = Boolean(visible); },
  };
}

export async function loadThreeScene(canvas, source) {
  const url = new URL(source, document.baseURI);
  const spec = await fetchSpec(url);
  const renderer = new THREE.WebGLRenderer({
    canvas,
    alpha:true,
    antialias:false,
    premultipliedAlpha:true,
    preserveDrawingBuffer:true,
  });
  renderer.setPixelRatio(1);
  renderer.setSize(spec.width, spec.height, false);
  renderer.setClearColor(0x000000, 0);
  // Match Canvas2D's direct 8-bit channel compositing. Linear-light output can
  // be introduced later as an explicit backend choice.
  renderer.outputColorSpace = THREE.LinearSRGBColorSpace;
  renderer.toneMapping = THREE.NoToneMapping;
  const loader = new THREE.TextureLoader();
  const resources = [];
  const graph = await buildGraph({
    spec,
    url,
    renderer,
    loader,
    resources,
    ancestry:[],
    output:false,
  });
  let clock = 0;
  function seek(time) {
    if (!Number.isFinite(time)) throw new Error('Invalid seek time');
    clock = Math.max(0, Math.min(spec.duration, time));
    graph.render(clock);
  }
  seek(0);
  return {
    spec,
    seek,
    seamStatus() { return graph.seamStatus(); },
    pose(key) { return structuredClone(graph.evaluator.evaluate(clock).pose(key)); },
    localTime(key) { return graph.evaluator.evaluate(clock).localTime(key); },
    surfacePoint(key, xy) {
      const node = spec.layers.find(layer => layer.key === key);
      if (!node || xy?.length !== 2 || !xy.every(Number.isFinite))
        throw new Error('Expected a cutout key and a source pixel point');
      const [left, top, right, bottom] = node.source_box;
      if (xy[0] < left || xy[0] > right || xy[1] < top || xy[1] > bottom)
        throw new Error('Point is outside the cutout raster');
      const point = graph.surfacePoint(key, xy);
      return [point.x, point.y];
    },
    backing(visible) { graph.setBacking(visible); seek(clock); },
    dispose() {
      for (const resource of resources) resource.dispose();
      renderer.dispose();
    },
  };
}
