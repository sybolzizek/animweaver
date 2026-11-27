import {readFileSync} from 'node:fs';
import {checked, revise} from './scene-plan.mjs';

try {
  const {scene, patch} = JSON.parse(readFileSync(0, 'utf8'));
  const recipe = patch === undefined ? checked(scene) : revise(scene, patch);
  const timelineOnly = patch !== undefined && Object.keys(patch).length > 0 &&
    Object.keys(patch).every(key => ['motions', 'duration', 'camera', 'opacities',
      'blend_modes', 'plane_depths', 'local_times'].includes(key));
  process.stdout.write(JSON.stringify({recipe, timelineOnly}));
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
