import os

os.environ.setdefault("LOG_FILE", "")

import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import feedback_agent as fa


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, timeout=30).stdout.strip()


def frames(link, action):
    out = []
    for frame in link.outbox:
        data = json.loads(json.loads(frame)["data"])
        if data.get("action") == action:
            out.append(data)
    return out


class ShipRepoTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin.git"
        git(["init", "-q", "--bare", "-b", "main", str(self.origin)], self.root)
        self.repo = self.root / "vroxy_web"
        git(["clone", "-q", str(self.origin), str(self.repo)], self.root)
        git(["config", "user.email", "t@example.test"], self.repo)
        git(["config", "user.name", "Test"], self.repo)
        git(["symbolic-ref", "HEAD", "refs/heads/main"], self.repo)
        (self.repo / "README.md").write_text("hello\n")
        (self.repo / "app.rb").write_text("puts 1\n")
        git(["add", "."], self.repo)
        git(["commit", "-qm", "init"], self.repo)
        git(["push", "-q", "-u", "origin", "main"], self.repo)

        self._saved = (fa.CODE_ROOT, fa.PROJECT, fa._run)
        fa.CODE_ROOT, fa.PROJECT = self.root, "vroxy_web"
        self.gh_calls = []
        self.gh_result = (0, "Creating pull request\nhttps://github.com/acme/web/pull/42", "")
        real_run = self._saved[2]

        def run(cmd, cwd):
            if cmd and cmd[0] == "gh":
                self.gh_calls.append(list(cmd))
                return self.gh_result
            return real_run(cmd, cwd)

        fa._run = run
        self.link = fa.CableLink()

    async def asyncTearDown(self):
        fa.CODE_ROOT, fa.PROJECT, fa._run = self._saved
        self.tmp.cleanup()

    def head(self, ref="HEAD"):
        return git(["rev-parse", ref], self.repo)

    def origin_head(self, ref="main"):
        return git(["rev-parse", ref], self.origin)

    def payload(self, policy=None, files=None, **proposal):
        body = {"summary": "Fix the title", "files": files if files is not None else
                [{"path": "app.rb", "content": "puts 2\n"}]}
        body.update(proposal)
        out = {"type": "approve.requested", "chat_id": "ch123456", "message_id": "ms1",
               "feedback_id": "fb123456", "proposal": body}
        if policy is not None:
            out["policy"] = policy
        return out

    def replies(self):
        return frames(self.link, "reply")

    def last_reply(self):
        return self.replies()[-1]


