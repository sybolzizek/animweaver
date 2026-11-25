#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const [command, ...args] = process.argv.slice(2);
const here = dirname(fileURLToPath(import.meta.url));
const {name, version} = JSON.parse(readFileSync(join(here, 'package.json'), 'utf8'));
const help = 'Usage: reference-materials setup --workspace <directory> [--client codex|claude|config] [--browser <executable> | --materials-only]';
if (!command || ['-h', '--help'].includes(command)) {
  console.log(help);
} else if (command === '--version') {
  console.log(version);
} else if (command !== 'setup') {
  console.error(help);
  process.exitCode = 2;
} else {
  const candidates = process.env.REFERENCE_PYTHON ? [[process.env.REFERENCE_PYTHON]] :
    process.platform === 'win32' ? [['py', '-3'], ['python'], ['python3']] : [['python3'], ['python']];
  const python = candidates.find(([name, ...flags]) => spawnSync(name, [
    ...flags, '-c', 'import sys; sys.exit(sys.version_info < (3, 11))',
  ], {stdio:'ignore', windowsHide:true}).status === 0);
  if (!python) {
    console.error('Python 3.11+ is required for image processing. Install it or set REFERENCE_PYTHON.');
    process.exitCode = 1;
  } else {
    const base = process.platform === 'win32' ? (process.env.LOCALAPPDATA || join(homedir(), 'AppData', 'Local')) :
      process.platform === 'darwin' ? join(homedir(), 'Library', 'Caches') : (process.env.XDG_CACHE_HOME || join(homedir(), '.cache'));
    const cp = spawnSync(python[0], [...python.slice(1), join(here, 'install_mcp.py'), ...args], {
      stdio:'inherit', windowsHide:true,
      env: {...process.env,
        REFERENCE_SETUP_NODE: process.execPath,
        REFERENCE_SETUP_PLAYWRIGHT: join(dirname(createRequire(import.meta.url).resolve('playwright-core/package.json')), 'cli.js'),
        REFERENCE_RUNTIME_DIR: resolve(process.env.REFERENCE_RUNTIME_DIR || join(base, name, version)),
      },
    });
    if (cp.error) console.error(cp.error.message);
    process.exitCode = cp.status ?? 1;
  }
}
