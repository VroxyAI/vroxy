import os

os.environ.setdefault("LOG_FILE", "")

import json
import stat
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import feedback_agent as fa


def text_event(text, sid="sess-1"):
    return json.dumps({"type": "text", "sessionID": sid,
                       "part": {"type": "text", "text": text}})


def finish_event(sid="sess-1"):
    return json.dumps({"type": "step_finish", "sessionID": sid,
                       "part": {"type": "step-finish",
                                "tokens": {"input": 1, "output": 1, "total": 2}}})


class FakeOpenCodeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "vroxy_web").mkdir()
        self.sid_dir = self.root / "sids"
        self.bin = self.root / "opencode"
        self.counter = self.root / "counter"
        self.pidfile = self.root / "pid"
        for name, value in (("CODE_ROOT", self.root), ("SID_DIR", self.sid_dir),
                            ("STALL_SECONDS", 0.3), ("STALL_WINDOWS_BEFORE_KILL", 2),
                            ("_engine_fault", None), ("_harness_cache", None),
                            ("_agent_model", None), ("_last_model", None),
                            ("_current_proc", None)):
            patcher = mock.patch.object(fa, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        fa._wedged_retried.clear()
        env = mock.patch.dict(os.environ, {
            "OPENCODE_BIN": str(self.bin),
            "COUNTER": str(self.counter),
            "PIDFILE": str(self.pidfile),
        })
        env.start()
        self.addCleanup(env.stop)
        self.events = []

    def script(self, body):
        self.bin.write_text("#!/bin/bash\n"
                            'n=$(cat "$COUNTER" 2>/dev/null || echo 0)\n'
                            'echo $((n+1)) > "$COUNTER"\n'
                            + textwrap.dedent(body))
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)

    def run_it(self, session_key="room_rm1_vroxy_web"):
        return fa.run_harness_streamed("opencode", "prompt", "vroxy_web",
                                       self.events.append, allow_resume=False,
                                       session_key=session_key)

    def assert_dead(self):
        pid = int(self.pidfile.read_text())
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"the wedged process {pid} is still alive")


class WedgeRetryTest(FakeOpenCodeTestCase):
    def test_a_silent_fresh_run_is_retried_once_and_recovers(self):
        self.script('if [ "$n" -eq 0 ]; then echo $$ > "$PIDFILE"; exec sleep 60; fi\n'
                    + self.emit(text_event("recovered"), finish_event()))
        self.assertEqual("recovered", self.run_it())
        self.assertEqual(2, int(self.counter.read_text()))
        self.assert_dead()

    def test_a_persistently_wedged_run_raises_a_clear_killed_message(self):
        self.script('echo $$ > "$PIDFILE"\nexec sleep 60\n')
        with self.assertRaises(RuntimeError) as cm:
            self.run_it()
        self.assertIn("killed it as wedged", str(cm.exception))
        self.assertNotIn("exited -15", str(cm.exception))
        self.assertEqual(2, int(self.counter.read_text()))
        self.assert_dead()

    def emit(self, *lines):
        return "".join(f"printf '%s\\n' {json.dumps(line)}\n" for line in lines)


if __name__ == "__main__":
    unittest.main()