class ShipPolicyObedienceTest(ShipRepoTestCase):
    async def test_ship_commits_onto_the_base_branch_and_pushes_it(self):
        before = self.origin_head()
        await fa.handle_approve(self.link, self.payload(
            {"mode": "ship", "base_ref": "main", "reason": "small change"}))

        self.assertNotEqual(before, self.origin_head())
        self.assertEqual(self.head(), self.origin_head())
        self.assertEqual("puts 2\n", git(["show", "main:app.rb"], self.origin) + "\n")
        self.assertEqual([], self.gh_calls)
        body = self.last_reply()["body"]
        self.assertIn("Shipped", body)
        self.assertIn("`main`", body)
        self.assertIn("(small change)", body)
        self.assertIn(self.head()[:12], body)

    async def test_pr_leaves_the_base_branch_alone_and_opens_a_pull_request(self):
        before = self.origin_head()
        await fa.handle_approve(self.link, self.payload(
            {"mode": "pr", "base_ref": "main", "reason": "touches a migration",
             "branch_prefix": "dispatch/", "pr_draft": True}))

        self.assertEqual(before, self.origin_head(), "a PR must never land on the base branch")
        self.assertEqual(before, self.head("main"))
        self.assertEqual("main", git(["rev-parse", "--abbrev-ref", "HEAD"], self.repo),
                         "the checkout is returned to the base branch")
        self.assertEqual("puts 1\n", (self.repo / "app.rb").read_text())

        self.assertEqual(1, len(self.gh_calls))
        argv = self.gh_calls[0]
        branch = argv[argv.index("--head") + 1]
        self.assertTrue(branch.startswith("dispatch/fix-the-title-"), branch)
        self.assertEqual("main", argv[argv.index("--base") + 1])
        self.assertIn("--draft", argv)
        pr_body = argv[argv.index("--body") + 1]
        self.assertIn("touches a migration", pr_body)
        self.assertIn("fb123456", pr_body)
        self.assertEqual("puts 2\n", git(["show", f"{branch}:app.rb"], self.origin) + "\n")

        reply = self.last_reply()
        self.assertEqual("pull_request", reply["kind"])
        self.assertEqual(42, reply["pull_request"]["number"])
        self.assertEqual(branch, reply["pull_request"]["branch"])
        self.assertEqual("main", reply["pull_request"]["base"])

    async def test_a_pr_is_not_a_draft_unless_the_policy_says_so(self):
        await fa.handle_approve(self.link, self.payload({"mode": "pr", "base_ref": "main"}))
        self.assertNotIn("--draft", self.gh_calls[0])
        self.assertTrue(self.gh_calls[0][self.gh_calls[0].index("--head") + 1]
                        .startswith("feedback/"))

    async def test_the_servers_ship_decision_outranks_the_proposals_own_mode(self):
        await fa.handle_approve(self.link, self.payload(
            {"mode": "ship", "base_ref": "main"}, mode="pull_request"))
        self.assertEqual([], self.gh_calls)
        self.assertEqual(self.head(), self.origin_head())

    async def test_the_servers_pr_decision_outranks_an_inline_proposal(self):
        before = self.origin_head()
        await fa.handle_approve(self.link, self.payload(
            {"mode": "pr", "base_ref": "main"}, mode="inline_ship"))
        self.assertEqual(before, self.origin_head())
        self.assertEqual(1, len(self.gh_calls))

    async def test_a_server_with_no_policy_falls_back_to_the_proposals_mode(self):
        before = self.origin_head()
        await fa.handle_approve(self.link, self.payload(None, mode="pull_request"))
        self.assertEqual(before, self.origin_head())
        self.assertEqual(1, len(self.gh_calls))

    async def test_an_unknown_policy_mode_falls_back_rather_than_guessing_ship(self):
        before = self.origin_head()
        await fa.handle_approve(self.link, self.payload(
            {"mode": "yolo", "base_ref": "main"}, mode="pull_request"))
        self.assertEqual(before, self.origin_head())
        self.assertEqual(1, len(self.gh_calls))

    async def test_mode_none_writes_nothing_and_says_why(self):
        before = self.head()
        await fa.handle_approve(self.link, self.payload(
            {"mode": "none", "reason": "dispatch disabled for vroxy_web"}))
        self.assertEqual(before, self.head())
        self.assertEqual("puts 1\n", (self.repo / "app.rb").read_text())
        self.assertIn("dispatch disabled for vroxy_web", self.last_reply()["body"])
        self.assertEqual("", git(["status", "--porcelain"], self.repo))


class ApproveRefusalTest(ShipRepoTestCase):
    async def test_a_stale_proposal_is_refused_and_nothing_is_written(self):
        stale = self.head()
        (self.repo / "README.md").write_text("moved on\n")
        git(["commit", "-qam", "later"], self.repo)
        after = self.head()

        await fa.handle_approve(self.link, self.payload(
            {"mode": "ship", "base_ref": "main"}, base_sha=stale))

        self.assertEqual(after, self.head())
        self.assertEqual("puts 1\n", (self.repo / "app.rb").read_text())
        self.assertIn(stale[:12], self.last_reply()["body"])
        self.assertIn(after[:12], self.last_reply()["body"])

    async def test_a_proposal_on_the_current_head_applies(self):
        await fa.handle_approve(self.link, self.payload(
            {"mode": "ship", "base_ref": "main"}, base_sha=self.head()))
        self.assertIn("Shipped", self.last_reply()["body"])

    async def test_one_escaping_path_refuses_the_whole_proposal(self):
        files = [{"path": "app.rb", "content": "puts 2\n"},
                 {"path": "../escaped.rb", "content": "pwned\n"}]
        await fa.handle_approve(self.link, self.payload(
            {"mode": "ship", "base_ref": "main"}, files=files))

        self.assertEqual("puts 1\n", (self.repo / "app.rb").read_text(),
                         "a proposal with one bad entry is refused whole, not half-applied")
        self.assertFalse((self.root / "escaped.rb").exists())
        self.assertIn("Nothing was written", self.last_reply()["body"])
        self.assertEqual(self.head(), self.origin_head())

    async def test_a_hook_planted_through_a_proposal_is_refused(self):
        files = [{"path": ".git/hooks/pre-commit", "content": "#!/bin/sh\ntouch /tmp/x\n"}]
        await fa.handle_approve(self.link, self.payload(
            {"mode": "ship", "base_ref": "main"}, files=files))
        self.assertFalse((self.repo / ".git/hooks/pre-commit").exists())
        self.assertIn("Refused", self.last_reply()["body"])

    async def test_no_files_is_a_friendly_no_op(self):
        before = self.head()
        await fa.handle_approve(self.link, self.payload({"mode": "ship"}, files=[]))
        self.assertEqual(before, self.head())
        self.assertIn("nothing to apply", self.last_reply()["body"])

    async def test_entries_without_content_are_skipped_not_fatal(self):
        files = [{"path": "app.rb", "content": "puts 2\n"}, {"path": "ghost.rb"}]
        await fa.handle_approve(self.link, self.payload(
            {"mode": "ship", "base_ref": "main"}, files=files))
        self.assertIn("Shipped", self.last_reply()["body"])
        self.assertEqual(["app.rb"],
                         git(["show", "--name-only", "--format=", "main"], self.origin).split())

    async def test_a_proposal_with_no_usable_entry_commits_nothing(self):
        (self.repo / "README.md").write_text("staged elsewhere\n")
        git(["add", "README.md"], self.repo)
        before = self.origin_head()
        await fa.handle_approve(self.link, self.payload(
            {"mode": "ship", "base_ref": "main"}, files=[{"path": "ghost.rb"}, {"content": "x"}]))
        self.assertEqual(before, self.origin_head())
        self.assertEqual(self.head(), before)
        self.assertIn("nothing to apply", self.last_reply()["body"])

    async def test_no_chat_id_sends_nothing(self):
        payload = self.payload({"mode": "ship", "base_ref": "main"})
        payload.pop("chat_id")
        before = self.head()
        await fa.handle_approve(self.link, payload)
        self.assertEqual([], self.replies())
        self.assertEqual(before, self.head())

    async def test_a_missing_project_is_reported_not_raised(self):
        fa.PROJECT = "not_here"
        await fa.handle_approve(self.link, self.payload({"mode": "ship"}))
        self.assertIn("not found", self.last_reply()["body"])


