import {assertLocalTime, assertTrack, assertWindows} from './cutout-timeline.mjs';
import type {CameraTrack, DepthKey, LightKey, LocalTime, Pose, ScalarKey, Timed, Window} from './cutout-timeline.mjs';

type PoseKey = Timed & Pose;
type SurfaceMount = {layer: string};
type Blend = 'normal' | 'add' | 'multiply' | 'screen' | 'overlay' | 'darken' | 'lighten' |
  'color-dodge' | 'color-burn' | 'hard-light' | 'soft-light' | 'difference' | 'exclusion' |
  'hue' | 'saturation' | 'color' | 'luminosity';
type Node = {
  key: string; parent?: string | null; mount?: SurfaceMount | null; motion?: PoseKey[];
  opacity?: ScalarKey[]; blend_mode?: Blend; plane_depth?: number; local_time?: LocalTime | null;
};
type Skin = {bones: {key: string}[]};
type Appearance = {light?: LightKey[]};
type Paint = {source: string};
type Layer = Node & {
  selection?: string | null; source?: string | null; scene?: string | null; edge_matte?: string | null;
  depth?: DepthKey[]; mesh?: Skin | null; appearance?: Appearance | null;
  underlap?: Paint[]; paintover?: Paint[]; matte_only?: boolean;
};
type Shadow = {caster: string; receiver?: string | null};
type Seam = {key: string; a: {layer: string}; b: {layer: string}; windows?: Window[]};
type Matte = {target: string; mask: string; windows?: Window[]};
export type SceneRecipe = {
  base: string; groups: Node[]; layers: Layer[]; duration: number; backing: string | null;
  claim_order: string[]; shadows: Shadow[]; seams: Seam[]; mattes: Matte[]; camera?: CameraTrack | null;
};
type ByKey<T> = Record<string, T>;
export type Revision = {
  replacements?: ByKey<string>; depth?: ByKey<DepthKey[]>; meshes?: ByKey<Skin | null>;
  mounts?: ByKey<SurfaceMount | null>; underlaps?: ByKey<Paint[]>; appearances?: ByKey<Appearance | null>;
  paintovers?: ByKey<Paint[]>; edge_mattes?: ByKey<string | null>; motions?: ByKey<PoseKey[]>;
  backing?: string | null; duration?: number | null; shadows?: Shadow[] | null;
  seams?: Seam[] | null; mattes?: Matte[] | null;
  camera?: CameraTrack | null;
  opacities?: ByKey<ScalarKey[]>; blend_modes?: ByKey<Blend>; plane_depths?: ByKey<number>;
  local_times?: ByKey<LocalTime | null>;
};

