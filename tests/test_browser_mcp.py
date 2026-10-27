import http.server
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path
from unittest import mock

import install_browser_mcp as ib

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "vroxy-browser-mcp"
INSTALLED = (ROOT / "browser_mcp" / "node_modules" / "@playwright" / "mcp" / "cli.js").exists()


class FakeHomeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.bin = self.home / ".local" / "bin"
        self.bin.mkdir(parents=True)
        self.calls = self.home / "calls.log"
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "PATH": "/usr/bin:/bin"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake(self, name, exit_code=0):
        path = self.bin / name
        path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\nexit {exit_code}\n')
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def logged(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []


class MergeJsonTest(FakeHomeTestCase):
    def test_adds_beside_existing_servers_and_backs_up(self):
        path = self.home / ".cursor" / "mcp.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"mcpServers": {"railway": {"command": "railway"}}}))
        entry = {"command": "/x/vroxy-browser-mcp", "args": []}

        self.assertIn("wrote", ib.merge_json(path, ["mcpServers"], entry, dry_run=False))
        data = json.loads(path.read_text())
        self.assertEqual(data["mcpServers"]["railway"], {"command": "railway"})
        self.assertEqual(data["mcpServers"][ib.NAME], entry)
        self.assertTrue(path.with_name("mcp.json.bak-vroxy-browser").exists())
        self.assertEqual(ib.merge_json(path, ["mcpServers"], entry, dry_run=False), "already registered")

    def test_dry_run_writes_nothing(self):
        path = self.home / "new.json"
        ib.merge_json(path, ["mcp"], {"type": "local"}, dry_run=True)
        self.assertFalse(path.exists())


class RegisterTest(FakeHomeTestCase):
    def test_only_installed_harnesses_are_touched(self):
        self.fake("claude")
        self.fake("opencode")
        results = dict((h, ok) for h, ok, _ in ib.register())

        self.assertEqual(set(results), {"claude", "opencode"})
        self.assertTrue(all(results.values()))
        launcher = str(self.bin / "vroxy-browser-mcp")
        self.assertIn(f"claude mcp add -s user {ib.NAME} -- {launcher}", self.logged())
        config = json.loads((self.home / ".config" / "opencode" / "opencode.json").read_text())
        self.assertEqual(config["mcp"][ib.NAME], {"type": "local", "command": [launcher], "enabled": True})

    def test_a_failed_remove_does_not_stop_the_add(self):
        path = self.bin / "codex"
        path.write_text(f'#!/bin/sh\necho "codex $*" >> "{self.calls}"\n[ "$3" = remove ] && exit 1\nexit 0\n')
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

        (_, ok, _), = ib.register()
        self.assertTrue(ok)
        self.assertEqual(len(self.logged()), 2)

    def test_a_failed_add_is_reported(self):
        self.fake("gemini", exit_code=3)
        (harness, ok, _), = ib.register()
        self.assertEqual(harness, "gemini")
        self.assertFalse(ok)

    def test_kiro_is_a_file_edit_because_its_cli_needs_a_login(self):
        self.fake("kiro-cli")
        ib.register()
        self.assertEqual(self.logged(), [])
        config = json.loads((self.home / ".kiro" / "settings" / "mcp.json").read_text())
        self.assertIn(ib.NAME, config["mcpServers"])


@unittest.skipUnless(INSTALLED, "browser_mcp not installed — run bin/install-browser-mcp")
class LiveServerTest(unittest.TestCase):
    def setUp(self):
        self.site = tempfile.TemporaryDirectory()
        self.addCleanup(self.site.cleanup)
        Path(self.site.name, "index.html").write_text(
            "<button onclick=\"document.getElementById('r').textContent='clicked ok'\">Press me</button>"
            "<p id='r'>not yet</p>")
        handler = partial(http.server.SimpleHTTPRequestHandler, directory=self.site.name)
        handler.log_message = lambda *a: None
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.proc = subprocess.Popen([str(LAUNCHER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self.proc.stdout.close)
        self.addCleanup(self.proc.stdin.close)
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.kill)
        self.next_id = 0

    def rpc(self, method, params=None):
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                                          "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        while True:
            reply = json.loads(self.proc.stdout.readline())
            if reply.get("id") == self.next_id:
                return reply

    def tool(self, name, **arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})["result"]
        return "\n".join(c.get("text", "") for c in result["content"])

    def test_an_agent_can_open_a_page_click_and_read_the_result(self):
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "1"}})
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()
        names = {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertTrue({"browser_navigate", "browser_click", "browser_snapshot"} <= names)

        self.tool("browser_navigate", url=f"http://127.0.0.1:{self.httpd.server_port}/index.html")
        ref = re.search(r'button "Press me".*?\[ref=([^\]]+)\]', self.tool("browser_snapshot")).group(1)
        self.tool("browser_click", element="Press me button", target=ref)
        self.assertIn("clicked ok", self.tool("browser_snapshot"))


if __name__ == "__main__":
    unittest.main()