class ApproveCheckoutSafetyTest(ShipRepoTestCase):
    async def test_a_checkout_on_another_branch_is_switched_before_anything_is_written(self):
        git(["checkout", "-q", "-b", "operator/wip"], self.repo)
        wip = self.head()

        await fa.handle_approve(self.link, self.payload({"mode": "ship", "base_ref": "main"}))

        self.assertIn("Shipped", self.last_reply()["body"])
        self.assertEqual(wip, self.head("operator/wip"), "the other branch is untouched")
        self.assertEqual("puts 2\n", git(["show", "main:app.rb"], self.origin) + "\n")

    async def test_a_dirty_checkout_on_another_branch_gets_nothing_written_into_it(self):
        git(["checkout", "-q", "-b", "operator/wip"], self.repo)
        (self.repo / "README.md").write_text("operator's unsaved work\n")
        before = self.origin_head()

        await fa.handle_approve(self.link, self.payload({"mode": "ship", "base_ref": "main"}))

        self.assertEqual("puts 1\n", (self.repo / "app.rb").read_text(),
                         "the proposal must not be left behind in someone else's checkout")
        self.assertEqual("operator's unsaved work\n", (self.repo / "README.md").read_text())
        self.assertEqual(before, self.origin_head())
        self.assertEqual("operator/wip", git(["rev-parse", "--abbrev-ref", "HEAD"], self.repo))
        self.assertIn("Nothing was written", self.last_reply()["body"])

    async def test_a_ship_does_not_sweep_in_work_someone_else_had_staged(self):
        (self.repo / "README.md").write_text("half-finished, staged by a room run\n")
        git(["add", "README.md"], self.repo)

        await fa.handle_approve(self.link, self.payload({"mode": "ship", "base_ref": "main"}))

        self.assertIn("Shipped", self.last_reply()["body"])
        self.assertEqual("hello", git(["show", "main:README.md"], self.origin),
                         "only the proposal's files may be committed")
        self.assertEqual("puts 2", git(["show", "main:app.rb"], self.origin))
        self.assertEqual("half-finished, staged by a room run\n",
                         (self.repo / "README.md").read_text())

    async def test_a_pr_does_not_sweep_in_work_someone_else_had_staged(self):
        (self.repo / "README.md").write_text("staged elsewhere\n")
        git(["add", "README.md"], self.repo)

        await fa.handle_approve(self.link, self.payload({"mode": "pr", "base_ref": "main"}))

        branch = self.gh_calls[0][self.gh_calls[0].index("--head") + 1]
        self.assertEqual("hello", git(["show", f"{branch}:README.md"], self.origin))
        self.assertEqual("puts 2", git(["show", f"{branch}:app.rb"], self.origin))

    async def test_a_new_file_in_a_proposal_is_committed(self):
        files = [{"path": "app/views/new.erb", "content": "<p>hi</p>\n"}]
        await fa.handle_approve(self.link, self.payload(
            {"mode": "ship", "base_ref": "main"}, files=files))
        self.assertEqual("<p>hi</p>", git(["show", "main:app/views/new.erb"], self.origin))


