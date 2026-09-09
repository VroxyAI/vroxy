"""
Unit tests for the pure-Python bits of feedback_agent.py — prompt
building and Claude-output parsing.  No websocket or subprocess
here; those get exercised in integration when we point the agent
at a real vroxy_web instance.
"""

import json
import re
import os

# Before feedback_agent is imported: it configures logging at import
# time, and the suite exercises the real reply/restart paths.  Without
# this every test run writes fake "Room reply sent" lines into the
# operator's log/dispatch.log and makes the real history unreadable.
os.environ.setdefault("LOG_FILE", "")

import asyncio
import subprocess
import time
import tempfile
import shutil
import logging
import unittest
from pathlib import Path

import feedback_agent as fa


class BuildPromptTest(unittest.TestCase):
    def test_full_payload_renders_all_sections(self):
        payload = {
            "type": "feedback.created",
            "feedback": {
                "hashid":            "abc12345",
                "note":              "make this bigger",
                "page_url":          "https://vroxy.ai/dashboard",
                "page_path":         "/dashboard",
                "controller_action": "workspace/dashboard#show",
                "viewport_width":    1440,
                "viewport_height":   900,
                "rendered_partials": [
                    "app/views/workspace/dashboard/_hero.html.erb|1.2ms",
                    "app/views/shared/_topnav.html.erb|0.4ms",
                ],
                "selector":     "main h2",
                "element_tag":  "h2",
                "element_text": "Welcome",
                "element_html": "<h2>Welcome</h2>",
            },
            "chat":    {"hashid": "chatidxx", "title": "Feedback #abc"},
            "message": {"hashid": "msgidxx", "body": "make this bigger"},
        }
        prompt = fa.build_prompt(payload)
        self.assertIn("make this bigger", prompt)
        self.assertIn("workspace/dashboard#show", prompt)
        self.assertIn("_hero.html.erb", prompt)
        # Timing suffix must be stripped when rendering partial paths.
        self.assertNotIn("|1.2ms", prompt)
        self.assertIn("main h2", prompt)
        self.assertIn("chatidxx", prompt)

    def test_page_level_note_flags_no_element(self):
        payload = {
            "feedback": {"note": "add a dark-mode toggle",
                         "page_url": "https://vroxy.ai/"},
            "chat":     {"hashid": "chatid1"},
        }
        self.assertIn("No element picked", fa.build_prompt(payload))

    def test_missing_keys_are_tolerated(self):
        # A sparse payload should never blow up prompt building — the
        # agent needs to degrade gracefully when a broadcast is thin.
        prompt = fa.build_prompt({"feedback": {"note": "hi"},
                                   "chat": {"hashid": "x"}})
        self.assertIn("hi", prompt)

    def test_duplicate_partials_are_collapsed(self):
        # vroxy stores rendered_partials as an ordered list; the
        # same partial can appear twice (rendered from two collection
        # loops).  Prompt should de-dupe so Claude doesn't waste a
        # tool call re-reading it.  Uses `_widget_row` (not present
        # in SYSTEM_PROMPT examples) to keep the count assertion
        # unambiguous.
        payload = {
            "feedback": {
                "note": "x",
                "rendered_partials": [
                    "app/views/tenants/_widget_row.html.erb|1.2ms",
                    "app/views/tenants/_widget_row.html.erb|0.8ms",
                    "app/views/tenants/_tenant_card.html.erb|0.3ms",
                ],
            },
            "chat": {"hashid": "c"},
        }
        prompt = fa.build_prompt(payload)
        self.assertEqual(1, prompt.count("_widget_row.html.erb"))
        self.assertEqual(1, prompt.count("_tenant_card.html.erb"))


class ParseProposalTest(unittest.TestCase):
    def test_plain_text_is_returned_unchanged(self):
        body, proposal = fa.parse_proposal("just a plain explanation")
        self.assertEqual("just a plain explanation", body)
        self.assertIsNone(proposal)

    def test_fenced_inline_ship_proposal_is_extracted(self):
        raw = (
            "Small tweak — flip the padding on the hero.\n\n"
            "```proposal\n"
            + json.dumps({
                "mode":    "inline_ship",
                "summary": "tighten hero padding",
                "files":   [{"path": "app/views/workspace/dashboard/_hero.html.erb",
                             "content": "<div>...</div>"}]
            })
            + "\n```\n"
        )
        body, proposal = fa.parse_proposal(raw)
        self.assertIn("Small tweak", body)
        self.assertNotIn("proposal", body.lower().split("\n")[-1])
        self.assertEqual("inline_ship", proposal["mode"])
        self.assertEqual(1, len(proposal["files"]))

    def test_invalid_json_falls_back_to_plain_text(self):
        raw = "here you go:\n```proposal\n{not json}\n```"
        body, proposal = fa.parse_proposal(raw)
        # Whole raw output returned as body when JSON parsing fails —
        # safer than silently dropping the fence.
        self.assertEqual(raw, body)
        self.assertIsNone(proposal)

    def test_pull_request_proposal_survives_round_trip(self):
        raw = (
            "Bigger change — needs a migration.\n\n"
            "```proposal\n"
            + json.dumps({
                "mode":    "pull_request",
                "summary": "add a foo column",
                "notes":   "adds an AR migration + touches the model"
            })
            + "\n```\n"
        )
        body, proposal = fa.parse_proposal(raw)
        self.assertEqual("pull_request", proposal["mode"])
        self.assertIn("Bigger change", body)


def ask_fence(payload) -> str:
    return "```ask\n" + json.dumps(payload) + "\n```"


class ParseRoomAskTest(unittest.TestCase):
    """A fenced ask is a question the room can render as buttons. A
    fence it can't use must cost the reply nothing."""

    def test_no_block_returns_the_reply_untouched(self):
        body, ask = fa.parse_room_ask("Shipped it — 3 files, no migration.")
        self.assertEqual("Shipped it — 3 files, no migration.", body)
        self.assertIsNone(ask)

    def test_valid_block_is_extracted_and_the_fence_is_stripped(self):
        raw = ("Both work. Which do you want?\n\n"
               + ask_fence({"prompt": "Ship this to master or open a PR?",
                            "mode": "one",
                            "options": ["Ship to master", "Open a PR"]}))
        body, ask = fa.parse_room_ask(raw)
        self.assertEqual("Both work. Which do you want?", body)
        self.assertNotIn("```", body)
        self.assertNotIn("prompt", body)
        self.assertEqual("one", ask["mode"])
        self.assertEqual("Ship this to master or open a PR?", ask["prompt"])
        self.assertEqual([{"label": "Ship to master", "value": "Ship to master"},
                          {"label": "Open a PR", "value": "Open a PR"}], ask["options"])

    def test_label_value_pairs_survive(self):
        raw = ask_fence({"prompt": "Which repo?", "mode": "one",
                         "options": [{"label": "vroxy_web", "value": "web"},
                                     {"label": "vroxy_mobile", "value": "mobile"}]})
        _, ask = fa.parse_room_ask(raw)
        self.assertEqual(["web", "mobile"], [o["value"] for o in ask["options"]])

    def test_malformed_json_leaves_the_reply_alone(self):
        raw = "Here's the question:\n```ask\n{not json,}\n```"
        body, ask = fa.parse_room_ask(raw)
        self.assertEqual(raw, body)
        self.assertIsNone(ask)

    def test_options_without_a_prompt_are_not_a_question(self):
        raw = "text\n" + ask_fence({"mode": "one", "options": ["a", "b"]})
        body, ask = fa.parse_room_ask(raw)
        self.assertEqual(raw, body)
        self.assertIsNone(ask)

    def test_pick_one_with_no_options_is_unanswerable(self):
        raw = "text\n" + ask_fence({"prompt": "Which?", "mode": "one", "options": []})
        body, ask = fa.parse_room_ask(raw)
        self.assertEqual(raw, body)
        self.assertIsNone(ask)

    def test_text_mode_needs_no_options(self):
        raw = "What should I call it?\n\n" + ask_fence(
            {"prompt": "What should the column be called?", "mode": "text"})
        body, ask = fa.parse_room_ask(raw)
        self.assertEqual("What should I call it?", body)
        self.assertEqual("text", ask["mode"])
        self.assertEqual([], ask["options"])

    def test_thirty_options_are_cut_to_the_twelve_the_room_stores(self):
        raw = ask_fence({"prompt": "Pick a file", "mode": "many",
                         "options": [f"file_{i}.rb" for i in range(30)]})
        _, ask = fa.parse_room_ask(raw)
        self.assertEqual(12, len(ask["options"]))
        self.assertEqual("file_0.rb", ask["options"][0]["label"])
        self.assertEqual("file_11.rb", ask["options"][-1]["label"])

    def test_duplicates_and_blanks_are_dropped_before_the_cap(self):
        raw = ask_fence({"prompt": "Pick", "mode": "one",
                         "options": ["a", "a", "   ", None, {"label": ""}, "b"]})
        _, ask = fa.parse_room_ask(raw)
        self.assertEqual(["a", "b"], [o["label"] for o in ask["options"]])

    def test_unknown_mode_falls_back_to_buttons(self):
        raw = ask_fence({"prompt": "Pick", "mode": "dropdown", "options": ["a"]})
        _, ask = fa.parse_room_ask(raw)
        self.assertEqual("one", ask["mode"])

    def test_long_prompt_and_labels_are_capped(self):
        raw = ask_fence({"prompt": "p" * 900, "mode": "one",
                         "options": ["l" * 400]})
        _, ask = fa.parse_room_ask(raw)
        self.assertEqual(fa.ASK_PROMPT_MAX, len(ask["prompt"]))
        self.assertEqual(fa.ASK_LABEL_MAX, len(ask["options"][0]["label"]))

    def test_oversized_block_is_left_as_text(self):
        raw = "here\n" + ask_fence({"prompt": "Pick", "mode": "one",
                                    "options": ["x" * 30_000]})
        body, ask = fa.parse_room_ask(raw)
        self.assertEqual(raw, body)
        self.assertIsNone(ask)

    def test_a_json_array_is_not_an_ask(self):
        body, ask = fa.parse_room_ask("```ask\n{}\n```")
        self.assertIsNone(ask)
        self.assertEqual("```ask\n{}\n```", body)

    def test_reply_that_is_only_a_fence_still_carries_the_question(self):
        raw = ask_fence({"prompt": "Ship or PR?", "mode": "one",
                         "options": ["Ship", "PR"]})
        body, ask = fa.parse_room_ask(raw)
        self.assertEqual("", body)
        self.assertIn("Ship or PR?", fa.ask_fallback(body, ask))


