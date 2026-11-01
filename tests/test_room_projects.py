import os

os.environ.setdefault("LOG_FILE", "")

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import feedback_agent as fa


class ProjectRootTestCase(unittest.TestCase):
    """A CODE_ROOT holding several checkouts, which is the shape a box
    of branch checkouts has."""

    CHECKOUTS = ("brevitas", "brev72true", "brev734", "brev81")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "code"
        for name in self.CHECKOUTS:
            (self.root / name / ".git").mkdir(parents=True)
        # A directory that is not a checkout, and one that is but was
        # never allowlisted.
        (self.root / "notes").mkdir(parents=True)
        (self.root / "unrelated" / ".git").mkdir(parents=True)

        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.patch(fa, "CODE_ROOT", self.root)
        self.patch(fa, "PROJECT", "brevitas")
        self.patch(fa, "STATE_DIR", self.state)
        self.patch(fa, "PROJECTS_RAW", ",".join(self.CHECKOUTS))
        self.patch(fa, "SERVICE_UNIT", "vroxy-dispatch@brevitas.service")

    def patch(self, target, attr, value):
        patcher = mock.patch.object(target, attr, value)
        patcher.start()
        self.addCleanup(patcher.stop)


class AllowlistTest(ProjectRootTestCase):
    def test_unset_projects_pins_the_agent_to_one_checkout(self):
        self.patch(fa, "PROJECTS_RAW", "")
        self.assertEqual(["brevitas"], fa.allowed_projects())

    def test_the_pinned_project_is_always_allowed(self):
        self.patch(fa, "PROJECTS_RAW", "brev81")
        self.assertEqual(["brevitas", "brev81"], fa.allowed_projects())

    def test_star_means_every_checkout_under_code_root(self):
        self.patch(fa, "PROJECTS_RAW", "*")
        self.assertEqual(sorted(self.CHECKOUTS) + ["unrelated"],
                         fa.allowed_projects())

    def test_a_non_checkout_never_becomes_a_project(self):
        self.patch(fa, "PROJECTS_RAW", "notes")
        self.assertEqual(["brevitas"], fa.allowed_projects())

    def test_a_typo_in_the_env_file_does_not_shrink_the_rest(self):
        self.patch(fa, "PROJECTS_RAW", "brev81,nope,brev734")
        self.assertEqual(["brevitas", "brev81", "brev734"],
                         fa.allowed_projects())

    def test_whitespace_and_commas_both_separate(self):
        self.patch(fa, "PROJECTS_RAW", "brev81 brev734,brev72true")
        self.assertEqual(["brevitas", "brev81", "brev734", "brev72true"],
                         fa.allowed_projects())


class RoomBindingTest(ProjectRootTestCase):
    def test_a_room_binding_round_trips(self):
        ok, detail = fa.set_room_project("rmAAAA", "brev81")
        self.assertTrue(ok, detail)
        self.assertEqual(str(self.root / "brev81"), detail)
        self.assertEqual("brev81", fa.load_room_project("rmAAAA"))

    def test_rooms_do_not_share_a_binding(self):
        fa.set_room_project("rmAAAA", "brev81")
        fa.set_room_project("rmBBBB", "brev734")
        self.assertEqual("brev81", fa.load_room_project("rmAAAA"))
        self.assertEqual("brev734", fa.load_room_project("rmBBBB"))
        self.assertIsNone(fa.load_room_project("rmCCCC"))

    def test_an_unbound_room_resolves_to_the_unit_project(self):
        self.assertEqual("brevitas", fa.resolve_room_project("rmAAAA"))

    def test_a_project_outside_the_allowlist_is_refused(self):
        ok, detail = fa.set_room_project("rmAAAA", "unrelated")
        self.assertFalse(ok)
        self.assertIn("unknown project", detail)
        self.assertIsNone(fa.load_room_project("rmAAAA"))

    def test_a_path_traversal_is_refused(self):
        for attempt in ("../../etc", "/etc", "..", "brev81/../unrelated"):
            with self.subTest(attempt=attempt):
                ok, _ = fa.set_room_project("rmAAAA", attempt)
                self.assertFalse(ok)
        self.assertIsNone(fa.load_room_project("rmAAAA"))

    def test_a_pinned_agent_says_how_to_allow_switching(self):
        self.patch(fa, "PROJECTS_RAW", "")
        ok, detail = fa.set_room_project("rmAAAA", "brev81")
        self.assertFalse(ok)
        self.assertIn("PROJECTS", detail)

    def test_revoking_a_project_releases_the_rooms_still_on_it(self):
        fa.set_room_project("rmAAAA", "brev81")
        self.patch(fa, "PROJECTS_RAW", "brev734")
        self.assertIsNone(fa.load_room_project("rmAAAA"))
        self.assertEqual("brevitas", fa.resolve_room_project("rmAAAA"))

    def test_a_binding_whose_checkout_vanished_is_refused_up_front(self):
        ok, _ = fa.set_room_project("rmAAAA", "brev81")
        self.assertTrue(ok)
        for child in sorted((self.root / "brev81").iterdir()):
            child.rmdir()
        (self.root / "brev81").rmdir()
        # Still allowlisted by name, but no longer a checkout on disk.
        self.assertIsNone(fa.load_room_project("rmAAAA"))

    def test_the_state_file_is_scoped_to_the_instance(self):
        a = fa.room_project_path("rmAAAA")
        self.patch(fa, "SERVICE_UNIT", "vroxy-dispatch@other.service")
        self.assertNotEqual(a, fa.room_project_path("rmAAAA"))

    def test_a_hostile_room_id_cannot_escape_the_state_dir(self):
        path = fa.room_project_path("../../etc/passwd")
        self.assertEqual(self.state, path.parent)


