#!/usr/bin/env python3
"""Install the vroxy browser MCP server and register it with every
coding harness on the box, so any agent can drive a real browser.

  install-browser-mcp              install deps + browser, then register everywhere
  install-browser-mcp --register   only (re)register with the harnesses
  install-browser-mcp --dry-run    print what would change, change nothing
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

NAME = "vroxy-browser"
ROOT = Path(__file__).resolve().parent
PACKAGE_DIR = ROOT / "browser_mcp"
LAUNCHER = ROOT / "bin" / "vroxy-browser-mcp"
CLI_TIMEOUT = 45


def home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def newest_node_bin() -> Path | None:
    versions = sorted((home() / ".nvm" / "versions" / "node").glob("v*/bin"),
                      key=lambda p: tuple(int(x) for x in p.parent.name[1:].split(".") if x.isdigit()))
    return versions[-1] if versions else None


def tool_env() -> dict:
    env = os.environ.copy()
    extra = [str(home() / ".local" / "bin"), str(home() / ".opencode" / "bin")]
    node_bin = newest_node_bin()
    if node_bin:
        extra.insert(0, str(node_bin))
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def which(binary: str) -> str | None:
    return shutil.which(binary, path=tool_env()["PATH"])


def launcher_path() -> str:
    return str(home() / ".local" / "bin" / "vroxy-browser-mcp")


def cli_commands(cmd: str) -> dict[str, list[list[str]]]:
    return {
        "claude":   [["claude", "mcp", "remove", "-s", "user", NAME],
                     ["claude", "mcp", "add", "-s", "user", NAME, "--", cmd]],
        "codex":    [["codex", "mcp", "remove", NAME],
                     ["codex", "mcp", "add", NAME, "--", cmd]],
        "gemini":   [["gemini", "mcp", "remove", "-s", "user", NAME],
                     ["gemini", "mcp", "add", "-s", "user", "--trust", NAME, cmd]],
        "amp":      [["amp", "mcp", "remove", NAME],
                     ["amp", "mcp", "add", NAME, "--", cmd]],
        "copilot":  [["copilot", "mcp", "remove", NAME],
                     ["copilot", "mcp", "add", NAME, "--", cmd]],
        "devin":    [["devin", "mcp", "remove", "-s", "user", NAME],
                     ["devin", "mcp", "add", "-s", "user", NAME, "--", cmd]],
        "openclaw": [["openclaw", "mcp", "set", NAME, json.dumps({"command": cmd, "args": []})]],
    }


def json_targets(cmd: str) -> dict[str, tuple[Path, list[str], dict]]:
    return {
        "cursor-agent": (home() / ".cursor" / "mcp.json", ["mcpServers"],
                         {"command": cmd, "args": []}),
        "opencode":     (home() / ".config" / "opencode" / "opencode.json", ["mcp"],
                         {"type": "local", "command": [cmd], "enabled": True}),
        "cline":        (home() / ".cline" / "data" / "settings" / "cline_mcp_settings.json", ["mcpServers"],
                         {"command": cmd, "args": [], "disabled": False}),
        "kiro-cli":     (home() / ".kiro" / "settings" / "mcp.json", ["mcpServers"],
                         {"command": cmd, "args": [], "disabled": False}),
    }


def merge_json(path: Path, keys: list[str], entry: dict, dry_run: bool) -> str:
    data = {}
    if path.exists():
        text = path.read_text() or "{}"
        data = json.loads(text)
    node = data
    for key in keys:
        node = node.setdefault(key, {})
    if node.get(NAME) == entry:
        return "already registered"
    if dry_run:
        return f"would write {path}"
    node[NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak-vroxy-browser"))
    path.write_text(json.dumps(data, indent=2) + "\n")
    return f"wrote {path}"


def run(argv: list[str], dry_run: bool, check: bool = True) -> tuple[bool, str]:
    if dry_run:
        return True, "would run: " + " ".join(argv)
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=CLI_TIMEOUT,
                             env=tool_env(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"{argv[0]} timed out"
    ok = out.returncode == 0 or not check
    detail = (out.stderr or out.stdout or "").strip().splitlines()
    return ok, (detail[-1] if detail else f"exit {out.returncode}")[:200]


def register(dry_run: bool = False) -> list[tuple[str, bool, str]]:
    cmd = launcher_path()
    results: list[tuple[str, bool, str]] = []
    for harness, steps in cli_commands(cmd).items():
        if not which(harness):
            continue
        ok, detail = True, ""
        for i, argv in enumerate(steps):
            is_cleanup = len(steps) > 1 and i == 0
            ok, detail = run(argv, dry_run, check=not is_cleanup)
            if not ok:
                break
        results.append((harness, ok, detail if not ok or dry_run else "registered"))
    for harness, (path, keys, entry) in json_targets(cmd).items():
        if not which(harness):
            continue
        try:
            results.append((harness, True, merge_json(path, keys, entry, dry_run)))
        except (OSError, ValueError) as exc:
            results.append((harness, False, f"{path}: {exc}"))
            continue
        if harness == "cursor-agent":
            run(["cursor-agent", "mcp", "enable", NAME], dry_run, check=False)
    return results


def install(dry_run: bool) -> None:
    env = tool_env()
    steps = [
        (["npm", "ci", "--silent"] if (PACKAGE_DIR / "package-lock.json").exists()
         else ["npm", "install", "--silent"]),
        ["node", "node_modules/@playwright/mcp/cli.js", "install-browser", "chrome-for-testing"],
    ]
    for argv in steps:
        print("·", " ".join(argv))
        if not dry_run:
            subprocess.run(argv, cwd=PACKAGE_DIR, env=env, check=True, timeout=600)
    link = Path(launcher_path())
    print(f"· link {link} → {LAUNCHER}")
    if not dry_run:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(LAUNCHER)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="install-browser-mcp", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--register", action="store_true", help="only register with the harnesses")
    parser.add_argument("--dry-run", action="store_true", help="change nothing")
    args = parser.parse_args(argv)

    if not args.register:
        install(args.dry_run)
    results = register(args.dry_run)
    if not results:
        print("no harnesses found on PATH")
    for harness, ok, detail in results:
        print(f"{'✓' if ok else '✗'} {harness:13} {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