class AskFallbackTest(unittest.TestCase):
    """The widget and older mobile builds render no buttons at all,
    so the question has to survive as something typeable."""

    ASK = {"prompt": "Ship this to master or open a PR?", "mode": "one",
           "options": [{"label": "Ship to master", "value": "ship"},
                       {"label": "Open a PR", "value": "pr"}]}

    def test_prose_that_says_none_of_it_gets_the_whole_question(self):
        out = fa.ask_fallback("Both are fine.", self.ASK)
        self.assertIn("Both are fine.", out)
        self.assertIn("Ship this to master or open a PR?", out)
        self.assertIn("1. Ship to master", out)
        self.assertIn("2. Open a PR", out)
        self.assertIn(fa.ASK_FALLBACK_HINT, out)

    def test_prose_that_already_says_all_of_it_is_left_alone(self):
        prose = ("Ship this to master or open a PR? Ship to master is fine "
                 "for three files; Open a PR if you want eyes on it.")
        self.assertEqual(prose, fa.ask_fallback(prose, self.ASK))

    def test_prose_that_asks_but_never_names_the_options_gets_the_list(self):
        prose = "Ship this to master or open a PR?"
        out = fa.ask_fallback(prose, self.ASK)
        self.assertEqual(1, out.count("Ship this to master or open a PR?"))
        self.assertIn("1. Ship to master", out)

    def test_whitespace_and_case_do_not_defeat_the_check(self):
        prose = "ship this to MASTER\n   or open a pr?  Ship to master. Open a PR."
        self.assertEqual(prose.strip(), fa.ask_fallback(prose, self.ASK))

    def test_text_mode_appends_the_prompt_with_no_numbered_list(self):
        ask = {"prompt": "What should the column be called?", "mode": "text",
               "options": []}
        out = fa.ask_fallback("Need a name for it.", ask)
        self.assertIn("What should the column be called?", out)
        self.assertNotIn("1.", out)
        self.assertNotIn(fa.ASK_FALLBACK_HINT, out)


def frames_of(link, action):
    out = []
    for frame in link.outbox:
        data = json.loads(json.loads(frame)["data"])
        if data.get("action") == action:
            out.append(data)
    return out


async def wait_for_replies(link, count):
    for _ in range(5000):
        if len(frames_of(link, "room_reply")) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {count} room_reply frames, "
                         f"saw {len(frames_of(link, 'room_reply'))}")


async def ack_each_reply(link, room_id, hashids):
    """Answers each `room_reply` the way the server does — one
    `room_reply.posted` per posted chunk, in order."""
    for i, hashid in enumerate(hashids, 1):
        await wait_for_replies(link, i)
        fa.note_posted_message(room_id, hashid)


class PostRoomReplyTest(unittest.IsolatedAsyncioTestCase):
    """An ask hangs off a message hashid, and only the server knows
    it. A split reply produces several — the ask belongs to the last,
    or the buttons render in the middle of the answer."""

    ASK = {"prompt": "Ship or PR?", "mode": "one",
           "options": [{"label": "Ship", "value": "ship"},
                       {"label": "PR", "value": "pr"}]}

    async def asyncSetUp(self):
        self._timeout = fa.ROOM_REPLY_ACK_TIMEOUT_SECONDS
        fa.ROOM_REPLY_ACK_TIMEOUT_SECONDS = 0.2
        fa._posted_message_acks.clear()

    async def asyncTearDown(self):
        fa.ROOM_REPLY_ACK_TIMEOUT_SECONDS = self._timeout
        fa._posted_message_acks.clear()

    async def test_the_last_chunk_is_the_one_returned(self):
        link = fa.CableLink()
        feeder = asyncio.create_task(
            ack_each_reply(link, "rm123456", ["ms000001", "ms000002", "ms000003"]))
        posted = await fa.post_room_reply(link, "rm123456", ["one", "two", "three"],
                                          "ms123456")
        await feeder
        self.assertEqual("ms000003", posted)
        self.assertEqual(["one", "two", "three"],
                         [f["body"] for f in frames_of(link, "room_reply")])

    async def test_the_ask_lands_on_the_final_chunk(self):
        link = fa.CableLink()
        feeder = asyncio.create_task(
            ack_each_reply(link, "rm123456", ["ms000001", "ms000002"]))
        posted = await fa.post_room_reply(link, "rm123456", ["first half", "second half"])
        await feeder
        await fa.room_ask(link, "rm123456", posted, self.ASK)

        asks = frames_of(link, "room_ask")
        self.assertEqual(1, len(asks))
        self.assertEqual("ms000002", asks[0]["message_id"],
                         "an ask on an earlier chunk renders mid-reply")
        self.assertEqual("one", asks[0]["mode"])
        self.assertEqual([{"label": "Ship", "value": "ship"},
                          {"label": "PR", "value": "pr"}], asks[0]["options"])
        self.assertEqual("Ship or PR?", asks[0]["prompt"])

    async def test_a_server_that_never_acks_still_gets_the_reply(self):
        link = fa.CableLink()
        started = time.monotonic()
        posted = await fa.post_room_reply(link, "rm123456", ["a", "b", "c"])
        elapsed = time.monotonic() - started

        self.assertIsNone(posted)
        self.assertEqual(["a", "b", "c"], [f["body"] for f in frames_of(link, "room_reply")],
                         "the reply must survive a server that can't name it")
        self.assertLess(elapsed, fa.ROOM_REPLY_ACK_TIMEOUT_SECONDS * 3,
                        "one timeout for the turn, not one per chunk")

    async def test_no_hashid_means_no_ask_frame(self):
        link = fa.CableLink()
        await fa.room_ask(link, "rm123456", None, self.ASK)
        self.assertEqual([], frames_of(link, "room_ask"))

    async def test_an_ack_that_stops_arriving_midway_drops_the_ask(self):
        link = fa.CableLink()
        feeder = asyncio.create_task(ack_each_reply(link, "rm123456", ["ms000001"]))
        posted = await fa.post_room_reply(link, "rm123456", ["one", "two"])
        await feeder
        self.assertIsNone(posted, "hanging the ask on chunk one would render it mid-reply")

    async def test_a_duplicate_ack_is_not_mistaken_for_the_next_chunks(self):
        link = fa.CableLink()

        async def feeder():
            await wait_for_replies(link, 1)
            fa.note_posted_message("rm123456", "ms000001")
            fa.note_posted_message("rm123456", "ms_DUPLICATE")
            await wait_for_replies(link, 2)
            fa.note_posted_message("rm123456", "ms000002")

        task = asyncio.create_task(feeder())
        posted = await fa.post_room_reply(link, "rm123456", ["one", "two"])
        await task
        self.assertEqual("ms000002", posted,
                         "each chunk waits for its OWN acknowledgement")

    async def test_an_ack_for_a_room_nobody_is_waiting_on_is_dropped(self):
        fa.note_posted_message("rm999999", "ms000001")
        self.assertEqual({}, fa._posted_message_acks)


class HandleRoomMessageAskTest(unittest.IsolatedAsyncioTestCase):
    """The whole path: Claude emits a fence, the room gets prose, and
    the question hangs off the message the prose landed in."""

    async def asyncSetUp(self):
        self._run = fa.run_claude_streamed
        self._timeout = fa.ROOM_REPLY_ACK_TIMEOUT_SECONDS
        fa.ROOM_REPLY_ACK_TIMEOUT_SECONDS = 0.2
        fa._posted_message_acks.clear()

    async def asyncTearDown(self):
        fa.run_claude_streamed = self._run
        fa.ROOM_REPLY_ACK_TIMEOUT_SECONDS = self._timeout
        fa._posted_message_acks.clear()

    def answer_with_ask(self, prose):
        return prose + "\n\n```ask\n" + json.dumps(
            {"prompt": "Ship this to master or open a PR?", "mode": "one",
             "options": ["Ship to master", "Open a PR"]}) + "\n```"

    async def test_the_fence_becomes_prose_and_an_ask_on_the_reply(self):
        link = fa.CableLink()
        fa.run_claude_streamed = lambda *a, **k: self.answer_with_ask("Three files, no migration.")

        feeder = asyncio.create_task(ack_each_reply(link, "rm123456", ["ms000009"]))
        await fa.handle_room_message(link, ROOM_PAYLOAD)
        await feeder

        replies = frames_of(link, "room_reply")
        self.assertEqual(1, len(replies))
        self.assertNotIn("```", replies[0]["body"])
        self.assertIn("Three files, no migration.", replies[0]["body"])
        self.assertIn("Ship this to master or open a PR?", replies[0]["body"])
        self.assertIn("1. Ship to master", replies[0]["body"])
        self.assertEqual("ms123456", replies[0]["reply_to"])

        asks = frames_of(link, "room_ask")
        self.assertEqual(1, len(asks))
        self.assertEqual("ms000009", asks[0]["message_id"])
        self.assertEqual("rm123456", asks[0]["room_id"])

    async def test_a_server_that_never_acks_keeps_the_reply(self):
        link = fa.CableLink()
        fa.run_claude_streamed = lambda *a, **k: self.answer_with_ask("Three files.")

        await fa.handle_room_message(link, ROOM_PAYLOAD)

        replies = frames_of(link, "room_reply")
        self.assertEqual(1, len(replies))
        self.assertIn("Ship this to master or open a PR?", replies[0]["body"],
                      "the prose fallback is the whole degraded path")
        self.assertEqual([], frames_of(link, "room_ask"))

    async def test_an_answer_with_no_fence_sends_no_ask(self):
        link = fa.CableLink()
        fa.run_claude_streamed = lambda *a, **k: "We're on 2.86.1."

        feeder = asyncio.create_task(ack_each_reply(link, "rm123456", ["ms000009"]))
        await fa.handle_room_message(link, ROOM_PAYLOAD)
        await feeder

        self.assertEqual(["We're on 2.86.1."],
                         [f["body"] for f in frames_of(link, "room_reply")])
        self.assertEqual([], frames_of(link, "room_ask"))


class RoomReplyPostedFrameTest(unittest.IsolatedAsyncioTestCase):
    """The frame arrives on the socket reader while the worker is
    mid-turn, so the stream loop has to route it."""

    async def asyncSetUp(self):
        self._saved_queue = fa._work_queue
        fa._work_queue = asyncio.Queue()
        fa._posted_message_acks.clear()

    async def asyncTearDown(self):
        fa._work_queue = self._saved_queue
        fa._posted_message_acks.clear()

    async def test_the_stream_loop_records_the_hashid(self):
        queue = asyncio.Queue()
        fa._posted_message_acks["rm123456"] = queue

        class OneFrameSocket(FakeSocket):
            def __aiter__(self):
                async def gen():
                    yield json.dumps({"identifier": fa.CHANNEL_IDENTIFIER,
                                      "message": {"type": "room_reply.posted",
                                                  "room_id": "rm123456",
                                                  "message_id": "ms000042"}})
                return gen()

        await fa.process_stream(fa.CableLink(), OneFrameSocket())
        self.assertEqual("ms000042", queue.get_nowait())
        self.assertTrue(fa._work_queue.empty(),
                        "an acknowledgement is not a unit of work")


