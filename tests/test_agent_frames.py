import os

os.environ.setdefault("LOG_FILE", "")

import asyncio
import json
import subprocess
import tempfile
import unittest
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


def sent_actions(sock):
    out = []
    for frame in sock.sent:
        envelope = json.loads(frame)
        data = json.loads(envelope["data"]) if "data" in envelope else {}
        out.append((envelope["command"], data.get("action"), data))
    return out


class FrameSocket:
    def __init__(self, *messages, raw=()):
        self.sent = []
        self.lines = [json.dumps(m) for m in raw] + [
            json.dumps({"identifier": fa.CHANNEL_IDENTIFIER, "message": m}) for m in messages]

    async def send(self, frame):
        self.sent.append(frame)

    def __aiter__(self):
        async def gen():
            for line in self.lines:
                yield line
        return gen()


def room_msg(hashid, body="hello", room="rm123456", **extra):
    return {"type": "room.message", "room": {"hashid": room, "name": "Dispatch"},
            "message": {"hashid": hashid, "body": body}, **extra}


async def idle_heartbeat(link):
    return None


class ProcessStreamTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved = (fa._work_queue, fa._current_work, set(fa._paused_messages),
                       set(fa._cancelled_messages), fa._current_proc)
        fa._work_queue = asyncio.Queue()
        fa._current_work = None
        fa._current_proc = None
        fa._paused_messages.clear()
        fa._cancelled_messages.clear()
        patcher = mock.patch.object(fa, "heartbeat_forever", idle_heartbeat)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        (fa._work_queue, fa._current_work, paused, cancelled, fa._current_proc) = self._saved
        fa._paused_messages.clear()
        fa._paused_messages.update(paused)
        fa._cancelled_messages.clear()
        fa._cancelled_messages.update(cancelled)

    def queued(self):
        items = []
        while not fa._work_queue.empty():
            items.append(fa._work_queue.get_nowait())
        for item in items:
            fa._work_queue.put_nowait(item)
        return items

    async def stream(self, *messages, raw=()):
        sock = FrameSocket(*messages, raw=raw)
        link = fa.CableLink()
        await fa.process_stream(link, sock)
        self.assertIsNone(link.ws, "the link lets go of a socket that closed")
        return sock


