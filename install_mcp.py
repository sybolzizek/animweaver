"""Install the local runtime and print or register its MCP launch configuration."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def executable(name):
    path = shutil.which(name)
    if path is None:
        raise ValueError(f"Cannot find {name}. Install it or add it to PATH.")
    return str(Path(path).absolute())


def client_prefix(client):
    if os.name != "nt":
        return [executable(client)]
    if path := shutil.which(f"{client}.exe"):
        return [str(Path(path).absolute())]
    # The usual npm Codex installation puts a .cmd shim on PATH. Invoke its
    # JS entry directly so Windows shell quoting cannot change workspace paths.
    if client == "codex" and (shim := shutil.which("codex.cmd")):
        entry = Path(shim).parent / "node_modules/@openai/codex/bin/codex.js"
        if entry.is_file():
            return [executable("node"), str(entry.resolve())]
    raise ValueError(
        f"Cannot find a supported {client} executable. Use --client config."
    )


def add_client(client, prefix, name, entry, workspace):
    launch = [entry["command"], *entry["args"]]
    env_args = [
        item
        for key, value in entry["env"].items()
        for item in ("--env", f"{key}={value}")
    ]
    if client == "codex":
        servers = json.loads(
            subprocess.check_output(
                [*prefix, "mcp", "list", "--json"], cwd=workspace, encoding="utf-8"
            )
        )
        current = next((row for row in servers if row["name"] == name), None)
        if current is not None:
            transport = current.get("transport") or {}
            if (
                current.get("enabled")
                and transport.get("type") == "stdio"
                and all(
                    (transport.get(key) or {}) == value for key, value in entry.items()
                )
            ):
                print(f"{name} is already configured.", file=sys.stderr)
                return
            raise ValueError(
                f"Codex already has {name}. Use --name with a new name; the existing entry was not changed."
            )
        cmd = [*prefix, "mcp", "add", name, *env_args, "--", *launch]
    else:
        cmd = [
            *prefix,
            "mcp",
            "add",
            "--scope",
            "local",
            *env_args,
            "--transport",
            "stdio",
            name,
            "--",
            *launch,
        ]
    subprocess.run(cmd, cwd=workspace, check=True, stdout=sys.stderr)
    print("Start a new agent session to load the server.", file=sys.stderr)


def setup(args):
    workspace = args.workspace.expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("--workspace must be an existing directory.")
    prefix = client_prefix(args.client) if args.client != "config" else None
    runtime = {}
    runtime_dir = Path(os.environ.get("REFERENCE_RUNTIME_DIR", ROOT)).resolve()
    playwright = os.environ.get("REFERENCE_SETUP_PLAYWRIGHT")
    if not args.materials_only:
        node = os.environ.get("REFERENCE_SETUP_NODE") or executable("node")
        npm = None if playwright else executable("npm")
        runtime["REFERENCE_NODE"] = node
        if ffmpeg := shutil.which("ffmpeg"):
            runtime["REFERENCE_FFMPEG"] = str(Path(ffmpeg).absolute())
        else:
            print(
                "FFmpeg not found: PNG works, MP4 requires FFmpeg with libx264.",
                file=sys.stderr,
            )
        if args.browser:
            browser = args.browser.expanduser().resolve(strict=True)
            if not browser.is_file() or not os.access(browser, os.X_OK):
                raise ValueError(
                    "--browser must point to a Chrome or Chromium executable."
                )
            runtime["REFERENCE_BROWSER_EXECUTABLE"] = str(browser)
        else:
            runtime["PLAYWRIGHT_BROWSERS_PATH"] = str(runtime_dir / ".browsers")

    env_dir = runtime_dir / ".venv"
    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(env_dir)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "--disable-pip-version-check",
            "install",
            "-r",
            str(ROOT / "requirements.txt"),
        ],
        check=True,
        stdout=sys.stderr,
    )
    if not args.materials_only:
        if npm:
            subprocess.run(
                [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                cwd=ROOT,
                check=True,
                stdout=sys.stderr,
            )
            subprocess.run([npm, "run", "build"], cwd=ROOT, check=True, stdout=sys.stderr)
        if not args.browser:
            subprocess.run(
                [
                    node,
                    playwright or str(ROOT / "node_modules/playwright-core/cli.js"),
                    "install",
                    "--only-shell",
                    "chromium",
                ],
                cwd=ROOT,
                env=os.environ | runtime,
                check=True,
                stdout=sys.stderr,
            )

    entry = {
        "command": str(python),
        "args": [str(ROOT / "server.py"), "--workspace", str(workspace)],
        "env": runtime,
    }
    if prefix:
        add_client(args.client, prefix, args.name, entry, workspace)
    else:
        print(
            json.dumps({"mcpServers": {args.name: entry}}, ensure_ascii=True, indent=2)
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="existing local materials directory",
    )
    parser.add_argument(
        "--client", choices=("config", "codex", "claude"), default="config"
    )
    parser.add_argument(
        "--name", default="reference-materials", help="local name for this MCP server"
    )
    renderer = parser.add_mutually_exclusive_group()
    renderer.add_argument(
        "--browser",
        type=Path,
        help="existing Chrome/Chromium executable; skips browser download",
    )
    renderer.add_argument(
        "--materials-only", action="store_true", help="skip the rendering runtime"
    )
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 or newer is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.name):
        parser.error("--name must contain only letters, numbers, '-' or '_'")
    try:
        setup(args)
    except (ValueError, OSError) as error:
        parser.exit(1, f"Setup failed: {error}\n")
    except subprocess.CalledProcessError as error:
        parser.exit(
            1,
            f"Setup stopped after a command exited with {error.returncode}; see the output above.\n",
        )


if __name__ == "__main__":
    main()