ROOM_PAYLOAD = {
    "type": "room.message",
    "room": {"hashid": "rm123456", "name": "Claude",
             "topic": "Claude Code sessions", "dispatch_mode": "all"},
    "message": {"hashid": "ms123456", "body": "what version are we on?"},
    "sender": {"hashid": "us123456", "name": "Sarah", "source": "app"},
    "history": [
        {"hashid": "ms000001", "body": "deploying now", "sender": "Sarah"},
        {"hashid": "ms000002", "body": "🚀 v2.31.0 deployed", "sender": "deploy"},
    ],
}


class BuildRoomPromptTest(unittest.TestCase):
    def test_renders_room_history_and_current_turn(self):
        prompt = fa.build_room_prompt(ROOM_PAYLOAD)
        self.assertIn("## Room: #Claude", prompt)
        self.assertIn("Claude Code sessions", prompt)
        self.assertIn("## Recent conversation", prompt)
        self.assertIn("Sarah: deploying now", prompt)
        self.assertIn("deploy: 🚀 v2.31.0 deployed", prompt)
        self.assertIn("## The message to answer", prompt)
        self.assertIn("Sarah: what version are we on?", prompt)

    def test_no_history_omits_the_section(self):
        payload = {**ROOM_PAYLOAD, "history": []}
        prompt = fa.build_room_prompt(payload)
        self.assertNotIn("## Recent conversation", prompt)
        self.assertIn("## The message to answer", prompt)

    def test_sparse_payload_does_not_raise(self):
        prompt = fa.build_room_prompt({"room": {}, "message": {}, "sender": {}})
        self.assertIn("## Room: #?", prompt)
        self.assertIn("someone:", prompt)

    def test_chat_instructions_forbid_a_proposal_block(self):
        prompt = fa.build_room_prompt(ROOM_PAYLOAD)
        self.assertIn("no fenced proposal block", prompt)


class AttachmentTest(unittest.TestCase):
    """Screenshots reach the model as files on disk, or not at all —
    a half-downloaded picture must never cost the room its answer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._real_dir = fa.ATTACHMENT_DIR
        fa.ATTACHMENT_DIR = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(fa, "ATTACHMENT_DIR", self._real_dir))

    def _serve(self, body: bytes):
        """Stub urlopen with a context manager yielding `body`."""
        class _Resp:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner, n=None): return body
        real = fa.urllib.request.urlopen
        fa.urllib.request.urlopen = lambda url, timeout=None: _Resp()
        self.addCleanup(lambda: setattr(fa.urllib.request, "urlopen", real))

    def test_downloads_to_disk_and_reports_the_path(self):
        self._serve(b"\x89PNG fake bytes")
        got = fa.download_attachments({
            "hashid": "msg00001",
            "attachments": [{
                "filename": "shot.png", "kind": "image",
                "content_type": "image/png", "byte_size": 15,
                "url": "https://cdn.example/u/shot.png",
            }],
        })
        self.assertEqual(1, len(got))
        path = Path(got[0]["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(b"\x89PNG fake bytes", path.read_bytes())

    def test_a_failed_download_is_skipped_not_raised(self):
        def boom(url, timeout=None):
            raise OSError("connection reset")
        real = fa.urllib.request.urlopen
        fa.urllib.request.urlopen = boom
        self.addCleanup(lambda: setattr(fa.urllib.request, "urlopen", real))

        got = fa.download_attachments({
            "hashid": "msg2",
            "attachments": [{"filename": "a.png", "url": "https://cdn.example/a.png"}],
        })
        self.assertEqual([], got)

    def test_a_relative_url_is_resolved_against_the_cable_host(self):
        seen = []
        real = fa.urllib.request.urlopen

        class _Resp:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner, n=None): return b"x"

        def capture(url, timeout=None):
            seen.append(url)
            return _Resp()

        fa.urllib.request.urlopen = capture
        self.addCleanup(lambda: setattr(fa.urllib.request, "urlopen", real))

        fa.download_attachments({
            "hashid": "msg3",
            "attachments": [{"filename": "d.png", "url": "/u-test/d.png"}],
        })
        self.assertEqual(1, len(seen))
        self.assertTrue(seen[0].startswith("http"), seen)
        self.assertTrue(seen[0].endswith("/u-test/d.png"), seen)

    def test_a_filename_cannot_climb_out_of_the_directory(self):
        self._serve(b"x")
        got = fa.download_attachments({
            "hashid": "msg4",
            "attachments": [{"filename": "../../../etc/cron.d/pwn",
                             "url": "https://cdn.example/x"}],
        })
        self.assertEqual(1, len(got))
        written = Path(got[0]["path"]).resolve()
        self.assertTrue(
            str(written).startswith(str(Path(self.tmp.name).resolve())),
            f"{written} escaped the attachment directory")

    def test_an_oversized_attachment_is_skipped(self):
        self._serve(b"x")
        got = fa.download_attachments({
            "hashid": "msg5",
            "attachments": [{"filename": "huge.bin", "byte_size": 10**9,
                             "url": "https://cdn.example/huge.bin"}],
        })
        self.assertEqual([], got)

    def test_the_prompt_lists_downloaded_paths(self):
        prompt = fa.build_room_prompt(ROOM_PAYLOAD, [
            {"path": "/tmp/shot.png", "filename": "shot.png",
             "kind": "image", "bytes": 1234},
        ])
        self.assertIn("## Attachments on that message", prompt)
        self.assertIn("/tmp/shot.png", prompt)
        self.assertIn("Read tool", prompt)

    def test_no_attachments_omits_the_section(self):
        self.assertNotIn("## Attachments", fa.build_room_prompt(ROOM_PAYLOAD))


class RoomProgressLineTest(unittest.TestCase):
    """One glanceable line per tool call: the tool plus the argument
    that says what it touched, never the whole input dict."""

    def test_prefers_the_argument_that_names_the_target(self):
        self.assertEqual("Read(config/application.rb)",
                         fa._progress_line("Read", {"file_path": "config/application.rb"}))
        self.assertEqual("Bash(git log -1)",
                         fa._progress_line("Bash", {"command": "git log -1",
                                                    "description": "recent commit"}))

    def test_falls_back_to_the_bare_tool_name(self):
        self.assertEqual("Read", fa._progress_line("Read", None))
        self.assertEqual("Read", fa._progress_line("Read", {"unknown_key": "x"}))
        self.assertEqual("Read", fa._progress_line("Read", {"file_path": "   "}))

    def test_takes_one_line_and_caps_its_length(self):
        line = fa._progress_line("Bash", {"command": "echo one\necho two"})
        self.assertEqual("Bash(echo one)", line)

        long_line = fa._progress_line("Bash", {"command": "x" * 400})
        self.assertLessEqual(len(long_line), 140)
        self.assertTrue(long_line.startswith("Bash(x"))


class RoomCommandTest(unittest.TestCase):
    def test_recognizes_reset_aliases(self):
        for word in ("/reset", "/clear", "/new"):
            self.assertEqual(word, fa.room_command(word))
            self.assertEqual(word, fa.room_command(f"  {word.upper()}  "))

    def test_trailing_words_still_reset(self):
        self.assertEqual("/reset", fa.room_command("/reset please, you're confused"))

    def test_ordinary_messages_are_not_commands(self):
        for body in ("", "   ", "hello", "what about /reset?", "resetting the db"):
            self.assertIsNone(fa.room_command(body))


class SplitRoomBodyTest(unittest.TestCase):
    def test_short_body_is_one_chunk(self):
        self.assertEqual(["hello"], fa.split_room_body("hello"))

    def test_empty_body_is_no_chunks(self):
        self.assertEqual([], fa.split_room_body("   "))

    def test_long_body_splits_on_paragraph_and_stays_under_the_cap(self):
        para = ("x" * 200 + "\n\n") * 60
        chunks = fa.split_room_body(para, limit=1000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1000)
        self.assertEqual(para.replace("\n\n", "").strip(), "".join(chunks).replace("\n\n", ""))

    def test_unbreakable_text_still_splits(self):
        chunks = fa.split_room_body("y" * 2500, limit=1000)
        self.assertEqual(3, len(chunks))
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1000)


class RoomSessionKeyTest(unittest.TestCase):
    def test_rooms_get_distinct_session_keys(self):
        self.assertNotEqual(fa._room_session_key("aaa"), fa._room_session_key("bbb"))

    def test_session_key_is_scoped_to_the_project(self):
        self.assertIn(fa.PROJECT, fa._room_session_key("aaa"))


class ClearSessionsTest(unittest.TestCase):
    """SID_DIR is redirected at a tmpdir — a test that reaches the
    real ~/.cache/claude-chat would wipe a live session."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._real_dir = fa.SID_DIR
        fa.SID_DIR = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(fa, "SID_DIR", self._real_dir))

    def write(self, name):
        (fa.SID_DIR / name).write_text("00000000-0000-0000-0000-000000000000")

    def test_clears_feedback_and_every_room_for_this_project(self):
        self.write(f"feedback_stream_{fa.PROJECT}")
        self.write(f"feedback_{fa.PROJECT}")
        self.write(f"feedback_stream_room_aaa_{fa.PROJECT}")
        self.write(f"feedback_stream_room_bbb_{fa.PROJECT}")
        removed = fa.clear_sessions()
        self.assertEqual(4, len(removed))
        self.assertEqual([], list(fa.SID_DIR.iterdir()))

    def test_leaves_other_projects_alone(self):
        self.write("feedback_stream_some_other_project")
        self.write(f"feedback_stream_{fa.PROJECT}")
        fa.clear_sessions()
        self.assertEqual(["feedback_stream_some_other_project"],
                         [p.name for p in fa.SID_DIR.iterdir()])

    def test_named_rooms_clear_only_themselves(self):
        self.write(f"feedback_stream_room_aaa_{fa.PROJECT}")
        self.write(f"feedback_stream_room_bbb_{fa.PROJECT}")
        self.write(f"feedback_stream_{fa.PROJECT}")
        removed = fa.clear_sessions([fa._room_session_key("aaa")])
        self.assertEqual([f"feedback_stream_room_aaa_{fa.PROJECT}"], removed)
        self.assertEqual(2, len(list(fa.SID_DIR.iterdir())))

    def test_clearing_nothing_is_not_an_error(self):
        self.assertEqual([], fa.clear_sessions())


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=cwd, check=True,
                   capture_output=True, text=True)