class FrameRoutingTest(ProcessStreamTestCase):
    async def test_each_work_frame_lands_on_the_queue_as_its_kind(self):
        await self.stream(
            {"type": "feedback.created", "feedback": {"hashid": "fb1"}},
            {"type": "feedback.followup", "feedback": {"hashid": "fb2"}},
            {"type": "approve.requested", "chat_id": "ch1"},
            room_msg("ms1"),
        )
        self.assertEqual(["feedback", "feedback", "approve", "room"],
                         [kind for kind, _ in self.queued()])

    async def test_a_queued_room_message_is_acknowledged_as_queued(self):
        sock = await self.stream(room_msg("ms1"))
        statuses = [d for c, a, d in sent_actions(sock) if a == "room_status"]
        self.assertEqual(1, len(statuses))
        self.assertEqual("queued", statuses[0]["state"])
        self.assertEqual("ms1", statuses[0]["reply_to"])
        self.assertEqual("rm123456", statuses[0]["room_id"])

    async def test_control_and_bookkeeping_frames_are_not_work(self):
        await self.stream(
            {"type": "room_reply.posted", "room_id": "rm1", "message_id": "ms9"},
            {"type": "room.pause", "message": {"hashid": "ms1"}},
            {"type": "room.resume", "message": {"hashid": "ms1"}},
            {"type": "something.new", "x": 1},
            raw=[{"type": "ping", "message": 1}, {"type": "disconnect"},
                 {"type": "reject_subscription"}, {"identifier": "x"}],
        )
        self.assertEqual([], self.queued())

    async def test_welcome_subscribes_on_that_socket(self):
        sock = await self.stream(raw=[{"type": "welcome"}])
        self.assertEqual([("subscribe", None)], [(c, a) for c, a, _ in sent_actions(sock)])

    async def test_a_confirmed_subscription_flushes_what_piled_up(self):
        link = fa.CableLink()
        await fa.room_reply(link, "rm1", "finished while the cable was down")
        sock = FrameSocket(raw=[{"type": "confirm_subscription"}])
        with mock.patch.object(fa, "announce_restart", mock.AsyncMock()), \
             mock.patch.object(fa, "announce_interrupted", mock.AsyncMock()):
            await fa.process_stream(link, sock)
        self.assertEqual([], link.outbox)
        self.assertEqual(["room_reply"], [a for _, a, _ in sent_actions(sock)])

    async def test_pause_holds_and_resume_releases(self):
        await self.stream({"type": "room.pause", "message": {"hashid": "ms1"}})
        self.assertTrue(fa.is_paused({"message": {"hashid": "ms1"}}))
        await self.stream({"type": "room.resume", "message": {"hashid": "ms1"}})
        self.assertFalse(fa.is_paused({"message": {"hashid": "ms1"}}))

    async def test_kill_marks_the_message_cancelled_and_held(self):
        await self.stream({"type": "room.kill", "message": {"hashid": "ms1"}})
        self.assertTrue(fa.was_cancelled("ms1"))
        self.assertTrue(fa.is_paused({"message": {"hashid": "ms1"}}))
        self.assertFalse(fa.was_cancelled("ms2"))

    async def test_kill_stops_the_live_process(self):
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        fa._current_proc = proc
        try:
            await self.stream({"type": "room.kill", "message": {"hashid": "ms1"}})
            self.assertIsNotNone(proc.wait(timeout=15))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    async def test_drop_removes_only_the_named_message(self):
        await self.stream(room_msg("ms1"), room_msg("ms2"),
                          {"type": "room.drop", "message": {"hashid": "ms1"}})
        self.assertEqual(["ms2"], [p["message"]["hashid"] for _, p in self.queued()])

    async def test_promote_moves_the_named_message_to_the_front(self):
        await self.stream(room_msg("ms1"), {"type": "approve.requested", "chat_id": "c"},
                          room_msg("ms2"),
                          {"type": "room.promote", "message": {"hashid": "ms2"}})
        order = [(k, (p.get("message") or {}).get("hashid")) for k, p in self.queued()]
        self.assertEqual([("room", "ms2"), ("room", "ms1"), ("approve", None)], order)

    async def test_an_edit_rewrites_the_queued_body(self):
        await self.stream(room_msg("ms1", body="old words"),
                          {"type": "room.message.edited", "room": {"hashid": "rm123456"},
                           "message": {"hashid": "ms1", "body": "new words"}})
        self.assertEqual("new words", self.queued()[0][1]["message"]["body"])

    async def test_an_edit_from_another_room_is_ignored(self):
        await self.stream(room_msg("ms1", body="old words"),
                          {"type": "room.message.edited", "room": {"hashid": "rmOTHER"},
                           "message": {"hashid": "ms1", "body": "new words"}})
        self.assertEqual("old words", self.queued()[0][1]["message"]["body"])


class FastLaneTest(ProcessStreamTestCase):
    async def test_a_fast_question_while_busy_runs_beside_the_build(self):
        fa._current_work = ("room", room_msg("msBUILD"))
        seen = []

        async def fake_fast(link, payload):
            seen.append(payload["message"]["hashid"])

        with mock.patch.object(fa, "_fast_lane_room", fake_fast):
            await self.stream(room_msg("msQ", fast_lane=True))
            await asyncio.sleep(0)
        self.assertEqual(["msQ"], seen)
        self.assertEqual([], self.queued(), "a parallel answer is not also queued")

    async def test_a_fast_question_while_idle_jumps_the_queue(self):
        fa._work_queue.put_nowait(("room", room_msg("msOLD")))
        await self.stream(room_msg("msQ", fast_lane=True))
        self.assertEqual(["msQ", "msOLD"],
                         [p["message"]["hashid"] for _, p in self.queued()])

    async def test_an_ordinary_message_while_busy_waits_its_turn(self):
        fa._current_work = ("room", room_msg("msBUILD"))
        with mock.patch.object(fa, "_fast_lane_room",
                               side_effect=AssertionError("not a fast-lane message")):
            await self.stream(room_msg("msNEXT"))
        self.assertEqual(["msNEXT"], [p["message"]["hashid"] for _, p in self.queued()])

    async def test_the_fast_lane_never_resumes_the_rooms_session(self):
        calls = []

        def run(prompt, project, on_event, allow_resume=True, session_key=None, *rest):
            calls.append((allow_resume, session_key))
            return "answer"

        link = fa.CableLink()
        with mock.patch.object(fa, "run_agent_streamed", run), \
             mock.patch.object(fa, "ROOM_REPLY_ACK_TIMEOUT_SECONDS", 0.01):
            await fa.handle_room_message(link, room_msg("msQ", fast_lane=True))
            await fa.handle_room_message(link, room_msg("msR"))
        self.assertEqual((False, "fast_msQ"), calls[0])
        self.assertEqual((True, fa._room_session_key("rm123456")), calls[1])


