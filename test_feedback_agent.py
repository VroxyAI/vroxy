"""
Unit tests for the pure-Python bits of feedback_agent.py — prompt
building and Claude-output parsing.  No websocket or subprocess
here; those get exercised in integration when we point the agent
at a real vroxy_web instance.
"""

import json
import subprocess
import tempfile
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


if __name__ == "__main__":
    unittest.main()