class GitRepoTestCase(unittest.TestCase):
    """A real throwaway git repo — the worktree helpers are entirely
    about git behavior, so stubbing git would test nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "vroxy_web"
        self.repo.mkdir()
        _git(["init", "-q", "-b", "main"], self.repo)
        _git(["config", "user.email", "t@example.test"], self.repo)
        _git(["config", "user.name", "Test"], self.repo)
        (self.repo / "README.md").write_text("hello\n")
        (self.repo / "CLAUDE.md").write_text("project rules\n")
        _git(["add", "."], self.repo)
        _git(["commit", "-qm", "init"], self.repo)


class ProposalWorktreeTest(GitRepoTestCase):
    def test_worktree_is_a_sibling_so_parent_claude_md_still_loads(self):
        with fa.proposal_worktree(self.repo) as wt:
            self.assertEqual(self.repo.parent, wt.parent,
                             "worktree must sit beside the project, not in /tmp — "
                             "the workspace CLAUDE.md and sibling repos hang off that parent")
            self.assertTrue((wt / "CLAUDE.md").is_file())

    def test_edits_in_the_worktree_never_touch_the_real_checkout(self):
        with fa.proposal_worktree(self.repo) as wt:
            (wt / "README.md").write_text("EDITED BY CLAUDE\n")
            (wt / "new_file.rb").write_text("fresh\n")
        self.assertEqual("hello\n", (self.repo / "README.md").read_text())
        self.assertFalse((self.repo / "new_file.rb").exists())

    def test_worktree_is_removed_afterwards(self):
        with fa.proposal_worktree(self.repo) as wt:
            path = wt
            self.assertTrue(path.is_dir())
        self.assertFalse(path.exists())

    def test_worktree_is_removed_even_when_the_run_raises(self):
        path = None
        with self.assertRaises(RuntimeError):
            with fa.proposal_worktree(self.repo) as wt:
                path = wt
                raise RuntimeError("claude blew up")
        self.assertFalse(path.exists())

    def test_two_runs_do_not_collide(self):
        with fa.proposal_worktree(self.repo) as a:
            with fa.proposal_worktree(self.repo) as b:
                self.assertNotEqual(a, b)


class WorktreeDiffstatTest(GitRepoTestCase):
    def test_no_changes_is_all_zeroes(self):
        with fa.proposal_worktree(self.repo) as wt:
            self.assertEqual({"files": 0, "insertions": 0, "deletions": 0},
                             fa.worktree_diffstat(wt))

    def test_counts_modified_lines(self):
        with fa.proposal_worktree(self.repo) as wt:
            (wt / "README.md").write_text("one\ntwo\nthree\n")
            stats = fa.worktree_diffstat(wt)
        self.assertEqual(1, stats["files"])
        self.assertEqual(3, stats["insertions"])
        self.assertEqual(1, stats["deletions"])

    def test_binary_files_do_not_crash_the_count(self):
        with fa.proposal_worktree(self.repo) as wt:
            (wt / "logo.png").write_bytes(b"\x89PNG\x00\x01\x02")
            _git(["add", "logo.png"], wt)
            stats = fa.worktree_diffstat(wt)
        self.assertGreaterEqual(stats["files"], 1)


class WorktreeChangedFilesTest(GitRepoTestCase):
    def test_collects_modified_and_new_files(self):
        with fa.proposal_worktree(self.repo) as wt:
            (wt / "README.md").write_text("changed\n")
            (wt / "added.rb").write_text("puts 1\n")
            files = fa.worktree_changed_files(wt)
        by_path = {f["path"]: f["content"] for f in files}
        self.assertEqual("changed\n", by_path["README.md"])
        self.assertEqual("puts 1\n", by_path["added.rb"])

    def test_an_untouched_worktree_yields_nothing(self):
        with fa.proposal_worktree(self.repo) as wt:
            self.assertEqual([], fa.worktree_changed_files(wt))

    def test_a_binary_file_is_skipped_rather_than_crashing(self):
        with fa.proposal_worktree(self.repo) as wt:
            (wt / "ok.rb").write_text("fine\n")
            (wt / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
            paths = [f["path"] for f in fa.worktree_changed_files(wt)]
        self.assertIn("ok.rb", paths)
        self.assertNotIn("blob.bin", paths)


class SafeTargetTest(GitRepoTestCase):
    """A proposal's `path` is LLM-authored and reaches the apply step
    from a note a widget visitor wrote.  `project_dir / path` is not a
    containment check, so these are the escapes it has to refuse."""

    def test_an_ordinary_relative_path_resolves_inside(self):
        t = fa.safe_target(self.repo, "app/views/foo.erb")
        self.assertEqual(self.repo.resolve() / "app/views/foo.erb", t)

    def test_an_absolute_path_is_refused(self):
        # Path("/a/b") / "/etc/x" is "/etc/x" — the base is discarded.
        for probe in ("/etc/cron.d/pwn", "/home/ubuntu/.ssh/authorized_keys",
                      "/home/ubuntu/.claude/settings.json"):
            with self.assertRaises(ValueError, msg=probe) as cm:
                fa.safe_target(self.repo, probe)
            self.assertIn("absolute", str(cm.exception))

    def test_traversal_out_of_the_project_is_refused(self):
        for probe in ("../escaped.txt", "../../.claude/settings.json",
                      "app/../../outside.rb", "a/b/../../../../etc/passwd"):
            with self.assertRaises(ValueError, msg=probe):
                fa.safe_target(self.repo, probe)

    def test_writing_into_dot_git_is_refused(self):
        # Inside the project, but .git/hooks/pre-commit runs on the
        # very commit the apply step is about to make.
        for probe in (".git/hooks/pre-commit", ".git/config",
                      ".git/hooks/post-checkout"):
            with self.assertRaises(ValueError, msg=probe) as cm:
                fa.safe_target(self.repo, probe)
            self.assertIn(".git", str(cm.exception))

    def test_the_project_root_itself_is_refused(self):
        for probe in (".", "", "./"):
            with self.assertRaises(ValueError):
                fa.safe_target(self.repo, probe)

    def test_a_path_that_merely_looks_scary_but_stays_inside_is_allowed(self):
        # Containment is decided by where it RESOLVES, not by spelling.
        t = fa.safe_target(self.repo, "app/../lib/ok.rb")
        self.assertEqual(self.repo.resolve() / "lib/ok.rb", t)

    def test_a_sibling_prefix_is_not_treated_as_inside(self):
        # /repo-evil must not pass because it starts with /repo.
        sibling = self.repo.parent / (self.repo.name + "-evil")
        sibling.mkdir()
        with self.assertRaises(ValueError):
            fa.safe_target(self.repo, f"../{sibling.name}/x.rb")


class HeadShaTest(GitRepoTestCase):
    def test_reports_the_current_commit(self):
        sha = fa.head_sha(self.repo)
        self.assertEqual(40, len(sha))
        self.assertRegex(sha, r"\A[0-9a-f]{40}\Z")

    def test_changes_when_a_commit_lands(self):
        before = fa.head_sha(self.repo)
        (self.repo / "NEW.md").write_text("x\n")
        _git(["add", "."], self.repo)
        _git(["commit", "-qm", "second"], self.repo)
        self.assertNotEqual(before, fa.head_sha(self.repo))

    def test_a_non_repo_yields_empty_rather_than_raising(self):
        self.assertEqual("", fa.head_sha(self.root))

    def test_a_proposal_worktree_shares_the_base_commit(self):
        # The proposal is generated in the worktree, so the sha it
        # stamps must match the project it will be applied to.
        with fa.proposal_worktree(self.repo) as wt:
            self.assertEqual(fa.head_sha(self.repo), fa.head_sha(wt))


class GitBranchHelpersTest(GitRepoTestCase):
    def test_current_branch(self):
        self.assertEqual("main", fa._current_branch(self.repo))

    def test_ensure_on_base_is_a_noop_when_already_there(self):
        ok, _ = fa._ensure_on_base(self.repo, "main")
        self.assertTrue(ok)

    def test_ensure_on_base_switches_branches(self):
        _git(["checkout", "-q", "-b", "feature"], self.repo)
        ok, _ = fa._ensure_on_base(self.repo, "main")
        self.assertTrue(ok)
        self.assertEqual("main", fa._current_branch(self.repo))

    def test_ensure_on_base_refuses_to_discard_uncommitted_work(self):
        _git(["checkout", "-q", "-b", "feature"], self.repo)
        (self.repo / "README.md").write_text("work in progress\n")
        ok, err = fa._ensure_on_base(self.repo, "main")
        self.assertFalse(ok)
        self.assertIn("uncommitted", err)
        self.assertEqual("feature", fa._current_branch(self.repo))
        self.assertEqual("work in progress\n", (self.repo / "README.md").read_text())


class BuildPromptPolicyTest(unittest.TestCase):
    def test_policy_block_tells_the_model_how_the_workspace_ships(self):
        prompt = fa.build_prompt({
            "feedback": {"note": "tweak it"}, "chat": {"hashid": "c"},
            "policy": {"policy": "auto", "base_ref": "master", "auto_apply": False},
        })
        self.assertIn("Ship policy", prompt)
        self.assertIn("`master`", prompt)
        self.assertIn("small changes commit", prompt)

    def test_auto_apply_is_called_out_because_it_removes_the_human(self):
        prompt = fa.build_prompt({
            "feedback": {"note": "tweak it"}, "chat": {"hashid": "c"},
            "policy": {"policy": "auto", "base_ref": "main", "auto_apply": True},
        })
        self.assertIn("Auto-apply is ON", prompt)

    def test_no_policy_block_when_the_server_sent_none(self):
        prompt = fa.build_prompt({"feedback": {"note": "x"}, "chat": {"hashid": "c"}})
        self.assertNotIn("Ship policy", prompt)

    def test_the_model_is_told_it_does_not_choose_the_mode(self):
        self.assertIn("You do NOT decide how this ships", fa.SYSTEM_PROMPT)


class FakeSocket:
    """Records frames; can be made to fail like a dropped cable."""

    def __init__(self, broken=False):
        self.sent = []
        self.broken = broken

    async def send(self, frame):
        if self.broken:
            raise ConnectionError("socket is gone")
        self.sent.append(frame)


class CableLinkTest(unittest.IsolatedAsyncioTestCase):
    """A deploy drops the cable mid-run.  The answer has already cost
    minutes and tokens by then, so it has to survive the gap."""

    async def test_sends_straight_through_when_connected(self):
        link, ws = fa.CableLink(), FakeSocket()
        link.attach(ws)
        await link.send("hello")
        self.assertEqual(["hello"], ws.sent)
        self.assertEqual([], link.outbox)

    async def test_buffers_while_disconnected_then_flushes(self):
        link = fa.CableLink()
        await link.send("one")
        await link.send("two")
        self.assertEqual(["one", "two"], link.outbox)

        ws = FakeSocket()
        link.attach(ws)
        await link.flush()
        self.assertEqual(["one", "two"], ws.sent, "order must be preserved")
        self.assertEqual([], link.outbox)

    async def test_a_send_onto_a_dead_socket_is_buffered_not_lost(self):
        # The exact shape of the bug: the run finished, the socket had
        # already died, and the reply went nowhere.
        link = fa.CableLink()
        link.attach(FakeSocket(broken=True))
        await link.send("the answer")
        self.assertEqual(["the answer"], link.outbox)
        self.assertIsNone(link.ws, "a failed send must drop the dead socket")

        good = FakeSocket()
        link.attach(good)
        await link.flush()
        self.assertEqual(["the answer"], good.sent)

    async def test_unbufferable_frames_are_dropped_not_replayed(self):
        # A heartbeat replayed after reconnect reports a stale status.
        link = fa.CableLink()
        await link.send("heartbeat", buffer=False)
        self.assertEqual([], link.outbox)

    async def test_the_outbox_is_bounded(self):
        link = fa.CableLink()
        for i in range(fa.CableLink.MAX_OUTBOX + 25):
            await link.send(f"frame-{i}")
        self.assertEqual(fa.CableLink.MAX_OUTBOX, len(link.outbox))
        # Oldest dropped first — a fresh reply outranks a stale chip.
        self.assertEqual(f"frame-{fa.CableLink.MAX_OUTBOX + 24}", link.outbox[-1])
        self.assertNotIn("frame-0", link.outbox)

    async def test_flush_re_buffers_when_the_new_socket_also_fails(self):
        link = fa.CableLink()
        await link.send("keep me")
        link.attach(FakeSocket(broken=True))
        await link.flush()
        self.assertEqual(["keep me"], link.outbox, "must not be dropped on a failed flush")

    async def test_detach_keeps_the_outbox(self):
        link = fa.CableLink()
        link.attach(FakeSocket())
        link.detach()
        await link.send("after detach")
        self.assertEqual(["after detach"], link.outbox)


class WorkerSurvivesReconnectTest(unittest.IsolatedAsyncioTestCase):
    """The worker used to be created inside process_stream and
    cancelled in its `finally`, so a reconnect destroyed the run in
    flight.  The Claude thread kept going, finished, and had nothing
    left to reply through."""

    async def asyncSetUp(self):
        self._saved_queue = fa._work_queue
        fa._work_queue = asyncio.Queue()

    async def asyncTearDown(self):
        fa._work_queue = self._saved_queue

    async def test_a_queued_item_still_runs_after_the_socket_drops(self):
        link = fa.CableLink()
        started = asyncio.Event()
        finished = asyncio.Event()

        async def slow_handler(_link, _payload):
            started.set()
            await asyncio.sleep(0.2)          # the Claude run
            await _link.send("the answer")    # replies afterwards
            finished.set()

        original = fa.handle_room_message
        fa.handle_room_message = slow_handler
        worker = asyncio.create_task(fa.worker_loop(link))
        try:
            link.attach(FakeSocket())
            await fa._work_queue.put(("room", {"room": {"hashid": "r1"}}))
            await asyncio.wait_for(started.wait(), 2)

            # The deploy lands mid-run.
            link.detach()

            await asyncio.wait_for(finished.wait(), 2)
            self.assertFalse(worker.done(), "the worker must outlive the connection")
            self.assertEqual(["the answer"], link.outbox,
                             "the result must be held for the next connection")
        finally:
            worker.cancel()
            fa.handle_room_message = original

    async def test_a_handler_raising_does_not_kill_the_worker(self):
        link = fa.CableLink()
        boom = asyncio.Event()

        async def bad_handler(_link, _payload):
            boom.set()
            raise RuntimeError("handler blew up")

        original = fa.handle_room_message
        fa.handle_room_message = bad_handler
        worker = asyncio.create_task(fa.worker_loop(link))
        try:
            await fa._work_queue.put(("room", {"room": {}}))
            await asyncio.wait_for(boom.wait(), 2)
            await asyncio.sleep(0.05)
            self.assertFalse(worker.done(), "one bad payload must not deafen dispatch")
        finally:
            worker.cancel()
            fa.handle_room_message = original


class CableSendTest(unittest.IsolatedAsyncioTestCase):
    async def test_cable_send_routes_through_the_link(self):
        link = fa.CableLink()
        await fa.cable_send(link, "message", {"action": "reply", "body": "hi"})
        self.assertEqual(1, len(link.outbox))
        self.assertIn("reply", link.outbox[0])

    async def test_heartbeat_is_not_buffered(self):
        link = fa.CableLink()
        await fa.heartbeat(link)
        self.assertEqual([], link.outbox, "a stale heartbeat must not be replayed")


def sent_bodies(link) -> list[str]:
    """The room_reply bodies a link recorded, unwrapped from the
    ActionCable envelope."""
    bodies = []
    for frame in link.outbox:
        data = json.loads(json.loads(frame)["data"])
        if data.get("action") == "room_reply":
            bodies.append(data["body"])
    return bodies


class SelfUpdateSandbox:
    """Points the agent's idea of "my own source" at a scratch copy so
    a test can edit it without touching the real checkout.

    A plain mixin, not a TestCase: IsolatedAsyncioTestCase calls
    `setUp` itself, so a shared base class would run the sandbox
    twice and tear it down onto already-patched globals."""

    def start_sandbox(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = root / "feedback_agent.py"
        self.source.write_text('AGENT_VERSION = "vroxy_dispatch 9.9.9"\n', encoding="utf-8")
        self.helper = root / "claude-chat"
        self.helper.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

        self._saved = (fa._SELF_FILES, fa._BOOT_FINGERPRINT, fa.STATE_DIR,
                       fa.RESTART_NOTICE_PATH, fa._restart_pending,
                       fa._restart_scheduled)
        fa._SELF_FILES = (self.source, self.helper)
        fa._BOOT_FINGERPRINT = fa.source_fingerprint()
        fa.STATE_DIR = root / "state"
        fa.RESTART_NOTICE_PATH = fa.STATE_DIR / "restart-notice.json"
        fa._restart_pending = None
        fa._restart_scheduled = False

    def stop_sandbox(self):
        (fa._SELF_FILES, fa._BOOT_FINGERPRINT, fa.STATE_DIR,
         fa.RESTART_NOTICE_PATH, fa._restart_pending,
         fa._restart_scheduled) = self._saved
        self.tmp.cleanup()

    def edit_source(self, body="AGENT_VERSION = \"vroxy_dispatch 9.9.10\"\n"):
        self.source.write_text(body, encoding="utf-8")


class SelfUpdateStateTestCase(SelfUpdateSandbox, unittest.TestCase):
    def setUp(self):
        self.start_sandbox()

    def tearDown(self):
        self.stop_sandbox()


class SourceFingerprintTest(SelfUpdateStateTestCase):
    def test_an_untouched_checkout_is_not_an_update(self):
        self.assertFalse(fa.self_updated())

    def test_editing_the_agent_is_an_update(self):
        self.edit_source()
        self.assertTrue(fa.self_updated())

    def test_editing_the_helper_script_counts_too(self):
        self.helper.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
        self.assertTrue(fa.self_updated())

    def test_a_commit_with_no_version_bump_is_still_an_update(self):
        # Hashing the bytes, rather than reading AGENT_VERSION, is
        # what makes a bug fix shipped without a bump detectable.
        self.edit_source('AGENT_VERSION = "vroxy_dispatch 9.9.9"\n# fixed a bug\n')
        self.assertTrue(fa.self_updated())
        self.assertEqual("vroxy_dispatch 9.9.9", fa.disk_agent_version())

    def test_a_deleted_source_does_not_raise(self):
        self.source.unlink()
        self.assertTrue(fa.self_updated())
        self.assertEqual("", fa.disk_agent_version())


class DiskAgentVersionTest(SelfUpdateStateTestCase):
    def test_reads_the_version_that_is_on_disk_now(self):
        self.edit_source('AGENT_VERSION = "vroxy_dispatch 1.0.0"\n')
        self.assertEqual("vroxy_dispatch 1.0.0", fa.disk_agent_version())

    def test_a_file_without_the_constant_yields_empty(self):
        self.edit_source("print('hi')\n")
        self.assertEqual("", fa.disk_agent_version())


class SelfCompilesTest(SelfUpdateStateTestCase):
    def test_valid_python_compiles(self):
        ok, _ = fa.self_compiles()
        self.assertTrue(ok)

    def test_a_syntax_error_is_caught_before_it_becomes_a_crash_loop(self):
        self.edit_source("def broken(:\n")
        ok, detail = fa.self_compiles()
        self.assertFalse(ok)
        self.assertTrue(detail, "the operator needs to see what broke")


class RestartNoticeTest(SelfUpdateStateTestCase):
    def test_round_trips_what_the_next_process_needs(self):
        fa.write_restart_notice("room1234", "msg5678", "vroxy_dispatch 1.0.0")
        notice = fa.take_restart_notice()
        self.assertEqual("room1234", notice["room_id"])
        self.assertEqual("msg5678", notice["reply_to"])
        self.assertEqual(fa.AGENT_VERSION, notice["from_version"])

    def test_taking_a_notice_deletes_it(self):
        # Every reconnect confirms the subscription again; a notice
        # that survived one read would be announced on each of them.
        fa.write_restart_notice("room1234", None, "vroxy_dispatch 1.0.0")
        self.assertIsNotNone(fa.take_restart_notice())
        self.assertIsNone(fa.take_restart_notice())
        self.assertFalse(fa.RESTART_NOTICE_PATH.exists())

    def test_no_notice_is_not_an_error(self):
        self.assertIsNone(fa.take_restart_notice())

    def test_a_stale_notice_is_dropped(self):
        fa.write_restart_notice("room1234", None, "vroxy_dispatch 1.0.0")
        stale = json.loads(fa.RESTART_NOTICE_PATH.read_text())
        stale["at"] = stale["at"] - fa.RESTART_NOTICE_MAX_AGE_SECONDS - 60
        fa.RESTART_NOTICE_PATH.write_text(json.dumps(stale), encoding="utf-8")
        self.assertIsNone(fa.take_restart_notice())

    def test_a_corrupt_notice_is_dropped_not_raised(self):
        fa.STATE_DIR.mkdir(parents=True, exist_ok=True)
        fa.RESTART_NOTICE_PATH.write_text("{not json", encoding="utf-8")
        self.assertIsNone(fa.take_restart_notice())

    def test_a_notice_with_no_room_is_dropped(self):
        fa.write_restart_notice(None, None, "vroxy_dispatch 1.0.0")
        self.assertIsNone(fa.take_restart_notice())


class RestartIfSelfUpdatedTest(SelfUpdateSandbox, unittest.IsolatedAsyncioTestCase):
    """The whole point: a run that edits dispatch leaves the process
    answering from code that no longer exists."""

    ROOM = {"room": {"hashid": "room1234"}, "message": {"hashid": "msg5678"}}

    async def asyncSetUp(self):
        self.start_sandbox()
        self.scheduled = []
        self._saved_schedule = fa.schedule_restart
        fa.schedule_restart = lambda: (self.scheduled.append(1), (True, "unit-test"))[1]
        self._saved_queue = fa._work_queue
        fa._work_queue = asyncio.Queue()

    async def asyncTearDown(self):
        fa.schedule_restart = self._saved_schedule
        fa._work_queue = self._saved_queue
        self.stop_sandbox()

    async def test_an_unchanged_checkout_does_nothing(self):
        link = fa.CableLink()
        await fa.restart_if_self_updated(link, "room", self.ROOM)
        self.assertEqual([], self.scheduled)
        self.assertEqual([], sent_bodies(link))

    async def test_an_update_announces_then_restarts(self):
        self.edit_source('AGENT_VERSION = "vroxy_dispatch 9.9.10"\n')
        link = fa.CableLink()
        await fa.restart_if_self_updated(link, "room", self.ROOM)

        self.assertEqual([1], self.scheduled)
        bodies = sent_bodies(link)
        self.assertEqual(1, len(bodies))
        self.assertIn("9.9.10", bodies[0])
        notice = json.loads(fa.RESTART_NOTICE_PATH.read_text())
        self.assertEqual("room1234", notice["room_id"])
        self.assertEqual("msg5678", notice["reply_to"])

    async def test_a_queue_with_work_on_it_no_longer_blocks_the_restart(self):
        # It used to wait for an empty queue, which on a busy agent
        # meant never — a shipped fix could sit un-run for hours while
        # every check logged "holding the restart".  spool_pending_work
        # writes the queue to disk on shutdown and the next process
        # replays it, so this costs a delay, not a message.
        self.edit_source()
        await fa._work_queue.put(("room", {}))
        link = fa.CableLink()

        await fa.restart_if_self_updated(link, "room", self.ROOM)

        self.assertEqual([1], self.scheduled)

    async def test_the_queued_work_is_spooled_so_the_restart_cannot_lose_it(self):
        self.edit_source()
        await fa._work_queue.put(("room", {"message": {"hashid": "keepme"}}))

        spooled = fa.spool_pending_work()

        self.assertEqual(1, spooled)
        self.assertIn("keepme", fa.WORK_SPOOL_PATH.read_text())

    async def test_it_refuses_to_restart_into_a_build_that_will_not_compile(self):
        self.edit_source("def broken(:\n")
        link = fa.CableLink()
        await fa.restart_if_self_updated(link, "room", self.ROOM)

        self.assertEqual([], self.scheduled, "a syntax error restarts into a crash loop")
        self.assertFalse(fa.RESTART_NOTICE_PATH.exists())
        self.assertIn("doesn't compile", sent_bodies(link)[0])

    async def test_it_only_fires_once(self):
        self.edit_source()
        link = fa.CableLink()
        await fa.restart_if_self_updated(link, "room", self.ROOM)
        await fa.restart_if_self_updated(link, "room", self.ROOM)
        self.assertEqual([1], self.scheduled)

    async def test_a_failed_schedule_says_so_and_leaves_no_notice(self):
        # Reporting a restart that never happened is worse than
        # reporting the failure — the next boot would announce a
        # version change nobody made.
        self.edit_source()
        fa.schedule_restart = lambda: (False, "sudo: a password is required")
        link = fa.CableLink()
        await fa.restart_if_self_updated(link, "room", self.ROOM)

        self.assertFalse(fa.RESTART_NOTICE_PATH.exists())
        self.assertIn("couldn't restart", sent_bodies(link)[-1])
        self.assertIn("systemctl restart", sent_bodies(link)[-1])

    async def test_a_non_room_update_restarts_without_announcing(self):
        # An approved proposal can update dispatch too; there's no
        # room to speak in, and guessing one would be worse.
        self.edit_source()
        link = fa.CableLink()
        await fa.restart_if_self_updated(link, "approve", {"feedback_id": "abc"})
        self.assertEqual([1], self.scheduled)
        self.assertEqual([], sent_bodies(link))
        self.assertFalse(fa.RESTART_NOTICE_PATH.exists())


class ProgressTrailTest(unittest.TestCase):
    """Text between tool calls is the most readable part of a run —
    "Now the view marker, the copy-link action, and the JS" says more
    at a glance than Bash(python3 - <<PY …). It used to be dropped
    twice: never sent to the trail, and then overwritten in the reply
    by the CLI's own final answer."""

    def run_trail(self, events):
        got = []
        trail = fa.ProgressTrail(lambda kind, text: got.append((kind, text)))
        for event in events:
            trail.feed(event)
        return got

    def test_narration_reaches_the_trail_but_the_answer_does_not(self):
        got = self.run_trail([
            {"type": "text_delta", "text": "Building the window first."},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "a.rb"}},
            {"type": "text_delta", "text": "Now the JS."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "text_delta", "text": "Shipped — 2.84.0."},
            {"type": "result", "usage": {}},
        ])

        self.assertEqual(["text", "tool", "text", "tool"], [k for k, _ in got])
        texts = [t for _, t in got]
        self.assertIn("Building the window first.", texts)
        self.assertIn("Now the JS.", texts)
        self.assertNotIn("Shipped — 2.84.0.", texts,
                         "the last block IS the reply — saying it twice helps nobody")

    def test_consecutive_text_is_one_line_not_two(self):
        got = self.run_trail([
            {"type": "text_delta", "text": "First half."},
            {"type": "text_delta", "text": "Second half."},
            {"type": "tool_use", "name": "Read", "input": {}},
            {"type": "result"},
        ])
        self.assertEqual(("text", "First half. Second half."), got[0])

    def test_a_run_ending_on_a_tool_call_holds_nothing_back(self):
        got = self.run_trail([
            {"type": "text_delta", "text": "Checking."},
            {"type": "tool_use", "name": "Read", "input": {}},
            {"type": "result"},
        ])
        self.assertEqual(["text", "tool"], [k for k, _ in got])

    def test_thinking_flushes_the_narration_before_it(self):
        got = self.run_trail([
            {"type": "text_delta", "text": "About to reason."},
            {"type": "thinking", "text": "the route is workspace-scoped"},
            {"type": "result"},
        ])
        self.assertEqual(["text", "thinking"], [k for k, _ in got])

    def test_an_answer_with_no_tool_calls_produces_no_trail_at_all(self):
        # A one-line answer is just a reply. A trail saying the same
        # thing under it would be the message twice.
        got = self.run_trail([
            {"type": "text_delta", "text": "0.13.0."},
            {"type": "result"},
        ])
        self.assertEqual([], got)

    def test_blank_text_and_unknown_events_are_ignored(self):
        got = self.run_trail([
            {"type": "text_delta", "text": ""},
            {"type": "something_new", "text": "?"},
            {"type": "tool_use", "name": "Read", "input": {}},
        ])
        self.assertEqual([ ("tool", "Read") ], got)

    def test_long_narration_is_trimmed_to_one_line(self):
        got = self.run_trail([
            {"type": "text_delta", "text": "line one\nline two " + ("x" * 900)},
            {"type": "tool_use", "name": "Read", "input": {}},
        ])
        text = got[0][1]
        self.assertNotIn("\n", text, "the trail is a list of lines, not prose")
        self.assertLessEqual(len(text), 401)


