import os

os.environ.setdefault("LOG_FILE", "")

import asyncio
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import feedback_agent as fa


def frames(link, action):
    out = []
    for frame in link.outbox:
        data = json.loads(json.loads(frame)["data"])
        if data.get("action") == action:
            out.append(data)
    return out


class SidDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(fa, "SID_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name):
        (fa.SID_DIR / name).write_text("thread-id")
        (fa.SID_DIR / f"{name}.meta").write_text(json.dumps({"turns": 3}))

    def names(self):
        return sorted(p.name for p in fa.SID_DIR.iterdir())


class ResetEveryEngineTest(SidDirTestCase):
    def room_file(self, engine, room="rmAAAA"):
        key = fa._room_session_key(room)
        if engine == "claude":
            return fa._streamed_sid_file(key).name
        return fa._streamed_sid_file(f"{engine}_{key}").name

    def test_every_engine_stores_room_sessions_where_reset_looks(self):
        for engine in fa.known_engines():
            with self.subTest(engine=engine):
                self.write(self.room_file(engine))
                removed = fa.clear_sessions([fa._room_session_key("rmAAAA")])
                self.assertIn(self.room_file(engine), removed,
                              f"/reset left the {engine} session behind")
                self.assertEqual([], self.names())

    def test_reset_of_one_room_leaves_the_other_rooms_on_every_engine(self):
        for engine in ("claude", "codex", "cursor", "gemini"):
            self.write(self.room_file(engine, "rmAAAA"))
            self.write(self.room_file(engine, "rmBBBB"))
        fa.clear_sessions([fa._room_session_key("rmAAAA")])
        self.assertTrue(all("rmBBBB" in n for n in self.names()), self.names())
        self.assertEqual(8, len(self.names()))

    def test_a_full_reset_clears_every_engines_feedback_and_room_sessions(self):
        for engine in fa.known_engines():
            self.write(self.room_file(engine))
            prefix = "" if engine == "claude" else f"{engine}_"
            self.write(fa._streamed_sid_file(f"{prefix}{fa.PROJECT}").name)
        fa.clear_sessions()
        self.assertEqual([], self.names())

    def test_a_full_reset_leaves_another_projects_sessions(self):
        other = [f"feedback_stream_codex_room_rmAAAA_some_other_project",
                 f"feedback_stream_codex_some_other_project",
                 f"feedback_stream_room_rmAAAA_some_other_project"]
        for name in other:
            (fa.SID_DIR / name).write_text("x")
        self.write(self.room_file("codex"))
        fa.clear_sessions()
        self.assertEqual(sorted(other), self.names())

    def test_the_sidecar_goes_with_its_session(self):
        self.write(self.room_file("codex"))
        removed = fa.clear_sessions([fa._room_session_key("rmAAAA")])
        self.assertIn(self.room_file("codex") + ".meta", removed)


class RoomResetOnOtherEnginesTest(SidDirTestCase):
    def test_slash_reset_on_codex_clears_the_codex_thread(self):
        name = fa._streamed_sid_file(f"codex_{fa._room_session_key('rm123456')}").name
        self.write(name)
        link = fa.CableLink()
        payload = {"room": {"hashid": "rm123456", "name": "Dispatch"},
                   "message": {"hashid": "ms1", "body": "/reset"}}
        with mock.patch.object(fa, "DISPATCH_ENGINE", "codex"), \
             mock.patch.object(fa, "run_agent_streamed",
                               side_effect=AssertionError("a reset must not run the agent")):
            asyncio.run(fa.handle_room_message(link, payload))
        self.assertEqual([], self.names())
        replies = frames(link, "room_reply")
        self.assertEqual(1, len(replies))
        self.assertIn("forgotten this room", replies[0]["body"])
        self.assertEqual("ms1", replies[0]["reply_to"])

    def test_slash_reset_with_nothing_stored_says_so(self):
        link = fa.CableLink()
        payload = {"room": {"hashid": "rm123456"}, "message": {"hashid": "ms1", "body": "/new"}}
        asyncio.run(fa.handle_room_message(link, payload))
        self.assertIn("Already on a fresh session", frames(link, "room_reply")[0]["body"])


class CommandLineResetTest(SidDirTestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["feedback_agent.py", *argv]), redirect_stdout(out), \
             mock.patch.object(fa, "main", side_effect=AssertionError("must not start the agent")):
            fa.cli()
        return out.getvalue()

    def test_reset_with_room_ids_clears_only_those_rooms(self):
        self.write(fa._streamed_sid_file(fa._room_session_key("rmAAAA")).name)
        self.write(fa._streamed_sid_file(fa._room_session_key("rmBBBB")).name)
        out = self.run_cli("--reset", "rmAAAA")
        self.assertIn("room(s) rmAAAA", out)
        self.assertTrue(all("rmBBBB" in n for n in self.names()))

    def test_reset_with_nothing_stored_says_so(self):
        self.assertIn("nothing to clear", self.run_cli("--reset"))

    def test_sessions_lists_what_is_stored(self):
        self.write(fa._streamed_sid_file(fa._room_session_key("rmAAAA")).name)
        out = self.run_cli("--sessions")
        self.assertIn("rmAAAA", out)
        self.assertIn("thread-id", out)

    def test_help_does_not_start_the_agent(self):
        self.assertIn("--reset", self.run_cli("--help"))


class StateFileRobustnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = Path(self.tmp.name)
        patcher = mock.patch.object(fa, "STATE_DIR", state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_spool_with_a_garbage_timestamp_is_dropped_not_a_boot_crash(self):
        for stamp in ("yesterday", [1], {"t": 1}):
            with self.subTest(stamp=stamp):
                fa.work_spool_path().write_text(json.dumps(
                    {"at": stamp, "items": [{"kind": "room", "payload": {"x": 1}}]}))
                self.assertEqual([], fa.take_spooled_work())
                self.assertFalse(fa.work_spool_path().exists())

    def test_a_spool_whose_items_are_not_a_list_is_dropped(self):
        for items in ("room", 7, {"kind": "room"}):
            fa.work_spool_path().write_text(json.dumps({"at": time.time(), "items": items}))
            self.assertEqual([], fa.take_spooled_work(), items)

    def test_a_spool_that_is_a_list_is_dropped(self):
        fa.work_spool_path().write_text(json.dumps([{"kind": "room", "payload": {}}]))
        self.assertEqual([], fa.take_spooled_work())

    def test_a_restart_notice_with_a_garbage_timestamp_is_dropped(self):
        for stamp in ("soon", [1]):
            with self.subTest(stamp=stamp):
                fa.restart_notice_path().write_text(json.dumps({"room_id": "r1", "at": stamp}))
                self.assertIsNone(fa.take_restart_notice())
                self.assertFalse(fa.restart_notice_path().exists())

    def test_a_fresh_spool_round_trips_through_a_restart(self):
        saved = (fa._work_queue, fa._current_work)
        try:
            fa._current_work = None
            fa._work_queue = asyncio.Queue()
            fa._work_queue.put_nowait(("room", {"message": {"hashid": "m1"}}))
            fa._work_queue.put_nowait(("approve", {"chat_id": "c1"}))
            self.assertEqual(2, fa.spool_pending_work())
            self.assertTrue(fa._work_queue.empty())
            items = fa.take_spooled_work()
        finally:
            fa._work_queue, fa._current_work = saved
        self.assertEqual(["room", "approve"], [i["kind"] for i in items])
        self.assertEqual([], fa.take_spooled_work(), "a spool replays once, never twice")


if __name__ == "__main__":
    unittest.main()
