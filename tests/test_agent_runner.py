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


def assistant(text, model="claude-test"):
    return json.dumps({"type": "assistant", "session_id": "sess-new",
                       "message": {"model": model, "content": [{"type": "text", "text": text}]}})


def result(text, session="sess-new"):
    return json.dumps({"type": "result", "session_id": session, "result": text,
                       "usage": {"input_tokens": 3}, "total_cost_usd": 0.002,
                       "duration_ms": 10, "num_turns": 1})


class FakeClaudeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "vroxy_web").mkdir()
        self.sid_dir = self.root / "sids"
        self.bin = self.root / "claude"
        self.argv_log = self.root / "argv.log"
        self.pidfile = self.root / "pid"
        for name, value in (("CODE_ROOT", self.root), ("SID_DIR", self.sid_dir),
                            ("STALL_SECONDS", 0.3), ("STALL_WINDOWS_BEFORE_KILL", 2),
                            ("_engine_fault", None), ("_harness_cache", None),
                            ("_last_model", None), ("_current_proc", None)):
            patcher = mock.patch.object(fa, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"CLAUDE_BIN": str(self.bin)})
        env.start()
        self.addCleanup(env.stop)
        self.events = []

    def script(self, body):
        self.bin.write_text("#!/bin/bash\n"
                            f'printf "%s\\n" "$*" >> "{self.argv_log}"\n'
                            + textwrap.dedent(body))
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)

    def emit(self, *lines):
        return "".join(f"echo {json.dumps(line)}\n" for line in lines)

    def run_it(self, allow_resume=True, session_key="room_rm1_vroxy_web"):
        return fa.run_claude_streamed("prompt", "vroxy_web", self.events.append,
                                      allow_resume, session_key)

    def sid_file(self, key="room_rm1_vroxy_web"):
        return fa._streamed_sid_file(key)

    def calls(self):
        return self.argv_log.read_text().splitlines() if self.argv_log.exists() else []

    def assert_dead(self):
        pid = int(self.pidfile.read_text())
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"the wedged process {pid} is still alive")


class StallKillTest(FakeClaudeTestCase):
    def test_a_silent_run_is_killed_and_raises_rather_than_hanging(self):
        self.script(f'echo $$ > "{self.pidfile}"\nexec sleep 60\n')
        started = time.monotonic()
        with self.assertRaises(RuntimeError) as cm:
            self.run_it()
        self.assertLess(time.monotonic() - started, 20)
        self.assertIn("killed it as wedged", str(cm.exception))
        self.assert_dead()
        stalls = [e for e in self.events if e["type"] == "stalled"]
        self.assertEqual([1, 2], [e["window"] for e in stalls])
        self.assertEqual(2, stalls[-1]["max_windows"])

    def test_a_run_that_wedges_after_speaking_returns_what_it_had_and_says_it_was_cut(self):
        self.script(self.emit(assistant("Half an answer."))
                    + f'echo $$ > "{self.pidfile}"\nexec sleep 60\n')
        answer = self.run_it()
        self.assertTrue(answer.startswith("Half an answer."))
        self.assertIn("⚠️ Cut short", answer)
        self.assert_dead()

    def test_output_resets_the_stall_count(self):
        self.script(self.emit(assistant("one")) + "sleep 0.45\n"
                    + self.emit(assistant("two")) + "sleep 0.45\n"
                    + self.emit(result("one two")))
        with mock.patch.object(fa, "STALL_WINDOWS_BEFORE_KILL", 3):
            self.assertEqual("one two", self.run_it())
        windows = [e["window"] for e in self.events if e["type"] == "stalled"]
        self.assertGreaterEqual(len(windows), 2)
        self.assertEqual(1, windows[-1], "each burst of output starts the count again")


class SessionResumeTest(FakeClaudeTestCase):
    def test_a_finished_turn_stores_its_session_for_the_next(self):
        self.script(self.emit(assistant("hi"), result("hi")))
        self.assertEqual("hi", self.run_it())
        self.assertEqual("sess-new", self.sid_file().read_text())
        meta = json.loads(Path(str(self.sid_file()) + ".meta").read_text())
        self.assertEqual(1, meta["turns"])
        self.assertEqual("claude-test", fa._last_model)

    def test_the_next_turn_resumes_the_stored_session(self):
        self.script(self.emit(result("again", session="sess-old")))
        self.sid_dir.mkdir()
        self.sid_file().write_text("sess-old")
        self.run_it()
        self.assertIn("--resume sess-old", self.calls()[0])

    def test_a_fresh_start_is_asked_for_by_not_resuming(self):
        self.script(self.emit(result("fresh")))
        self.sid_dir.mkdir()
        self.sid_file().write_text("sess-old")
        self.run_it(allow_resume=False)
        self.assertNotIn("--resume", self.calls()[0])

    def test_a_vanished_transcript_is_dropped_and_the_prompt_retried_fresh(self):
        self.script('if [[ "$*" == *--resume* ]]; then echo "no such session" >&2; exit 1; fi\n'
                    + self.emit(result("recovered", session="sess-fresh")))
        self.sid_dir.mkdir()
        self.sid_file().write_text("sess-gone")
        self.assertEqual("recovered", self.run_it())
        self.assertEqual(2, len(self.calls()))
        self.assertIn("--resume sess-gone", self.calls()[0])
        self.assertNotIn("--resume", self.calls()[1])
        self.assertEqual("sess-fresh", self.sid_file().read_text())

    def test_rooms_do_not_share_a_session(self):
        self.script(self.emit(result("a", session="sess-a")))
        self.run_it(session_key="room_rmA_vroxy_web")
        self.script(self.emit(result("b", session="sess-b")))
        self.run_it(session_key="room_rmB_vroxy_web")
        self.assertEqual("sess-a", self.sid_file("room_rmA_vroxy_web").read_text())
        self.assertEqual("sess-b", self.sid_file("room_rmB_vroxy_web").read_text())
        self.assertNotIn("--resume", self.calls()[1])


class FailedRunTest(FakeClaudeTestCase):
    def test_a_failure_with_nothing_to_show_raises_with_its_stderr(self):
        self.script('echo "You have exceeded your usage limit" >&2\nexit 2\n')
        with self.assertRaises(RuntimeError) as cm:
            self.run_it()
        self.assertIn("exited 2", str(cm.exception))
        self.assertIn("usage limit", str(cm.exception))

    def test_a_failure_after_partial_output_is_cut_short_not_passed_off_as_done(self):
        self.script(self.emit(assistant("Ran the suite, 3 failures"))
                    + 'echo "API Error: overloaded" >&2\nexit 1\n')
        answer = self.run_it()
        self.assertIn("Ran the suite, 3 failures", answer)
        self.assertIn("⚠️ Cut short", answer)
        self.assertIn("overloaded", answer)

    def test_a_missing_project_directory_is_a_loud_error(self):
        self.script(self.emit(result("x")))
        with self.assertRaises(FileNotFoundError):
            fa.run_claude_streamed("p", "no_such_project", self.events.append)
        self.assertEqual([], self.calls())

    def test_the_result_event_carries_cost_and_usage(self):
        self.script(self.emit(result("ok")))
        self.run_it()
        final = [e for e in self.events if e["type"] == "result"][-1]
        self.assertEqual({"input_tokens": 3}, final["usage"])
        self.assertEqual(0.002, final["cost_usd"])
        self.assertFalse(final["is_error"])

    def test_garbage_lines_in_the_stream_are_skipped(self):
        self.script('echo "not json"\necho ""\n' + self.emit(result("fine")))
        self.assertEqual("fine", self.run_it())


if __name__ == "__main__":
    unittest.main()