class PrecedenceTest(ProjectRootTestCase):
    """Server frame → room binding → unit env, widest authority first."""

    def test_the_server_frame_outranks_a_room_binding(self):
        fa.set_room_project("rmAAAA", "brev81")
        self.assertEqual(
            "brev734",
            fa.resolve_room_project("rmAAAA", {"agent": {"project": "brev734"}}))

    def test_a_room_binding_outranks_the_unit_project(self):
        fa.set_room_project("rmAAAA", "brev81")
        self.assertEqual("brev81", fa.resolve_room_project("rmAAAA", {}))

    def test_the_env_file_outranks_the_server(self):
        payload = {"agent": {"project": "unrelated"}}
        self.assertIsNone(fa.frame_project(payload))
        self.assertEqual("brevitas", fa.resolve_room_project("rmAAAA", payload))

    def test_a_server_frame_does_not_persist_as_the_binding(self):
        fa.resolve_room_project("rmAAAA", {"agent": {"project": "brev734"}})
        self.assertIsNone(fa.load_room_project("rmAAAA"))

    def test_a_frame_with_no_project_falls_through(self):
        for payload in ({}, {"agent": {}}, {"agent": {"model": "x/y"}},
                        {"agent": None}, {"agent": {"project": "  "}}):
            with self.subTest(payload=payload):
                self.assertIsNone(fa.frame_project(payload))


class ProjectCommandParseTest(unittest.TestCase):
    def test_the_command_and_its_aliases_parse(self):
        for word in ("/project", "/cd", "/repo"):
            with self.subTest(word=word):
                self.assertEqual((word, None), fa.project_command(word))
                self.assertEqual((word, "brev81"),
                                 fa.project_command(f"{word} brev81"))

    def test_case_and_surrounding_whitespace_do_not_matter(self):
        self.assertEqual(("/project", "brev81"),
                         fa.project_command("  /PROJECT   brev81  "))

    def test_only_the_first_argument_counts(self):
        self.assertEqual(("/project", "brev81"),
                         fa.project_command("/project brev81 please"))

    def test_ordinary_messages_are_not_commands(self):
        for body in ("", "   ", "what project is this?", "/projects",
                     "/harness claude", "tell me about /project"):
            with self.subTest(body=body):
                self.assertIsNone(fa.project_command(body))

    def test_a_bare_checkout_name_is_not_a_switch(self):
        """`/harness` accepts a bare engine slug; this must not, or
        saying the word `brev81` would move the room."""
        self.assertIsNone(fa.project_command("brev81"))


class SessionIsolationTest(ProjectRootTestCase):
    def test_each_checkout_gets_its_own_room_session(self):
        a = fa._room_session_key("rmAAAA", "brev81")
        b = fa._room_session_key("rmAAAA", "brev734")
        self.assertNotEqual(a, b)

    def test_the_key_is_unchanged_for_a_room_that_never_switched(self):
        self.assertEqual(fa._room_session_key("rmAAAA"),
                         fa._room_session_key("rmAAAA", "brevitas"))

    def test_a_full_reset_clears_every_checkouts_room_sessions(self):
        with tempfile.TemporaryDirectory() as sid:
            self.patch(fa, "SID_DIR", Path(sid))
            keys = [fa._room_session_key("rmAAAA", p) for p in self.CHECKOUTS]
            for key in keys:
                fa._streamed_sid_file(key).write_text("thread-id")
            removed = fa.clear_sessions()
            for key in keys:
                self.assertIn(fa._streamed_sid_file(key).name, removed,
                              f"--reset left {key} behind")


