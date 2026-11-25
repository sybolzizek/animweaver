import {assertTrack, assertWindows} from './cutout-timeline.mjs';
import type {DepthKey, LightKey, Pose, Timed, Window} from './cutout-timeline.mjs';

type PoseKey = Timed & Pose;
type SurfaceMount = {layer: string};
type Node = {key: string; parent?: string | null; mount?: SurfaceMount | null; motion?: PoseKey[]};
type Skin = {bones: {key: string}[]};
type Appearance = {light?: LightKey[]};
type Paint = {source: string};
type Layer = Node & {
  selection?: string | null; source?: string | null; edge_matte?: string | null;
  depth?: DepthKey[]; mesh?: Skin | null; appearance?: Appearance | null;
  underlap?: Paint[]; paintover?: Paint[]; matte_only?: boolean;
};
type Shadow = {caster: string; receiver?: string | null};
type Seam = {key: string; a: {layer: string}; b: {layer: string}; windows?: Window[]};
type Matte = {target: string; mask: string; windows?: Window[]};
export type SceneRecipe = {
  base: string; groups: Node[]; layers: Layer[]; duration: number; backing: string | null;
  claim_order: string[]; shadows: Shadow[]; seams: Seam[]; mattes: Matte[];
};
type ByKey<T> = Record<string, T>;
export type Revision = {
  replacements?: ByKey<string>; depth?: ByKey<DepthKey[]>; meshes?: ByKey<Skin | null>;
  mounts?: ByKey<SurfaceMount | null>; underlaps?: ByKey<Paint[]>; appearances?: ByKey<Appearance | null>;
  paintovers?: ByKey<Paint[]>; edge_mattes?: ByKey<string | null>; motions?: ByKey<PoseKey[]>;
  backing?: string | null; duration?: number | null; shadows?: Shadow[] | null;
  seams?: Seam[] | null; mattes?: Matte[] | null;
};

// Validate structure here, not in the image worker. The worker can check only
// facts which require source masks, pixels or precomputed rig geometry.
export function checked(scene: SceneRecipe): SceneRecipe {
  const {groups, layers, duration, shadows, seams, mattes, claim_order} = scene;
  if (!Number.isFinite(duration) || duration <= 0) throw new Error('Scene duration must be positive');
  if (!layers.length) throw new Error('Use at least one layer');
  const nodes = [...groups, ...layers];
  const groupKeys = new Set(groups.map(g => g.key));
  const cutouts = new Set(layers.map(l => l.key));
  const visible = new Set(layers.filter(l => !l.matte_only).map(l => l.key));
  const links = new Map(nodes.map(n => [n.key, [] as string[]]));
  if (links.size !== nodes.length) throw new Error('Duplicate scene keys');
  if (claim_order.length !== cutouts.size || new Set(claim_order).size !== cutouts.size ||
      claim_order.some(key => !cutouts.has(key))) throw new Error('Claim order must list every cutout once');

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
    assertTrack(node.motion ?? [], duration, 'Motion');
  }
  for (const layer of layers) {
    assertTrack(layer.appearance?.light ?? [], duration, 'Light');
    assertTrack(layer.depth ?? [], duration, 'Depth');
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
  for (const {caster, receiver} of shadows) {
    if (!cutouts.has(caster) || (receiver != null && !cutouts.has(receiver)) || receiver === caster)
      throw new Error('A shadow needs a caster layer and a different receiver layer or the backdrop');
  }
  const seamKeys = new Set<string>();
  for (const seam of seams) {
    if (seamKeys.has(seam.key)) throw new Error(`Duplicate seam key: ${seam.key}`);
    seamKeys.add(seam.key);
    if (!visible.has(seam.a.layer) || !visible.has(seam.b.layer))
      throw new Error(`Seam ${seam.key} references an unknown cutout`);
    assertWindows(seam.windows ?? [], duration, `Seam ${seam.key}`);
  }
  for (const {target, mask, windows} of mattes) {
    if (!visible.has(target) || !cutouts.has(mask))
      throw new Error('A matte needs a visible target layer and an existing mask layer');
    assertWindows(windows ?? [], duration, 'Matte');
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
  const replacements = patch.replacements ?? {};
  return checked({
    ...scene,
    duration: patch.duration ?? scene.duration,
    backing: patch.backing ?? scene.backing,
    shadows: patch.shadows ?? scene.shadows,
    seams: patch.seams ?? scene.seams,
    mattes: patch.mattes ?? scene.mattes,
    claim_order: [...scene.claim_order.filter(key => !Object.hasOwn(replacements, key)), ...Object.keys(replacements)],
    layers: scene.layers.map(layer => {
      const {key} = layer;
      const replaced = Object.hasOwn(replacements, key);
      return {
        ...layer,
        selection: patchValue(replacements, key, layer.selection),
        source: replaced ? null : layer.source,
        // A matte belongs to a specific selection and cannot survive a replacement.
        edge_matte: patchValue(patch.edge_mattes, key, replaced ? null : layer.edge_matte),
        depth: patchValue(patch.depth, key, layer.depth),
        mesh: patchValue(patch.meshes, key, layer.mesh),
        underlap: patchValue(patch.underlaps, key, layer.underlap),
        paintover: patchValue(patch.paintovers, key, layer.paintover),
        appearance: patchValue(patch.appearances, key, layer.appearance),
        motion: patchValue(patch.motions, key, layer.motion),
      };
    }),
    groups: scene.groups.map(group => ({
      ...group,
      mount: patchValue(patch.mounts, group.key, group.mount),
      motion: patchValue(patch.motions, group.key, group.motion),
    })),
  });
}