class EngineRoutingMatrixTest(unittest.TestCase):
    def test_each_engine_answers_only_its_own_agent_kind(self):
        expected = {"claude": "claude_code", "codex": "codex", "cursor": "cursor",
                    "gemini": "claude_code", "opencode": "claude_code", "amp": "claude_code",
                    "copilot_cli": "claude_code"}
        kinds = ("claude_code", "codex", "cursor", "copilot")
        for engine, mine in expected.items():
            with mock.patch.object(fa, "DISPATCH_ENGINE", engine):
                for kind in kinds:
                    with self.subTest(engine=engine, kind=kind):
                        self.assertEqual(kind == mine, fa._is_ours({"agent": {"kind": kind}}))

    def test_a_remote_agent_kind_is_never_answered_locally(self):
        for engine in fa.known_engines():
            with mock.patch.object(fa, "DISPATCH_ENGINE", engine):
                self.assertFalse(fa._is_ours({"agent": {"kind": "copilot"}}), engine)

    def test_whitespace_around_the_kind_does_not_misroute(self):
        with mock.patch.object(fa, "DISPATCH_ENGINE", "codex"):
            self.assertTrue(fa._is_ours({"agent": {"kind": " codex "}}))
            self.assertFalse(fa._is_ours({"agent": {"kind": " claude_code "}}))

    def test_an_agent_block_that_is_not_an_object_is_treated_as_absent(self):
        with mock.patch.object(fa, "DISPATCH_ENGINE", "codex"):
            for agent in ("codex", ["codex"], 7):
                self.assertTrue(fa._is_ours({"agent": agent}), agent)


class RoomMessageOutcomeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved = (fa.ROOM_REPLY_ACK_TIMEOUT_SECONDS, set(fa._cancelled_messages),
                       fa._work_queue)
        fa.ROOM_REPLY_ACK_TIMEOUT_SECONDS = 0.01
        fa._work_queue = asyncio.Queue()
        self.link = fa.CableLink()

    async def asyncTearDown(self):
        (fa.ROOM_REPLY_ACK_TIMEOUT_SECONDS, cancelled, fa._work_queue) = self._saved
        fa._cancelled_messages.clear()
        fa._cancelled_messages.update(cancelled)

    async def handle(self, runner, payload=None):
        with mock.patch.object(fa, "run_agent_streamed", runner):
            await fa.handle_room_message(self.link, payload or room_msg("ms1"))

    def bodies(self):
        return [f["body"] for f in frames(self.link, "room_reply")]

    async def test_a_message_for_another_engine_sends_nothing_at_all(self):
        def boom(*a, **k):
            raise AssertionError("must not run")

        with mock.patch.object(fa, "DISPATCH_ENGINE", "claude"):
            await self.handle(boom, room_msg("ms1", agent={"kind": "codex", "name": "Codex"}))
        self.assertEqual([], self.link.outbox)

    async def test_no_room_hashid_is_ignored(self):
        await self.handle(lambda *a, **k: "x", {"type": "room.message", "message": {"body": "hi"}})
        self.assertEqual([], self.link.outbox)

    async def test_an_empty_answer_says_so_and_records_an_error_run(self):
        await self.handle(lambda *a, **k: "")
        self.assertEqual(["(I came back with an empty response.)"], self.bodies())
        runs = frames(self.link, "room_run")
        self.assertEqual(1, len(runs))
        self.assertEqual("error", runs[0]["status"])

    async def test_a_crash_is_reported_in_the_room(self):
        def crash(*a, **k):
            raise RuntimeError("claude exited 1 with no output")

        await self.handle(crash)
        self.assertEqual(1, len(self.bodies()))
        self.assertIn("RuntimeError: claude exited 1 with no output", self.bodies()[0])
        self.assertEqual("ms1", frames(self.link, "room_reply")[0]["reply_to"])
        self.assertEqual("error", frames(self.link, "room_run")[0]["status"])

    async def test_a_timeout_is_reported_in_the_room(self):
        def slow(*a, **k):
            raise subprocess.TimeoutExpired(["claude"], 360)

        await self.handle(slow)
        self.assertIn("timed out", self.bodies()[0])
        self.assertEqual("error", frames(self.link, "room_run")[0]["status"])

    async def test_a_stop_the_operator_asked_for_is_held_not_reported_as_a_crash(self):
        def stopped(*a, **k):
            fa._cancelled_messages.add("ms1")
            raise RuntimeError("killed")

        payload = room_msg("ms1")
        await self.handle(stopped, payload)
        self.assertEqual([], self.bodies())
        self.assertEqual([("room", payload)], [fa._work_queue.get_nowait()])

    async def test_a_stop_that_ends_the_stream_cleanly_is_also_held(self):
        def stopped(*a, **k):
            fa._cancelled_messages.add("ms1")
            return "partial narration"

        await self.handle(stopped)
        self.assertEqual([], self.bodies())
        self.assertEqual(1, fa._work_queue.qsize())

    async def test_a_stopped_fast_lane_question_is_not_requeued(self):
        def stopped(*a, **k):
            fa._cancelled_messages.add("ms1")
            raise RuntimeError("killed")

        await self.handle(stopped, room_msg("ms1", fast_lane=True))
        self.assertTrue(fa._work_queue.empty())

    async def test_a_long_answer_is_split_and_every_chunk_threads_under_the_question(self):
        long = "\n\n".join(f"paragraph {i} " + "x" * 900 for i in range(12))
        await self.handle(lambda *a, **k: long)
        replies = frames(self.link, "room_reply")
        self.assertGreater(len(replies), 1)
        self.assertTrue(all(len(r["body"]) <= fa.ROOM_BODY_MAX for r in replies))
        self.assertTrue(all(r["reply_to"] == "ms1" for r in replies))
        self.assertEqual(long.replace("\n", ""), "".join(r["body"] for r in replies).replace("\n", ""))

    async def test_the_run_is_recorded_after_the_answer(self):
        def run(prompt, project, on_event, *rest):
            on_event({"type": "result", "usage": {"input_tokens": 5}, "cost_usd": 0.01,
                      "duration_ms": 1200, "num_turns": 2, "is_error": False})
            return "done"

        await self.handle(run)
        actions = [json.loads(json.loads(f)["data"])["action"] for f in self.link.outbox]
        self.assertLess(actions.index("room_reply"), actions.index("room_run"))
        record = frames(self.link, "room_run")[0]
        self.assertEqual("ok", record["status"])
        self.assertEqual({"input_tokens": 5}, record["usage"])
        self.assertEqual(0.01, record["cost_usd"])
        self.assertEqual(fa.AGENT_VERSION, record["agent_version"])

    async def test_telemetry_off_keeps_the_model_out_of_the_run_and_the_status(self):
        with mock.patch.object(fa, "TELEMETRY_ENABLED", False), \
             mock.patch.object(fa, "_last_model", "claude-secret-model"):
            await self.handle(lambda *a, **k: "done")
            await fa.room_status(self.link, "rm123456", "ms1", "working", model="named-model")
        self.assertIsNone(frames(self.link, "room_run")[0]["model"])
        self.assertNotIn("model", frames(self.link, "room_status")[-1])
        self.assertNotIn("claude-secret-model", json.dumps(
            [json.loads(json.loads(f)["data"]) for f in self.link.outbox]))

    async def test_telemetry_on_reports_the_model(self):
        with mock.patch.object(fa, "TELEMETRY_ENABLED", True), \
             mock.patch.object(fa, "_last_model", "claude-x"):
            await self.handle(lambda *a, **k: "done")
        self.assertEqual("claude-x", frames(self.link, "room_run")[0]["model"])

    async def test_the_prompt_the_agent_gets_carries_the_message(self):
        seen = {}

        def run(prompt, *rest):
            seen["prompt"] = prompt
            return "ok"

        await self.handle(run, room_msg("ms1", body="what version are we on?"))
        self.assertIn("what version are we on?", seen["prompt"])


class PausedWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_held_message_goes_to_the_back_and_the_next_one_runs(self):
        saved = (fa._work_queue, fa._current_work, set(fa._paused_messages),
                 fa.PAUSED_REQUEUE_DELAY_SECONDS, fa._restart_scheduled)
        fa._work_queue = asyncio.Queue()
        fa._paused_messages.add("msHELD")
        fa.PAUSED_REQUEUE_DELAY_SECONDS = 0
        fa._restart_scheduled = False
        handled = []

        async def fake_room(link, payload):
            handled.append(payload["message"]["hashid"])

        fa._work_queue.put_nowait(("room", room_msg("msHELD")))
        fa._work_queue.put_nowait(("room", room_msg("msNEXT")))
        with mock.patch.object(fa, "handle_room_message", fake_room), \
             mock.patch.object(fa, "restart_if_self_updated", mock.AsyncMock()), \
             mock.patch.object(fa, "run_deferred_update", mock.AsyncMock()), \
             mock.patch.object(fa, "room_status", mock.AsyncMock()):
            worker = asyncio.create_task(fa.worker_loop(fa.CableLink()))
            for _ in range(200):
                if handled:
                    break
                await asyncio.sleep(0.005)
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker
        try:
            self.assertEqual(["msNEXT"], handled)
            self.assertEqual(["msHELD"], [p["message"]["hashid"]
                                          for _, p in [fa._work_queue.get_nowait()]])
        finally:
            (fa._work_queue, fa._current_work, paused,
             fa.PAUSED_REQUEUE_DELAY_SECONDS, fa._restart_scheduled) = saved
            fa._paused_messages.clear()
            fa._paused_messages.update(paused)


class HeldWorkSurvivesShutdownTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_held_message_is_spooled_while_the_worker_waits_on_it(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        saved = (fa._work_queue, fa._current_work, set(fa._paused_messages),
                 fa.PAUSED_REQUEUE_DELAY_SECONDS, fa._restart_scheduled,
                 fa.STATE_DIR)
        fa._work_queue = asyncio.Queue()
        fa._current_work = None
        fa._paused_messages.add("msHELD")
        fa.PAUSED_REQUEUE_DELAY_SECONDS = 5
        fa._restart_scheduled = False
        fa.STATE_DIR = state
        fa._work_queue.put_nowait(("room", room_msg("msHELD")))
        try:
            worker = asyncio.create_task(fa.worker_loop(fa.CableLink()))
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(1, fa.spool_pending_work(),
                             "a held message waiting out its delay must survive a SIGTERM")
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker
            self.assertEqual(["msHELD"], [i["payload"]["message"]["hashid"]
                                          for i in fa.take_spooled_work()])
        finally:
            (fa._work_queue, fa._current_work, paused, fa.PAUSED_REQUEUE_DELAY_SECONDS,
             fa._restart_scheduled, fa.STATE_DIR) = saved
            fa._paused_messages.clear()
            fa._paused_messages.update(paused)


class FeedbackProposalTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "vroxy_web"
        self.repo.mkdir()
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@x.test"],
                     ["config", "user.name", "T"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True,
                           timeout=30)
        (self.repo / "app.rb").write_text("puts 1\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, timeout=30)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.repo, check=True, timeout=30)
        self._saved = (fa.CODE_ROOT, fa.PROJECT, fa.STREAM_ENABLED)
        fa.CODE_ROOT, fa.PROJECT, fa.STREAM_ENABLED = self.root, "vroxy_web", True
        self.link = fa.CableLink()
        self.seen_dirs = []

    async def asyncTearDown(self):
        fa.CODE_ROOT, fa.PROJECT, fa.STREAM_ENABLED = self._saved
        self.tmp.cleanup()

    def payload(self, **extra):
        return {"type": "feedback.created", "chat": {"hashid": "ch1"},
                "feedback": {"hashid": "fb1", "note": "make the title bigger"},
                "message": {"hashid": "ms1", "body": "make the title bigger"}, **extra}

    def editing_run(self, answer=""):
        def run(prompt, project, on_event, allow_resume, session_key, work_dir, *rest):
            work_dir = Path(work_dir)
            self.seen_dirs.append(work_dir)
            (work_dir / "app.rb").write_text("puts 2\n")
            (work_dir / "new.rb").write_text("fresh\n")
            on_event({"type": "tool_use", "name": "Edit", "input": {"file_path": "app.rb"}})
            return answer
        return run

    async def handle(self, runner, payload=None):
        with mock.patch.object(fa, "run_agent_streamed", runner):
            await fa.handle_feedback(self.link, payload or self.payload())
        for _ in range(20):
            await asyncio.sleep(0)

    def siblings(self):
        return sorted(p.name for p in self.root.iterdir())

    async def test_the_run_happens_in_a_sibling_worktree_and_the_checkout_is_untouched(self):
        await self.handle(self.editing_run("Bigger title."))

        self.assertEqual(1, len(self.seen_dirs))
        self.assertEqual(self.root, self.seen_dirs[0].parent)
        self.assertNotEqual(self.repo, self.seen_dirs[0])
        self.assertTrue(self.seen_dirs[0].name.startswith(".dispatch-vroxy_web-"))
        self.assertEqual("puts 1\n", (self.repo / "app.rb").read_text())
        self.assertFalse((self.repo / "new.rb").exists())
        self.assertEqual(["vroxy_web"], self.siblings(), "the worktree is removed afterwards")

    async def test_edits_without_a_fence_are_salvaged_into_a_proposal(self):
        await self.handle(self.editing_run("Bigger title."))

        reply = frames(self.link, "reply")[-1]
        self.assertEqual("code_proposal", reply["kind"])
        files = {f["path"]: f["content"] for f in reply["proposal"]["files"]}
        self.assertEqual({"app.rb": "puts 2\n", "new.rb": "fresh\n"}, files)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True,
                              text=True, timeout=30).stdout.strip()
        self.assertEqual(head, reply["proposal"]["base_sha"])
        self.assertGreaterEqual(reply["proposal"]["stats"]["insertions"], 1)

    async def test_the_investigation_chip_and_tool_chips_reach_the_chat(self):
        await self.handle(self.editing_run("done"))
        names = [f["name"] for f in frames(self.link, "progress")]
        self.assertEqual("investigating…", names[0])
        self.assertIn("Edit", names)

    async def test_a_plain_answer_with_no_edits_is_a_plain_reply(self):
        def run(*a, **k):
            return "It is already 24px."

        await self.handle(run)
        reply = frames(self.link, "reply")[-1]
        self.assertEqual("assistant", reply["kind"])
        self.assertEqual("It is already 24px.", reply["body"])
        self.assertNotIn("proposal", reply)

    async def test_nothing_at_all_says_so(self):
        await self.handle(lambda *a, **k: "")
        self.assertEqual("(Claude returned an empty response.)",
                         frames(self.link, "reply")[-1]["body"])

    async def test_a_crash_is_reported_and_the_worktree_still_goes(self):
        def crash(*a, **k):
            raise ValueError("bad flag")

        with mock.patch.object(fa, "run_claude", side_effect=RuntimeError("fallback died")):
            await self.handle(crash)
        self.assertIn("fallback died", frames(self.link, "reply")[-1]["body"])
        self.assertEqual(["vroxy_web"], self.siblings())

    async def test_feedback_routed_to_another_engine_is_left_alone(self):
        def boom(*a, **k):
            raise AssertionError("must not run")

        with mock.patch.object(fa, "DISPATCH_ENGINE", "claude"):
            await self.handle(boom, self.payload(agent={"kind": "codex"}))
        self.assertEqual([], self.link.outbox)
        self.assertEqual(["vroxy_web"], self.siblings())

    async def test_feedback_with_no_chat_is_dropped(self):
        await self.handle(lambda *a, **k: "x", {"type": "feedback.created", "feedback": {}})
        self.assertEqual([], self.link.outbox)


if __name__ == "__main__":
    unittest.main()