class ProjectMenuTest(ProjectRootTestCase):
    def setUp(self):
        super().setUp()
        self.patch(fa, "_current_branch", lambda d: f"{Path(d).name}-branch")

    def test_the_menu_marks_the_room_and_lists_the_rest(self):
        fa.set_room_project("rmAAAA", "brev81")
        text = fa._project_menu("rmAAAA")
        self.assertIn("Working in **brev81**", text)
        self.assertIn("brev81-branch", text)
        self.assertIn("← this room", text)
        for name in self.CHECKOUTS:
            self.assertIn(f"`{name}`", text)

    def test_a_pinned_agent_is_told_how_to_open_it_up(self):
        self.patch(fa, "PROJECTS_RAW", "")
        text = fa._project_menu("rmAAAA")
        self.assertIn("pinned to `brevitas`", text)
        self.assertIn("PROJECTS=", text)
        self.assertNotIn("Switch with", text)


class FakeLink:
    def __init__(self):
        self.outbox = []

    async def send(self, frame):
        self.outbox.append(frame)


def replies(link):
    out = []
    for frame in link.outbox:
        data = json.loads(json.loads(frame)["data"])
        if data.get("action") == "room_reply":
            out.append(data.get("body") or "")
    return out


class ProjectCommandHandlerTest(ProjectRootTestCase):
    def setUp(self):
        super().setUp()
        self.patch(fa, "_current_branch", lambda d: f"{Path(d).name}-branch")
        self.link = FakeLink()

    def run_cmd(self, target):
        asyncio.run(fa.handle_project_command(self.link, "rmAAAA", target, "msg1"))
        return replies(self.link)

    def test_a_switch_confirms_the_tree_and_the_branch(self):
        body = self.run_cmd("brev81")[-1]
        self.assertIn("brev81", body)
        self.assertIn(str(self.root / "brev81"), body)
        self.assertIn("brev81-branch", body)
        self.assertEqual("brev81", fa.load_room_project("rmAAAA"))

    def test_a_refused_switch_says_why_and_changes_nothing(self):
        body = self.run_cmd("unrelated")[-1]
        self.assertIn("⚠️", body)
        self.assertIn("unknown project", body)
        self.assertIsNone(fa.load_room_project("rmAAAA"))

    def test_bare_shows_the_menu_without_changing_anything(self):
        body = self.run_cmd(None)[-1]
        self.assertIn("Working in **brevitas**", body)
        self.assertIsNone(fa.load_room_project("rmAAAA"))


class HeartbeatTest(ProjectRootTestCase):
    def meta(self):
        link = FakeLink()
        with mock.patch.object(fa, "TELEMETRY_ENABLED", False):
            asyncio.run(fa.heartbeat(link))
        data = json.loads(json.loads(link.outbox[-1])["data"])
        return data.get("meta") or {}

    def test_the_choices_are_reported_so_the_web_can_offer_them(self):
        meta = self.meta()
        self.assertEqual(list(self.CHECKOUTS), meta.get("projects"))
        self.assertEqual(str(self.root), meta.get("code_root_base"))

    def test_a_single_project_agent_reports_no_menu(self):
        self.patch(fa, "PROJECTS_RAW", "")
        meta = self.meta()
        self.assertNotIn("projects", meta)
        self.assertEqual("brevitas", meta.get("project"))


class WorkDirTest(ProjectRootTestCase):
    def test_the_work_dir_is_the_checkout_under_code_root(self):
        self.assertEqual(self.root / "brev81", fa.project_work_dir("brev81"))

    def test_the_prompt_names_the_checkout_the_run_happens_in(self):
        payload = {"room": {"name": "ops", "hashid": "rmAAAA"},
                   "message": {"body": "ship it"},
                   "sender": {"name": "will"}}
        prompt = fa.build_room_prompt(payload, None, "brev81")
        self.assertIn("Repo: brev81", prompt)
        self.assertNotIn("Repo: brevitas", prompt)

    def test_the_prompt_falls_back_to_the_unit_project(self):
        payload = {"room": {"name": "ops", "hashid": "rmAAAA"},
                   "message": {"body": "ship it"},
                   "sender": {"name": "will"}}
        self.assertIn("Repo: brevitas", fa.build_room_prompt(payload))


if __name__ == "__main__":
    unittest.main()
