import os

os.environ.setdefault("LOG_FILE", "")

import asyncio
import json
import stat
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import feedback_agent as fa
from tests.test_agent_runner import FakeClaudeTestCase


def wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class ClaudeSteerTest(FakeClaudeTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(fa, "STALL_SECONDS", 3)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stdin_log = self.root / "stdin.log"

    def echo_script(self):
        self.script(f"""
            read -r first
            printf '%s\\n' "$first" >> "{self.stdin_log}"
            echo '{{"type":"assistant","session_id":"s1","message":{{"content":[{{"type":"text","text":"working"}}]}}}}'
            read -r -t 5 second
            printf '%s\\n' "$second" >> "{self.stdin_log}"
            echo '{{"type":"result","session_id":"s1","result":"done","usage":{{}}}}'
            cat > /dev/null
            echo '{{"type":"assistant","session_id":"s1","message":{{"content":[{{"type":"text","text":"stdin closed"}}]}}}}'
        """)

    def test_the_prompt_goes_in_on_stdin_and_a_steer_lands_mid_turn(self):
        self.echo_script()
        out = {}
        worker = threading.Thread(target=lambda: out.setdefault("text", fa.run_claude_streamed(
            "the prompt", "vroxy_web", self.events.append, True, "room_rm1", None, None, "msg1")))
        worker.start()
        self.assertTrue(wait_for(lambda: any(e.get("type") == "text_delta" for e in self.events)))
        self.assertTrue(fa.steer_live_run("msg1", "also do X"))
        worker.join(10)
        self.assertFalse(worker.is_alive(), "closing stdin after the result lets claude exit")

        sent = [json.loads(line) for line in self.stdin_log.read_text().splitlines()]
        self.assertEqual(sent[0]["message"]["content"], "the prompt")
        self.assertEqual(sent[1]["message"]["content"], "also do X")
        self.assertNotIn("the prompt", self.calls()[0])
        self.assertIn("--input-format stream-json", self.calls()[0])
        self.assertNotIn("msg1", fa._live_inputs)
        self.assertFalse(fa.steer_live_run("msg1", "too late"))

    def test_a_steer_that_becomes_a_second_turn_keeps_both_answers_and_costs(self):
        self.script(f"""
            read -r first
            echo '{{"type":"result","session_id":"s1","result":"ALPHA","usage":{{"output_tokens":2}},"total_cost_usd":0.01,"num_turns":1}}'
            echo '{{"type":"result","session_id":"s1","result":"BETA","usage":{{"output_tokens":3}},"total_cost_usd":0.02,"num_turns":1}}'
        """)
        text = self.run_it()
        self.assertEqual(text, "ALPHA\n\nBETA")
        last = [e for e in self.events if e.get("type") == "result"][-1]
        self.assertEqual(last["usage"], {"output_tokens": 5})
        self.assertAlmostEqual(last["cost_usd"], 0.03)
        self.assertEqual(last["num_turns"], 2)

    def test_a_run_without_a_cancel_key_cannot_be_steered(self):
        self.echo_script()
        worker = threading.Thread(target=lambda: fa.run_claude_streamed(
            "p", "vroxy_web", self.events.append, True, "room_rm1"))
        worker.start()
        self.assertTrue(wait_for(lambda: bool(self.events)))
        self.assertEqual(fa._live_inputs, {})
        worker.join(10)


class ClaudeSubagentEventTest(FakeClaudeTestCase):
    def test_agent_tasks_start_and_finish_and_shells_are_ignored(self):
        lines = [
            {"type": "system", "subtype": "task_started", "task_id": "a1",
             "task_type": "local_agent", "subagent_type": "Explore",
             "description": "find auth", "prompt": "Find the auth code", "is_backgrounded": False},
            {"type": "system", "subtype": "task_started", "task_id": "b1",
             "task_type": "local_bash", "description": "sleep 8", "parent_task_id": "a1"},
            {"type": "system", "subtype": "task_notification", "task_id": "b1", "status": "completed"},
            {"type": "system", "subtype": "task_notification", "task_id": "a1",
             "status": "completed", "summary": "It is in app/auth.rb"},
            {"type": "result", "session_id": "s", "result": "ok", "usage": {}},
        ]
        self.script(self.emit(*[json.dumps(line) for line in lines]))
        self.run_it()
        agents = [e for e in self.events if e.get("type") == "agent"]
        self.assertEqual([(a["id"], a["state"]) for a in agents], [("a1", "started"), ("a1", "done")])
        self.assertEqual(agents[0]["agent_type"], "Explore")
        self.assertEqual(agents[0]["prompt"], "Find the auth code")
        self.assertEqual(agents[1]["summary"], "It is in app/auth.rb")

    def test_a_failed_task_is_an_error(self):
        open_agents = {}
        fa._claude_task_event({"subtype": "task_started", "task_id": "x",
                               "task_type": "local_agent"}, open_agents)
        ended = fa._claude_task_event({"subtype": "task_notification", "task_id": "x",
                                       "status": "failed"}, open_agents)
        self.assertEqual(ended["state"], "error")
        self.assertIsNone(fa._claude_task_event({"subtype": "task_notification", "task_id": "x",
                                                 "status": "completed"}, open_agents))


class SubagentFromToolTest(unittest.TestCase):
    def test_spawn_tools_across_harnesses(self):
        cases = [
            ("Task", {"description": "scan", "prompt": "Scan it", "subagent_type": "general"}),
            ("invoke_agent", {"agent_name": "codebase_investigator", "prompt": "Look"}),
            ("Spawn_agent", {"task": "Write tests"}),
            ("Use_subagent", {"initialQuery": "Check the build"}),
        ]
        for name, args in cases:
            agent = fa._subagent_from_tool(name, args)
            self.assertEqual(agent["state"], "started", name)
            self.assertTrue(agent["prompt"], name)
            self.assertTrue(agent["description"], name)
        self.assertEqual(fa._subagent_from_tool("invoke_agent", {"agent_name": "x", "prompt": "p"})["agent_type"], "x")

    def test_ordinary_tools_are_not_agents(self):
        self.assertIsNone(fa._subagent_from_tool("Bash", {"command": "ls"}))
        self.assertIsNone(fa._subagent_from_tool(None, None))


class AgentProgressHashTest(unittest.TestCase):
    def test_shape_and_caps(self):
        agent = fa._agent_progress_hash({"type": "agent", "id": "a1", "state": "started",
                                         "agent_type": "Explore", "prompt": "p" * 9000,
                                         "description": "find it", "engine": "claude"})
        self.assertEqual(agent["type"], "Explore")
        self.assertEqual(len(agent["prompt"]), fa.AGENT_PROMPT_MAX)
        self.assertEqual(fa._agent_progress_line(agent), "Explore started — find it")

    def test_rejects_missing_id_or_unknown_state(self):
        self.assertIsNone(fa._agent_progress_hash({"state": "started"}))
        self.assertIsNone(fa._agent_progress_hash({"id": "a", "state": "paused"}))


class NewHarnessParserTest(unittest.TestCase):
    def test_cline_stream(self):
        _, sid, model = fa._cline_shaped({"type": "run_start", "sessionId": "c1", "modelId": "m1"})
        self.assertEqual((sid, model), ("c1", "m1"))
        events, _, _ = fa._cline_shaped({"type": "agent_event", "event": {
            "type": "content_start", "contentType": "tool", "toolName": "read_file",
            "input": {"path": "a.txt"}}})
        self.assertEqual(events[0]["name"], "Read_file")
        events, _, _ = fa._cline_shaped({"type": "agent_event", "event": {
            "type": "content_start", "contentType": "reasoning", "reasoning": "hmm"}})
        self.assertEqual(events[0], {"type": "thinking", "text": "hmm"})
        events, _, _ = fa._cline_shaped({"type": "run_result", "finishReason": "error",
                                         "usage": {"inputTokens": 3, "outputTokens": 4,
                                                   "totalCost": 0.01}})
        self.assertEqual(events[0]["usage"], {"input_tokens": 3, "output_tokens": 4})
        self.assertTrue(events[0]["is_error"])

    def test_kiro_stream(self):
        events, sid, _ = fa._kiro_shaped({"type": "sessionUpdate", "data": {
            "sessionId": "k1", "update": {"sessionUpdate": "tool_call", "title": "fs_read",
                                          "rawInput": {"path": "x"}}}})
        self.assertEqual(sid, "k1")
        self.assertEqual(events[0]["type"], "tool_use")
        events, _, _ = fa._kiro_shaped({"type": "sessionUpdate", "data": {
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}}}})
        self.assertEqual(events, [{"type": "text_delta", "text": "hi"}])
        events, _, _ = fa._kiro_shaped({"type": "runFinished", "data": {"finalText": "bye"}})
        self.assertEqual(events[0]["final_text"], "bye")

    def test_openclaw_envelope(self):
        events, sid, model = fa._openclaw_shaped({
            "ok": True, "final": "Answer", "sessionId": "o1", "model": "gpt",
            "usage": {"input": 5, "output": 6}, "costUsd": 0.2,
            "toolSummary": {"calls": 1, "tools": ["read"]}})
        self.assertEqual((sid, model), ("o1", "gpt"))
        self.assertEqual([e["type"] for e in events], ["tool_use", "text_delta", "result"])
        self.assertEqual(events[-1]["usage"], {"input_tokens": 5, "output_tokens": 6})

    def test_openclaw_failure_names_the_error(self):
        events, _, _ = fa._openclaw_shaped({"ok": False, "error": "no provider"})
        self.assertTrue(events[-1]["is_error"])
        self.assertEqual(events[-1]["final_text"], "no provider")

    def test_openclaw_only_passes_cwd_to_exec(self):
        argv = fa.HARNESS_SPECS["openclaw"]["argv"]
        self.assertIn("--cwd", argv("oc", "p", None, "/w"))
        self.assertNotIn("--cwd", argv("oc", "p", "sid", "/w"))

    def test_new_engines_are_known_and_windsurf_is_devin(self):
        for engine in ("cline", "kiro", "devin", "openclaw"):
            self.assertIn(engine, fa.known_engines())
        self.assertEqual(fa.normalize_engine("windsurf"), "devin")
        self.assertEqual(fa.normalize_engine("kiro-cli"), "kiro")


class PreflightAndPlainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self._tmpdir())
        (self.tmp / "vroxy_web").mkdir()
        for name, value in (("CODE_ROOT", self.tmp), ("SID_DIR", self.tmp / "sids"),
                            ("STALL_SECONDS", 2), ("STALL_WINDOWS_BEFORE_KILL", 2),
                            ("_last_model", None), ("_current_proc", None)):
            patcher = mock.patch.object(fa, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _tmpdir(self):
        import tempfile
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return d.name

    def fake(self, name, body):
        path = self.tmp / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_kiro_logged_out_fails_fast_without_starting_a_chat(self):
        marker = self.tmp / "chat.ran"
        binpath = self.fake("kiro-cli", f"""
if [ "$1" = whoami ]; then echo '{{"account":null}}'; exit 1; fi
touch "{marker}"
""")
        with mock.patch.dict(os.environ, {"KIRO_CLI_BIN": str(binpath)}):
            with self.assertRaises(RuntimeError) as ctx:
                fa.run_harness_streamed("kiro", "hi", "vroxy_web", lambda e: None)
        self.assertIn("Kiro CLI cannot run a turn", str(ctx.exception))
        self.assertFalse(marker.exists())

    def test_devin_plain_text_is_the_answer(self):
        binpath = self.fake("devin", """
if [ "$1" = auth ]; then echo "Logged in as test"; exit 0; fi
echo "line one"
echo ""
echo '{"looks": "like json"}'
""")
        events = []
        with mock.patch.dict(os.environ, {"DEVIN_BIN": str(binpath)}):
            text = fa.run_harness_streamed("devin", "hi", "vroxy_web", events.append)
        self.assertEqual(text, 'line one\n\n{"looks": "like json"}')
        self.assertIn({"type": "text_delta", "text": "line one"}, events)

    def test_devin_logged_out_is_caught_despite_exit_zero(self):
        binpath = self.fake("devin", 'if [ "$1" = auth ]; then echo "Not logged in."; exit 0; fi\necho ran\n')
        with mock.patch.dict(os.environ, {"DEVIN_BIN": str(binpath)}):
            with self.assertRaises(RuntimeError):
                fa.run_harness_streamed("devin", "hi", "vroxy_web", lambda e: None)


class SteerRoutingTest(unittest.TestCase):
    def setUp(self):
        self.sent = []

        async def fake_cable_send(ws, command, data=None, **kwargs):
            self.sent.append(data)

        for name, value in (("cable_send", fake_cable_send), ("_room_inflight", {"rm1": "msg1"}),
                            ("_live_inputs", {})):
            patcher = mock.patch.object(fa, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def payload(self, body="/steer also do X"):
        return {"room": {"hashid": "rm1"}, "message": {"hashid": "msg2", "body": body},
                "sender": {"name": "Sarah"}}

    def test_strip_steer(self):
        payload = self.payload("/claude-code /steer also do X")
        fa._strip_steer(payload)
        self.assertEqual(payload["message"]["body"], "also do X")

    def test_a_bare_steer_strips_to_nothing_and_is_not_injected(self):
        live = mock.Mock()
        fa._live_inputs["msg1"] = live
        payload = self.payload("/steer")
        fa._strip_steer(payload)
        self.assertEqual(payload["message"]["body"], "")
        with mock.patch.object(fa, "DISPATCH_ENGINE", "claude"):
            self.assertFalse(asyncio.run(fa.steer_room_message(None, payload)))
        live.send.assert_not_called()

    def test_claude_takes_the_steer(self):
        live = mock.Mock()
        live.send.return_value = True
        fa._live_inputs["msg1"] = live
        payload = self.payload()
        fa._strip_steer(payload)
        with mock.patch.object(fa, "DISPATCH_ENGINE", "claude"):
            self.assertTrue(asyncio.run(fa.steer_room_message(None, payload)))
        self.assertIn("also do X", live.send.call_args[0][0])
        self.assertIn("Sarah", live.send.call_args[0][0])
        states = [d.get("state") for d in self.sent if d.get("action") == "room_status"]
        self.assertEqual(states, ["steered"])
        progress = [d for d in self.sent if d.get("action") == "room_progress"]
        self.assertEqual(progress[0]["reply_to"], "msg1")

    def test_an_engine_that_cannot_steer_falls_back(self):
        fa._live_inputs["msg1"] = mock.Mock()
        with mock.patch.object(fa, "DISPATCH_ENGINE", "codex"):
            self.assertFalse(asyncio.run(fa.steer_room_message(None, self.payload())))
        self.assertEqual(self.sent, [])

    def test_nothing_in_flight_falls_back(self):
        fa._room_inflight.clear()
        with mock.patch.object(fa, "DISPATCH_ENGINE", "claude"):
            self.assertFalse(asyncio.run(fa.steer_room_message(None, self.payload())))


if __name__ == "__main__":
    unittest.main()