// Validate structure here, not in the image worker. The worker can check only
// facts which require source masks, pixels or precomputed rig geometry.
export function checked(scene: SceneRecipe): SceneRecipe {
  const {groups, layers, duration, shadows, seams, mattes, claim_order} = scene;
  if (!Number.isFinite(duration) || duration <= 0) throw new Error('Scene duration must be positive');
  if (!layers.length) throw new Error('Use at least one layer');
  const nodes = [...groups, ...layers];
  const groupKeys = new Set(groups.map(g => g.key));
  const groupByKey = new Map(groups.map(group => [group.key, group]));
  const cutouts = new Set(layers.map(l => l.key));
  const visible = new Set(layers.filter(l => !l.matte_only).map(l => l.key));
  const links = new Map(nodes.map(n => [n.key, [] as string[]]));
  if (links.size !== nodes.length) throw new Error('Duplicate scene keys');
  if (claim_order.length !== cutouts.size || new Set(claim_order).size !== cutouts.size ||
      claim_order.some(key => !cutouts.has(key))) throw new Error('Claim order must list every cutout once');

  const usesLocalTime = (node: Node) => {
    let current: Node | undefined = node;
    const seen = new Set<string>();
    while (current && !seen.has(current.key)) {
      seen.add(current.key);
      if (current.local_time) return true;
      current = current.parent == null ? undefined : groupByKey.get(current.parent);
    }
    return false;
  };
  for (const node of nodes) {
    const edges = links.get(node.key)!;
    if (node.parent != null) {
      if (!groupKeys.has(node.parent)) throw new Error('Each parent must be a group');
      edges.push(node.parent);
    }
    if (node.mount) {
      if (!groupKeys.has(node.key) || node.parent != null || !cutouts.has(node.mount.layer))
        throw new Error('Only an unparented group can mount to a surface layer');
      if (node.motion?.some(pose => pose.move.some(Boolean)))
        throw new Error('A surface mount controls translation; animate a child for offsets');
      edges.push(node.mount.layer);
    }
    assertLocalTime(node.local_time, `Node ${node.key}`);
    const available = usesLocalTime(node) ? null : duration;
    assertTrack(node.motion ?? [], available, 'Motion');
    assertTrack(node.opacity ?? [], available, 'Opacity');
  }
  for (const layer of layers) {
    if ([layer.selection, layer.source, layer.scene].filter(value => value != null).length !== 1)
      throw new Error(`Layer ${layer.key} needs exactly one selection, image source or scene source`);
    if (layer.scene != null && (layer.edge_matte != null || layer.mesh != null ||
        layer.underlap?.length || layer.paintover?.length || layer.matte_only || layer.appearance != null))
      throw new Error(`Scene source ${layer.key} cannot use raster-only layer fields`);
    const available = usesLocalTime(layer) ? null : duration;
    assertTrack(layer.appearance?.light ?? [], available, 'Light');
    assertTrack(layer.depth ?? [], available, 'Depth');
    if (!layer.mesh) continue;
    const bones = layer.mesh.bones.map(bone => bone.key);
    if (new Set(bones).size !== bones.length || bones.some(key => !groupKeys.has(key)))
      throw new Error('Mesh bones must name distinct scene groups');
    if (layer.parent != null) throw new Error('A mesh inherits through its bones; do not give it a parent');
    links.get(layer.key)!.push(...bones);
  }
  const resolved = new Set<string>(), resolving = new Set<string>();
  function visit(key: string) {
    if (resolving.has(key)) throw new Error(`Cyclic scene dependency at ${key}`);
    if (resolved.has(key)) return;
    resolving.add(key);
    for (const dependency of links.get(key)!) visit(dependency);
    resolving.delete(key);
    resolved.add(key);
  }
  for (const key of links.keys()) visit(key);
  const isolated = new Set(groups.filter(group =>
    Boolean(group.opacity?.length) || (group.blend_mode ?? 'normal') !== 'normal' ||
    (group.plane_depth ?? 0) !== 0).map(group => group.key));
  const scopeOf = (layer: Layer) => {
    let parent = layer.parent;
    while (parent != null) {
      if (isolated.has(parent)) return parent;
      parent = groupByKey.get(parent)?.parent;
    }
    return null;
  };
  const groupScope = (group: Node) => {
    let parent = group.parent;
    while (parent != null) {
      if (isolated.has(parent)) return parent;
      parent = groupByKey.get(parent)?.parent;
    }
    return null;
  };
  for (const key of isolated) {
    const indices = layers.flatMap((layer, index) => {
      let parent = layer.parent;
      while (parent != null && parent !== key) parent = groupByKey.get(parent)?.parent;
      return parent === key ? [index] : [];
    });
    if (!indices.length) throw new Error(`Isolated group ${key} has no drawable descendants`);
    if (indices.at(-1)! - indices[0] + 1 !== indices.length)
      throw new Error(`Isolated group ${key} must occupy one contiguous layer block`);
  }
  const scopeKey = (key: string) => scopeOf(layers.find(layer => layer.key === key)!);
  for (const group of groups) {
    if (group.mount && groupScope(group) !== scopeKey(group.mount.layer))
      throw new Error(`Surface mount ${group.key} cannot cross an isolated group boundary`);
  }
  for (const {caster, receiver} of shadows) {
    if (!cutouts.has(caster) || (receiver != null && !cutouts.has(receiver)) || receiver === caster)
      throw new Error('A shadow needs a caster layer and a different receiver layer or the backdrop');
    if (scopeKey(caster) !== (receiver == null ? null : scopeKey(receiver)))
      throw new Error('A shadow cannot cross an isolated group boundary');
  }
  const seamKeys = new Set<string>();
  for (const seam of seams) {
    if (seamKeys.has(seam.key)) throw new Error(`Duplicate seam key: ${seam.key}`);
    seamKeys.add(seam.key);
    if (!visible.has(seam.a.layer) || !visible.has(seam.b.layer))
      throw new Error(`Seam ${seam.key} references an unknown cutout`);
    if (scopeKey(seam.a.layer) !== scopeKey(seam.b.layer))
      throw new Error(`Seam ${seam.key} cannot cross an isolated group boundary`);
    assertWindows(seam.windows ?? [], duration, `Seam ${seam.key}`);
  }
  for (const {target, mask, windows} of mattes) {
    if (!visible.has(target) || !cutouts.has(mask))
      throw new Error('A matte needs a visible target layer and an existing mask layer');
    if (scopeKey(target) !== scopeKey(mask))
      throw new Error('A matte cannot cross an isolated group boundary');
    assertWindows(windows ?? [], duration, 'Matte');
  }
  assertTrack(scene.camera?.motion ?? [], duration, 'Camera');
  for (const shake of scene.camera?.shakes ?? []) {
    if (!Number.isFinite(shake.time) || !Number.isFinite(shake.duration) ||
        shake.time < 0 || shake.duration <= 0 || shake.time + shake.duration > duration)
      throw new Error('Camera shakes must have positive duration and fit the scene duration');
  }
  return scene;
}