class GitShipFailureTest(ShipRepoTestCase):
    async def test_a_push_failure_names_the_commit_it_made(self):
        git(["remote", "set-url", "origin", str(self.root / "gone.git")], self.repo)
        await fa.handle_approve(self.link, self.payload({"mode": "ship", "base_ref": "main"}))
        body = self.last_reply()["body"]
        self.assertIn("Push failed after commit", body)
        self.assertIn(self.head()[:12], body)

    async def test_a_failed_pr_create_still_returns_the_checkout_to_base(self):
        self.gh_result = (1, "", "gh: not logged in")
        await fa.handle_approve(self.link, self.payload({"mode": "pr", "base_ref": "main"}))
        self.assertIn("gh pr create", self.last_reply()["body"])
        self.assertIn("not logged in", self.last_reply()["body"])
        self.assertEqual("main", git(["rev-parse", "--abbrev-ref", "HEAD"], self.repo))
        self.assertEqual("", git(["status", "--porcelain"], self.repo))

    async def test_a_failed_pr_push_still_returns_the_checkout_to_base(self):
        git(["remote", "set-url", "origin", str(self.root / "gone.git")], self.repo)
        await fa.handle_approve(self.link, self.payload({"mode": "pr", "base_ref": "main"}))
        self.assertIn("Push failed", self.last_reply()["body"])
        self.assertEqual("main", git(["rev-parse", "--abbrev-ref", "HEAD"], self.repo))
        self.assertEqual([], self.gh_calls)

    async def test_an_identical_proposal_reports_a_failed_commit(self):
        files = [{"path": "app.rb", "content": "puts 1\n"}]
        before = self.origin_head()
        await fa.handle_approve(self.link, self.payload(
            {"mode": "ship", "base_ref": "main"}, files=files))
        self.assertIn("Commit failed", self.last_reply()["body"])
        self.assertEqual(before, self.origin_head())

    async def test_a_version_bump_rides_the_same_commit(self):
        (self.repo / "version_bump.sh").write_text(
            "mkdir -p config\necho 'VERSION = 2' > config/application.rb\n"
            "echo '## 2' > CHANGELOG.md\n")
        git(["add", "version_bump.sh"], self.repo)
        git(["commit", "-qm", "bump script"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)

        await fa.handle_approve(self.link, self.payload({"mode": "ship", "base_ref": "main"}))

        changed = git(["show", "--name-only", "--format=", "main"], self.origin).split()
        self.assertEqual(sorted(["CHANGELOG.md", "app.rb", "config/application.rb"]),
                         sorted(changed))

    async def test_a_failed_version_bump_still_ships_the_change_without_it(self):
        (self.repo / "version_bump.sh").write_text("echo nope >&2\nexit 3\n")
        git(["add", "version_bump.sh"], self.repo)
        git(["commit", "-qm", "bump script"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)

        await fa.handle_approve(self.link, self.payload({"mode": "ship", "base_ref": "main"}))

        changed = git(["show", "--name-only", "--format=", "main"], self.origin).split()
        self.assertEqual(["app.rb"], changed)


class PullRequestUrlTest(ShipRepoTestCase):
    async def test_a_url_without_a_pull_number_still_reports_the_url(self):
        self.gh_result = (0, "https://example.test/somewhere", "")
        await fa.handle_approve(self.link, self.payload({"mode": "pr", "base_ref": "main"}))
        reply = self.last_reply()
        self.assertIsNone(reply["pull_request"]["number"])
        self.assertEqual("https://example.test/somewhere", reply["pull_request"]["url"])

    async def test_a_summary_with_no_usable_characters_still_names_a_branch(self):
        payload = self.payload({"mode": "pr", "base_ref": "main"})
        payload["proposal"]["summary"] = "✨✨✨"
        await fa.handle_approve(self.link, payload)
        branch = self.gh_calls[0][self.gh_calls[0].index("--head") + 1]
        self.assertRegex(branch, r"\Afeedback/ui-[0-9a-f]{6}\Z")


if __name__ == "__main__":
    unittest.main()