class RoomStatusTest(unittest.IsolatedAsyncioTestCase):
    """A run can sit queued behind a twenty-minute one. Silence and
    "never arrived" look identical from the room."""

    async def test_queued_and_working_are_announced(self):
        link = fa.CableLink()
        await fa.room_status(link, "rm123456", "ms123456", "queued")
        await fa.room_status(link, "rm123456", "ms123456", "working")

        frames = [json.loads(json.loads(f)["data"]) for f in link.outbox]
        self.assertEqual(["room_status", "room_status"], [f["action"] for f in frames])
        self.assertEqual(["queued", "working"], [f["state"] for f in frames])
        self.assertEqual("ms123456", frames[0]["reply_to"])

    async def test_no_message_to_hang_it_on_means_no_frame(self):
        link = fa.CableLink()
        await fa.room_status(link, "rm123456", None, "queued")
        self.assertEqual([], link.outbox, "a status with nothing to attach to is noise")


class WorkSpoolTest(SelfUpdateSandbox, unittest.IsolatedAsyncioTestCase):
    """A restart used to swallow whatever was queued. The server
    broadcasts each message exactly once, so a task lost here is a
    question the asker never hears back about."""

    async def asyncSetUp(self):
        self.start_sandbox()
        fa.WORK_SPOOL_PATH = fa.STATE_DIR / "work-spool.json"
        self._saved_queue, self._saved_work = fa._work_queue, fa._current_work
        fa._work_queue, fa._current_work = asyncio.Queue(), None

    async def asyncTearDown(self):
        fa._work_queue, fa._current_work = self._saved_queue, self._saved_work
        self.stop_sandbox()

    async def test_the_in_flight_task_is_spooled_ahead_of_the_queued_ones(self):
        # The one being worked was taken off the queue but never
        # answered — from the asker's side it is exactly as lost as
        # the ones behind it, and it was asked first.
        fa._current_work = ("room", {"message": {"body": "the one running"}})
        fa._work_queue.put_nowait(("room", {"message": {"body": "next"}}))

        self.assertEqual(2, fa.spool_pending_work())
        replayed = fa.take_spooled_work()
        self.assertEqual(["the one running", "next"],
                         [i["payload"]["message"]["body"] for i in replayed])

    async def test_nothing_pending_leaves_no_spool(self):
        self.assertEqual(0, fa.spool_pending_work())
        self.assertFalse(fa.WORK_SPOOL_PATH.exists())
        self.assertEqual([], fa.take_spooled_work())

    async def test_taking_the_spool_deletes_it(self):
        fa._current_work = ("room", {"message": {"body": "x"}})
        fa.spool_pending_work()
        self.assertEqual(1, len(fa.take_spooled_work()))
        self.assertEqual([], fa.take_spooled_work(), "a spool must not replay on every boot")

    async def test_a_stale_spool_is_dropped(self):
        fa._current_work = ("room", {"message": {"body": "x"}})
        fa.spool_pending_work()
        stale = json.loads(fa.WORK_SPOOL_PATH.read_text())
        stale["at"] = stale["at"] - fa.WORK_SPOOL_MAX_AGE_SECONDS - 60
        fa.WORK_SPOOL_PATH.write_text(json.dumps(stale), encoding="utf-8")
        self.assertEqual([], fa.take_spooled_work())

    async def test_a_corrupt_or_malformed_spool_is_dropped_not_raised(self):
        fa.STATE_DIR.mkdir(parents=True, exist_ok=True)
        fa.WORK_SPOOL_PATH.write_text("{not json", encoding="utf-8")
        self.assertEqual([], fa.take_spooled_work())

        fa.WORK_SPOOL_PATH.write_text(
            json.dumps({"at": time.time(),
                        "items": [{"kind": "room"}, {"payload": {}}, {"kind": "room", "payload": {"ok": 1}}]}),
            encoding="utf-8")
        self.assertEqual([{"kind": "room", "payload": {"ok": 1}}], fa.take_spooled_work())

    async def test_the_spool_is_bounded(self):
        for i in range(fa.WORK_SPOOL_MAX + 20):
            fa._work_queue.put_nowait(("room", {"i": i}))
        self.assertEqual(fa.WORK_SPOOL_MAX, fa.spool_pending_work())