function patchValue<T, F>(map: ByKey<T> | undefined, key: string, fallback: F): T | F {
  return map && Object.hasOwn(map, key) ? map[key] : fallback;
}

function requireKeys(label: string, allowed: Set<string>, ...maps: (object | undefined)[]) {
  const unknown = new Set(maps.flatMap(map => Object.keys(map ?? {})).filter(key => !allowed.has(key)));
  if (unknown.size) throw new Error(`Unknown ${label}: ${Array.from(unknown).sort().join(', ')}`);
}

export function revise(scene: SceneRecipe, patch: Revision): SceneRecipe {
  const keys = new Set(scene.layers.map(l => l.key));
  const groups = new Set(scene.groups.map(g => g.key));
  requireKeys('cutouts', keys, patch.replacements, patch.depth, patch.meshes,
    patch.underlaps, patch.appearances, patch.paintovers, patch.edge_mattes);
  requireKeys('groups', groups, patch.mounts);
  requireKeys('motion nodes', new Set([...keys, ...groups]), patch.motions);
  requireKeys('composite nodes', new Set([...keys, ...groups]), patch.opacities,
    patch.blend_modes, patch.plane_depths);
  requireKeys('time nodes', new Set([...keys, ...groups]), patch.local_times);
  const replacements = patch.replacements ?? {};
  return checked({
    ...scene,
    duration: patch.duration ?? scene.duration,
    backing: patch.backing ?? scene.backing,
    shadows: patch.shadows ?? scene.shadows,
    seams: patch.seams ?? scene.seams,
    mattes: patch.mattes ?? scene.mattes,
    camera: patch.camera ?? scene.camera,
    claim_order: [...scene.claim_order.filter(key => !Object.hasOwn(replacements, key)), ...Object.keys(replacements)],
    layers: scene.layers.map(layer => {
      const {key} = layer;
      const replaced = Object.hasOwn(replacements, key);
      return {
        ...layer,
        selection: patchValue(replacements, key, layer.selection),
        source: replaced ? null : layer.source,
        scene: replaced ? null : layer.scene,
        // A matte belongs to a specific selection and cannot survive a replacement.
        edge_matte: patchValue(patch.edge_mattes, key, replaced ? null : layer.edge_matte),
        depth: patchValue(patch.depth, key, layer.depth),
        mesh: patchValue(patch.meshes, key, layer.mesh),
        underlap: patchValue(patch.underlaps, key, layer.underlap),
        paintover: patchValue(patch.paintovers, key, layer.paintover),
        appearance: patchValue(patch.appearances, key, layer.appearance),
        motion: patchValue(patch.motions, key, layer.motion),
        opacity: patchValue(patch.opacities, key, layer.opacity),
        blend_mode: patchValue(patch.blend_modes, key, layer.blend_mode),
        plane_depth: patchValue(patch.plane_depths, key, layer.plane_depth),
        local_time: patchValue(patch.local_times, key, layer.local_time),
      };
    }),
    groups: scene.groups.map(group => ({
      ...group,
      mount: patchValue(patch.mounts, group.key, group.mount),
      motion: patchValue(patch.motions, group.key, group.motion),
      opacity: patchValue(patch.opacities, group.key, group.opacity),
      blend_mode: patchValue(patch.blend_modes, group.key, group.blend_mode),
      plane_depth: patchValue(patch.plane_depths, group.key, group.plane_depth),
      local_time: patchValue(patch.local_times, group.key, group.local_time),
    })),
  });
}