class AnnounceRestartTest(SelfUpdateSandbox, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.start_sandbox()

    async def asyncTearDown(self):
        self.stop_sandbox()

    def write_notice(self, from_version):
        fa.STATE_DIR.mkdir(parents=True, exist_ok=True)
        fa.RESTART_NOTICE_PATH.write_text(json.dumps({
            "room_id": "room1234", "reply_to": "msg5678",
            "from_version": from_version, "to_version": fa.AGENT_VERSION,
            "at": time.time(),
        }), encoding="utf-8")

    async def test_the_process_that_came_back_reports_its_version(self):
        self.write_notice("vroxy_dispatch 0.0.1")
        link = fa.CableLink()
        await fa.announce_restart(link)

        bodies = sent_bodies(link)
        self.assertEqual(1, len(bodies))
        self.assertIn(fa.AGENT_VERSION, bodies[0])
        self.assertIn("0.0.1", bodies[0], "the version it came from is the interesting half")

    async def test_nothing_is_said_when_no_restart_happened(self):
        link = fa.CableLink()
        await fa.announce_restart(link)
        self.assertEqual([], sent_bodies(link))

    async def test_a_reconnect_does_not_re_announce(self):
        fa.write_restart_notice("room1234", None, "vroxy_dispatch 9.9.10")
        link = fa.CableLink()
        await fa.announce_restart(link)
        await fa.announce_restart(link)
        self.assertEqual(1, len(sent_bodies(link)))


class StreamLinesStallTest(unittest.TestCase):
    """`_stream_lines` is the only thing standing between a wedged
    claude and a room that waits forever."""

    def setUp(self):
        self._real = fa.STALL_SECONDS
        fa.STALL_SECONDS = 0.05

    def tearDown(self):
        fa.STALL_SECONDS = self._real

    def test_lines_pass_through_in_order_then_stop_at_eof(self):
        proc = FakeProc(["a\n", "b\n"])
        self.assertEqual(["a\n", "b\n"], list(fa._stream_lines(proc)))

    def test_silence_yields_a_stall_marker_per_window(self):
        proc = FakeProc(["a\n"], delay_before_last=0.18)
        got = list(fa._stream_lines(proc))
        self.assertGreaterEqual(got.count(fa._STALLED), 2)
        self.assertEqual(["a\n"], [g for g in got if g is not fa._STALLED])

    def test_a_stream_that_never_ends_still_lets_the_caller_out(self):
        proc = FakeProc([], never_eof=True)
        seen = 0
        for item in fa._stream_lines(proc):
            self.assertIs(fa._STALLED, item)
            seen += 1
            if seen == 3:
                break
        self.assertEqual(3, seen)


class FakeProc:
    """Stands in for Popen: a readable stdout and a poll()."""

    def __init__(self, lines, delay_before_last=0.0, never_eof=False):
        self._lines = list(lines)
        self._delay = delay_before_last
        self._never_eof = never_eof
        self.stdout = self
        self.pid = os.getpid()
        self.returncode = None

    def __iter__(self):
        for line in self._lines:
            yield line
        if self._delay:
            time.sleep(self._delay)
        while self._never_eof:
            time.sleep(0.01)

    def poll(self):
        return self.returncode


class StallCeilingTest(unittest.TestCase):
    """The 90 s ceiling is a rule, not a suggestion — every wait in
    this process is derived from it, so a regression that raises one
    of them fails here."""

    def test_the_ceiling_is_ninety_seconds(self):
        self.assertEqual(90, fa.STALL_SECONDS)

    def test_the_hard_cap_is_the_ceiling_times_the_window_count(self):
        self.assertEqual(fa.STALL_SECONDS * fa.STALL_WINDOWS_BEFORE_KILL,
                         fa.SUBPROCESS_HARD_CAP_SECONDS)

    def test_no_subprocess_wait_in_the_module_outruns_the_hard_cap(self):
        source = Path(fa.__file__).read_text()
        literals = [int(m) for m in re.findall(r"timeout=(\d+)", source)]
        self.assertTrue(literals)
        worst = max(literals)
        self.assertLessEqual(
            worst, fa.SUBPROCESS_HARD_CAP_SECONDS,
            f"a literal timeout={worst} exceeds the "
            f"{fa.SUBPROCESS_HARD_CAP_SECONDS}s cap — background it and poll instead")

    def test_the_room_prompt_carries_the_ceiling(self):
        self.assertIn("90 SECONDS", fa.ROOM_SYSTEM_PROMPT)
        self.assertIn("timeout: 90000", fa.ROOM_SYSTEM_PROMPT)


class StallTrailTest(unittest.TestCase):
    """A stalled run must reach the room; silence rendered as nothing
    is the bug the whole ceiling exists to prevent."""

    def setUp(self):
        self.lines = []
        self.trail = fa.ProgressTrail(lambda kind, text: self.lines.append((kind, text)))

    def test_a_stall_becomes_a_visible_trail_line(self):
        self.trail.feed({"type": "stalled", "seconds": 90,
                         "window": 1, "max_windows": 4})
        self.assertEqual([("stalled", "still working — nothing back for 90s")],
                         self.lines)

    def test_the_last_window_says_it_is_giving_up(self):
        self.trail.feed({"type": "stalled", "seconds": 360,
                         "window": 4, "max_windows": 4})
        self.assertIn("giving up", self.lines[-1][1])

    def test_held_narration_is_flushed_before_the_stall_line(self):
        self.trail.feed({"type": "text_delta", "text": "Running the suite."})
        self.trail.feed({"type": "stalled", "seconds": 90,
                         "window": 1, "max_windows": 4})
        self.assertEqual(["text", "stalled"], [kind for kind, _ in self.lines])



class _FakeProc:
    """Stands in for a `codex exec --json` subprocess: stdout is the
    canned JSONL, stderr is empty, and it has already exited."""

    def __init__(self, lines, returncode=0):
        import io
        self.stdout = io.StringIO("".join(f"{l}\n" for l in lines))
        self.stderr = io.StringIO("")
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


class CodexStreamTest(unittest.TestCase):
    """The codex event stream must come out of `on_event` in exactly
    the vocabulary the claude one does — everything downstream reads
    that and nothing else."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._sid_dir = fa.SID_DIR
        fa.SID_DIR = Path(self.tmp) / "sids"
        self._popen = fa.subprocess.Popen
        self._bin = fa._resolve_codex_bin
        fa._resolve_codex_bin = lambda: "/usr/bin/true"

    def tearDown(self):
        fa.SID_DIR = self._sid_dir
        fa.subprocess.Popen = self._popen
        fa._resolve_codex_bin = self._bin
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, lines, returncode=0, **kw):
        events = []
        fa.subprocess.Popen = lambda *a, **k: _FakeProc(lines, returncode)
        text = fa.run_codex_streamed(
            "do the thing", "vroxy_web", events.append,
            work_dir_override=Path(self.tmp), **kw)
        return text, events

    def test_a_shell_call_becomes_a_tool_use_when_it_starts(self):
        text, events = self._run([
            json.dumps({"type": "thread.started", "thread_id": "T1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.started", "item": {
                "id": "i1", "type": "command_execution",
                "command": "ls -la", "status": "in_progress"}}),
            json.dumps({"type": "item.completed", "item": {
                "id": "i1", "type": "command_execution",
                "command": "ls -la", "exit_code": 0,
                "aggregated_output": "note.txt"}}),
            json.dumps({"type": "item.completed", "item": {
                "id": "i2", "type": "agent_message", "text": "One file."}}),
            json.dumps({"type": "turn.completed",
                        "usage": {"input_tokens": 10, "output_tokens": 2}}),
        ])
        self.assertEqual(text, "One file.")
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, ["tool_use", "text_delta", "result"])
        self.assertEqual(events[0]["name"], "Bash")
        self.assertEqual(events[0]["input"]["command"], "ls -la")
        self.assertFalse(events[-1]["is_error"])
        self.assertIsNone(events[-1]["cost_usd"])

    def test_the_last_message_is_the_answer_not_all_of_them(self):
        text, events = self._run([
            json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": "I will look at the file."}}),
            json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": "It has three lines."}}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ])
        self.assertEqual(text, "It has three lines.")
        self.assertEqual([e["type"] for e in events],
                         ["text_delta", "text_delta", "result"])

    def test_reasoning_arrives_as_thinking(self):
        _, events = self._run([
            json.dumps({"type": "item.completed", "item": {
                "type": "reasoning", "text": "Weighing two options."}}),
            json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": "Option B."}}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ])
        self.assertEqual(events[0]["type"], "thinking")
        self.assertEqual(events[0]["text"], "Weighing two options.")

    def test_an_item_kind_this_build_predates_is_still_reported(self):
        _, events = self._run([
            json.dumps({"type": "item.completed", "item": {
                "id": "i9", "type": "web_search", "query": "rails 8"}}),
            json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": "Found it."}}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ])
        self.assertEqual(events[0]["type"], "tool_use")
        self.assertEqual(events[0]["name"], "web_search")
        self.assertEqual(events[0]["input"], {"query": "rails 8"})

    def test_the_thread_id_is_stored_for_the_next_turn(self):
        self._run([
            json.dumps({"type": "thread.started", "thread_id": "T-42"}),
            json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": "done"}}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ], session_key="room-7")
        sid = fa._streamed_sid_file("codex_room-7")
        self.assertTrue(sid.exists())
        self.assertEqual(sid.read_text(), "T-42")

    def test_a_failed_turn_with_nothing_to_show_raises(self):
        with self.assertRaises(RuntimeError):
            self._run([json.dumps({"type": "turn.failed", "usage": {}})])

    def test_a_failed_turn_that_did_work_still_answers(self):
        text, _ = self._run([
            json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": "Got partway."}}),
            json.dumps({"type": "turn.failed", "usage": {}}),
        ])
        self.assertEqual(text, "Got partway.")


class EngineSelectorTest(unittest.TestCase):
    def setUp(self):
        self._engine = fa.DISPATCH_ENGINE

    def tearDown(self):
        fa.DISPATCH_ENGINE = self._engine

    def test_the_default_engine_is_claude(self):
        fa.DISPATCH_ENGINE = "claude"
        seen = {}
        real = fa.run_claude_streamed
        fa.run_claude_streamed = lambda *a, **k: seen.setdefault("ran", "claude")
        try:
            fa.run_agent_streamed("p", "vroxy_web", lambda e: None)
        finally:
            fa.run_claude_streamed = real
        self.assertEqual(seen["ran"], "claude")

    def test_codex_selects_the_codex_runner(self):
        fa.DISPATCH_ENGINE = "codex"
        seen = {}
        real = fa.run_codex_streamed
        fa.run_codex_streamed = lambda *a, **k: seen.setdefault("ran", "codex")
        try:
            fa.run_agent_streamed("p", "vroxy_web", lambda e: None)
        finally:
            fa.run_codex_streamed = real
        self.assertEqual(seen["ran"], "codex")

    def test_a_typo_is_refused_rather_than_silently_run(self):
        fa.DISPATCH_ENGINE = "cldue"
        with self.assertRaises(ValueError):
            fa.run_agent_streamed("p", "vroxy_web", lambda e: None)



class EngineAddressingTest(unittest.TestCase):
    """One workspace can run a Claude instance and a Codex instance;
    both see every room message on the tenant channel, so each must
    answer only what was routed to it."""

    def setUp(self):
        self._engine = fa.DISPATCH_ENGINE

    def tearDown(self):
        fa.DISPATCH_ENGINE = self._engine

    def test_claude_answers_a_claude_code_agent(self):
        fa.DISPATCH_ENGINE = "claude"
        self.assertTrue(fa._is_ours({"agent": {"kind": "claude_code"}}))

    def test_claude_leaves_codex_work_alone(self):
        fa.DISPATCH_ENGINE = "claude"
        self.assertFalse(fa._is_ours({"agent": {"kind": "codex"}}))

    def test_codex_answers_a_codex_agent(self):
        fa.DISPATCH_ENGINE = "codex"
        self.assertTrue(fa._is_ours({"agent": {"kind": "codex"}}))

    def test_codex_leaves_claude_work_alone(self):
        fa.DISPATCH_ENGINE = "codex"
        self.assertFalse(fa._is_ours({"agent": {"kind": "claude_code"}}))

    def test_a_frame_with_no_agent_is_still_answered(self):
        fa.DISPATCH_ENGINE = "codex"
        self.assertTrue(fa._is_ours({"room": {"hashid": "r1"}}))
        self.assertTrue(fa._is_ours({"agent": None}))
        self.assertTrue(fa._is_ours({"agent": {"name": "Dispatch"}}))

    def test_the_heartbeat_names_the_engine(self):
        sent = {}

        async def fake_send(ws, kind, payload, buffer=True):
            sent.update(payload)

        real = fa.cable_send
        fa.cable_send = fake_send
        fa.DISPATCH_ENGINE = "codex"
        try:
            asyncio.run(fa.heartbeat(None))
        finally:
            fa.cable_send = real
        self.assertEqual(sent["meta"]["engine"], "codex")



class HarnessProbeTest(unittest.TestCase):
    """What coding CLIs this box has, reported so an operator does not
    have to shell in to find out."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._path = os.environ.get("PATH", "")
        self._engine = fa.DISPATCH_ENGINE
        os.environ["PATH"] = self.tmp
        fa._harness_cache = None

    def tearDown(self):
        os.environ["PATH"] = self._path
        fa.DISPATCH_ENGINE = self._engine
        fa._harness_cache = None
        for var in ("CLAUDE_BIN", "CODEX_BIN", "GEMINI_BIN"):
            os.environ.pop(var, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install(self, name, version_output="9.9.9", exit_code=0):
        path = Path(self.tmp) / name
        path.write_text(f'#!/bin/sh\necho "{version_output}"\nexit {exit_code}\n')
        path.chmod(0o755)
        return path

    def test_only_what_is_installed_is_reported(self):
        self._install("codex", "codex-cli 0.153.4")
        found = fa.probe_harnesses()
        self.assertEqual([h["id"] for h in found], ["codex"])
        self.assertEqual(found[0]["version"], "codex-cli 0.153.4")

    def test_nothing_installed_reports_an_empty_list_not_an_error(self):
        self.assertEqual(fa.probe_harnesses(), [])

    def test_a_cli_that_will_not_report_a_version_is_still_listed(self):
        self._install("aider", "", exit_code=1)
        found = fa.probe_harnesses()
        self.assertEqual([h["id"] for h in found], ["aider"])
        self.assertNotIn("version", found[0],
                         "installed-but-broken must not read as absent")

    def test_the_running_engine_is_flagged_active(self):
        self._install("claude")
        self._install("codex")
        fa.DISPATCH_ENGINE = "codex"
        found = {h["id"]: h for h in fa.probe_harnesses()}
        self.assertTrue(found["codex"]["active"])
        self.assertFalse(found["claude"]["active"])

    def test_a_harness_we_cannot_drive_is_reported_without_an_engine(self):
        self._install("gemini")
        found = fa.probe_harnesses()
        self.assertEqual(found[0]["id"], "gemini")
        self.assertNotIn("engine", found[0])

    def test_an_explicit_bin_override_wins_over_path(self):
        outside = Path(self.tmp) / "elsewhere"
        outside.mkdir()
        binary = outside / "codex-real"
        binary.write_text('#!/bin/sh\necho "codex-cli 1.2.3"\n')
        binary.chmod(0o755)
        os.environ["CODEX_BIN"] = str(binary)
        found = {h["id"]: h for h in fa.probe_harnesses()}
        self.assertEqual(found["codex"]["version"], "codex-cli 1.2.3")

    def test_the_probe_is_cached_rather_than_run_every_heartbeat(self):
        self._install("codex")
        calls = []
        real = fa.probe_harnesses
        fa.probe_harnesses = lambda: calls.append(1) or [{"id": "codex"}]
        try:
            fa.harnesses(now=1000.0)
            fa.harnesses(now=1000.0 + fa.HARNESS_REPROBE_SECONDS - 1)
            self.assertEqual(len(calls), 1)
            fa.harnesses(now=1000.0 + fa.HARNESS_REPROBE_SECONDS + 1)
            self.assertEqual(len(calls), 2,
                             "a CLI installed while dispatch runs should turn "
                             "up without a restart")
        finally:
            fa.probe_harnesses = real

    def test_every_probe_timeout_stays_under_the_stall_ceiling(self):
        worst_case = fa.HARNESS_PROBE_TIMEOUT * len(fa.KNOWN_HARNESSES)
        self.assertLess(worst_case, fa.STALL_SECONDS,
                        "a full sweep must not outlast the stall window")

    def test_the_heartbeat_carries_the_harness_list(self):
        self._install("codex")
        sent = {}

        async def fake_send(ws, kind, payload, buffer=True):
            sent.update(payload)

        real = fa.cable_send
        fa.cable_send = fake_send
        try:
            asyncio.run(fa.heartbeat(None))
        finally:
            fa.cable_send = real
        self.assertEqual([h["id"] for h in sent["meta"]["harnesses"]], ["codex"])


if __name__ == "__main__":
    unittest.main()


class ProgressActionTest(unittest.TestCase):
    def test_ordinary_lines_are_sent(self):
        self.assertEqual("send", fa.progress_action(0, False))
        self.assertEqual(
            "send",
            fa.progress_action(fa.ROOM_PROGRESS_MAX - 1, False))

    def test_the_cap_says_so_once_instead_of_going_silent(self):
        self.assertEqual(
            "notice",
            fa.progress_action(fa.ROOM_PROGRESS_MAX, False))

    def test_after_the_notice_further_lines_drop_quietly(self):
        self.assertEqual(
            "drop",
            fa.progress_action(fa.ROOM_PROGRESS_MAX, True))
        self.assertEqual(
            "drop",
            fa.progress_action(fa.ROOM_PROGRESS_MAX + 500, True))

    def test_the_live_cap_clears_the_old_silent_120(self):
        self.assertGreater(
            fa.ROOM_PROGRESS_MAX, 500,
            "a run of a few hundred steps must not stop reporting mid-flight")
        self.assertEqual("send", fa.progress_action(200, False))


class TaskLabelTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        fa.set_task_label("")

    def test_nothing_in_flight_reads_as_idle(self):
        fa.set_task_label("")
        self.assertEqual("idle", fa.task_label())

    def test_the_label_is_one_line_and_bounded(self):
        fa.set_task_label("#Claude fix the thing\nand then\nship it " + "x" * 200)
        label = fa.task_label()
        self.assertLessEqual(len(label), fa.TASK_LABEL_MAX + 1)
        self.assertTrue(label.endswith("\u2026"))
        self.assertNotIn("\n", label)

    def test_every_record_carries_the_task(self):
        fa.set_task_label("#Claude what is it working on")
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "x", None, None)
        self.assertTrue(fa._TaskFilter().filter(record))
        self.assertEqual("#Claude what is it working on", record.task)

    def test_an_idle_record_says_idle_rather_than_blank(self):
        fa.set_task_label("")
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "x", None, None)
        fa._TaskFilter().filter(record)
        self.assertEqual("idle", record.task)

    async def test_the_label_reaches_the_claude_worker_thread(self):
        fa.set_task_label("#Claude tail the log")
        seen = await asyncio.to_thread(fa.task_label)
        self.assertEqual("#Claude tail the log", seen,
                         "tool_use lines are logged off the event loop; "
                         "an unpropagated context would leave them unlabelled")


class QueuedEditTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fa._work_queue = asyncio.Queue()

    async def asyncTearDown(self):
        fa._work_queue = None

    def room_task(self, hashid, body, room="room1"):
        return ("room", {"room": {"hashid": room},
                         "message": {"hashid": hashid, "body": body}})

    async def drain(self):
        out = []
        while not fa._work_queue.empty():
            out.append(fa._work_queue.get_nowait())
        return out

    async def test_a_queued_task_picks_up_the_edit(self):
        fa._work_queue.put_nowait(self.room_task("m1", "old words"))

        self.assertTrue(fa.apply_queued_edit("room1", "m1", "new words"))

        items = await self.drain()
        self.assertEqual("new words", items[0][1]["message"]["body"])

    async def test_order_survives_the_rewrite(self):
        for i in range(4):
            fa._work_queue.put_nowait(self.room_task(f"m{i}", f"body {i}"))

        fa.apply_queued_edit("room1", "m2", "edited")

        items = await self.drain()
        self.assertEqual(["m0", "m1", "m2", "m3"],
                         [i[1]["message"]["hashid"] for i in items])
        self.assertEqual("edited", items[2][1]["message"]["body"])

    async def test_an_unknown_message_changes_nothing(self):
        fa._work_queue.put_nowait(self.room_task("m1", "keep me"))

        self.assertFalse(fa.apply_queued_edit("room1", "nope", "clobbered"))

        items = await self.drain()
        self.assertEqual("keep me", items[0][1]["message"]["body"])

    async def test_the_same_id_in_another_room_is_not_touched(self):
        fa._work_queue.put_nowait(self.room_task("m1", "mine", room="other"))

        self.assertFalse(fa.apply_queued_edit("room1", "m1", "clobbered"))

        items = await self.drain()
        self.assertEqual("mine", items[0][1]["message"]["body"])

    async def test_non_room_work_is_left_alone(self):
        fa._work_queue.put_nowait(("feedback", {"message": {"hashid": "m1", "body": "fb"}}))

        self.assertFalse(fa.apply_queued_edit("room1", "m1", "clobbered"))

        items = await self.drain()
        self.assertEqual("fb", items[0][1]["message"]["body"])
