import json
import os
import stat
import tempfile
import unittest
import urllib.error
from io import BytesIO, StringIO
from unittest import mock

from vroxy_cli import config
from vroxy_cli.cli import main
from vroxy_cli.client import Client, VroxyError


def _response(payload):
    body = BytesIO(json.dumps(payload).encode())
    body.__enter__ = lambda self=body: self
    body.__exit__ = lambda *a, **k: False
    return body


def _http_error(status, payload):
    return urllib.error.HTTPError(
        "http://x", status, "err", {}, BytesIO(json.dumps(payload).encode())
    )


class CredentialsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        patcher = mock.patch.object(config, "CONFIG_DIR", __import__("pathlib").Path(self.dir))
        patcher.start()
        self.addCleanup(patcher.stop)
        creds = __import__("pathlib").Path(self.dir) / "credentials.json"
        patcher2 = mock.patch.object(config, "CREDENTIALS", creds)
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def test_saved_token_is_not_world_readable(self):
        path = config.save("https://vroxy.ai", "secret-token", email="a@b.co")

        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600, f"credentials were {oct(mode)}")

    def test_token_is_not_returned_for_a_different_host(self):
        config.save("https://vroxy.ai", "secret-token")

        self.assertEqual(config.token_for("https://vroxy.ai"), "secret-token")
        self.assertIsNone(
            config.token_for("https://staging.example.test"),
            "a token minted for one host must not be sent to another",
        )

    def test_trailing_slash_is_not_a_different_host(self):
        config.save("https://vroxy.ai", "secret-token")

        self.assertEqual(config.token_for("https://vroxy.ai/"), "secret-token")


class ClientTest(unittest.TestCase):
    def test_unauthenticated_call_says_what_to_do(self):
        with self.assertRaises(VroxyError) as ctx:
            Client(host="https://x.test", token=None).me()

        self.assertIn("vroxy login", str(ctx.exception))

    def test_a_401_is_reported_as_a_stale_token_not_a_raw_code(self):
        client = Client(host="https://x.test", token="old")
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(401, {})):
            with self.assertRaises(VroxyError) as ctx:
                client.me()

        self.assertIn("vroxy login", str(ctx.exception))
        self.assertEqual(ctx.exception.status, 401)

    def test_an_api_error_message_survives(self):
        client = Client(host="https://x.test", token="t")
        payload = {"error": "forbidden", "code": "email_unverified"}
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(403, payload)):
            with self.assertRaises(VroxyError) as ctx:
                client.me()

        self.assertEqual(str(ctx.exception), "forbidden")
        self.assertEqual(ctx.exception.code, "email_unverified")

    def test_a_non_json_body_does_not_crash_the_parser(self):
        client = Client(host="https://x.test", token="t")
        err = urllib.error.HTTPError("http://x", 502, "bad", {}, BytesIO(b"<html>nope"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(VroxyError) as ctx:
                client.me()

        self.assertIn("502", str(ctx.exception))

    def test_bearer_header_is_sent(self):
        from vroxy_cli.client import USER_AGENT

        client = Client(host="https://x.test", token="tok123")
        seen = {}

        def capture(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            seen["ua"] = req.get_header("User-agent") or req.get_header("User-Agent")
            return _response({"user": {}})

        with mock.patch("urllib.request.urlopen", side_effect=capture):
            client.me()

        self.assertEqual(seen["auth"], "Bearer tok123")
        self.assertEqual(seen["ua"], USER_AGENT)


class CliTest(unittest.TestCase):
    def test_an_error_exits_nonzero_rather_than_raising(self):
        with mock.patch("vroxy_cli.cli._client") as fake:
            fake.return_value.workspaces.side_effect = VroxyError("nope")
            code = main(["workspaces"])

        self.assertEqual(code, 1)

    def test_json_output_is_machine_readable(self):
        rows = [{"hashid": "ws1", "name": "Acme", "role": "owner"}]
        with mock.patch("vroxy_cli.cli._client") as fake:
            fake.return_value.workspaces.return_value = rows
            with mock.patch("builtins.print") as printed:
                code = main(["--json", "workspaces"])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(printed.call_args[0][0]), rows)


class ManagementCommandsTest(unittest.TestCase):
    def run_cli(self, argv):
        with mock.patch("vroxy_cli.cli._client") as fake:
            with mock.patch("builtins.print"):
                code = main(argv)
        return code, fake.return_value

    def test_docs_create_reads_stdin_when_the_body_is_a_dash(self):
        with mock.patch("sys.stdin", StringIO("# Refunds\n14 days.")):
            code, client = self.run_cli(["docs", "ws1", "create", "Refunds", "--body", "-"])

        self.assertEqual(code, 0)
        client.create_doc.assert_called_once_with("ws1", title="Refunds", body_md="# Refunds\n14 days.")

    def test_docs_create_without_a_body_refuses_rather_than_posting_an_empty_one(self):
        code, client = self.run_cli(["docs", "ws1", "create", "Refunds"])

        self.assertEqual(code, 1)
        client.create_doc.assert_not_called()

    def test_docs_edit_with_nothing_to_change_refuses(self):
        code, client = self.run_cli(["docs", "ws1", "edit", "d1"])

        self.assertEqual(code, 1)
        client.update_doc.assert_not_called()

    def test_unpublish_is_the_publish_call_with_published_false(self):
        _, client = self.run_cli(["docs", "ws1", "unpublish", "d1"])

        client.publish_doc.assert_called_once_with("ws1", "d1", published=False)

    def test_member_management_is_not_a_cli_command(self):
        for argv in (
            ["members", "ws1", "invite", "a@b.test", "admin"],
            ["members", "ws1", "role", "7", "admin"],
            ["members", "ws1", "remove", "7"],
            ["members", "ws1", "revoke", "inv1"],
        ):
            with mock.patch("sys.stderr", StringIO()):
                with self.assertRaises(SystemExit, msg=argv):
                    main(argv)
        for name in ("invite_member", "set_member_role", "remove_member", "revoke_invitation"):
            self.assertFalse(hasattr(Client, name), name)

    def test_members_list_still_works(self):
        _, client = self.run_cli(["members", "ws1", "list"])

        client.members.assert_called_once_with("ws1")

    def test_enable_and_disable_are_explicit_not_a_flip(self):
        _, client = self.run_cli(["tools", "ws1", "disable", "t1"])

        client.toggle_tool.assert_called_once_with("ws1", "t1", enabled=False)

    def test_tool_params_parse_into_name_and_description(self):
        _, client = self.run_cli([
            "tools", "ws1", "create", "search_listings",
            "--label", "Search", "--description", "Find listings.",
            "--url", "https://x.test/s?q={query}",
            "--param", "query=what to search for",
        ])

        self.assertEqual(
            client.create_tool.call_args.kwargs["params"],
            [{"name": "query", "description": "what to search for"}],
        )

    def test_a_param_with_no_name_is_refused(self):
        code, client = self.run_cli([
            "tools", "ws1", "create", "search_listings",
            "--label", "Search", "--description", "Find listings.",
            "--url", "https://x.test/s", "--param", "=orphaned",
        ])

        self.assertEqual(code, 1)
        client.create_tool.assert_not_called()


class ReadCommandsTest(unittest.TestCase):
    def run_cli(self, argv, **returns):
        with mock.patch("vroxy_cli.cli._client") as fake:
            for name, value in returns.items():
                getattr(fake.return_value, name).return_value = value
            with mock.patch("builtins.print") as printed:
                code = main(argv)
        return code, fake.return_value, printed

    def test_errors_list_passes_source_and_page(self):
        code, client, _ = self.run_cli(
            ["errors", "ws1", "list", "--source", "js", "--page", "2"], errors={"errors": []}
        )

        self.assertEqual(code, 0)
        client.errors.assert_called_once_with("ws1", source="js", page=2)

    def test_errors_show_asks_for_the_fingerprint(self):
        code, client, printed = self.run_cli(
            ["errors", "ws1", "show", "abc123"],
            error={"error": {"error_class": "TypeError", "backtrace": ["a.js:1"]}, "occurrences": []},
        )

        self.assertEqual(code, 0)
        client.error.assert_called_once_with("ws1", "abc123", page=None)
        self.assertIn("TypeError", printed.call_args_list[0][0][0])

    def test_usage_prints_unlimited_for_a_missing_limit(self):
        payload = {
            "plan": {"name": "Free", "price_label": "$0"},
            "enforced": True,
            "metrics": [{"metric": "docs", "label": "Documents", "used": 3, "limit": None}],
            "features": [],
        }
        code, client, printed = self.run_cli(["usage", "ws1"], usage=payload)

        self.assertEqual(code, 0)
        client.usage.assert_called_once_with("ws1")
        lines = [c[0][0] for c in printed.call_args_list]
        self.assertIn("  Documents: 3 / unlimited", lines)

    def test_visitors_list_maps_flags_to_the_api(self):
        code, client, _ = self.run_cli(
            ["visitors", "ws1", "list", "--identity", "identified", "--active"],
            visitors={"visitors": []},
        )

        self.assertEqual(code, 0)
        client.visitors.assert_called_once_with("ws1", identity="identified", active=True, page=None)

    def test_visitors_identity_outside_the_choices_never_reaches_the_server(self):
        with self.assertRaises(SystemExit):
            main(["visitors", "ws1", "list", "--identity", "everyone"])

    def test_visitors_show(self):
        _, client, _ = self.run_cli(["visitors", "ws1", "show", "vst1"], visitor={"visitor": {}})

        client.visitor.assert_called_once_with("ws1", "vst1")

    def test_targets_prints_repo_policy_and_agent(self):
        payload = {"targets": [{
            "name": "vroxy_web", "online": True,
            "repo": {"full_name": "acme/web"}, "agent": {"name": "Claude Code"},
            "effective": {"policy": "always_pr", "base_ref": "master"},
        }]}
        code, client, printed = self.run_cli(["targets", "ws1"], targets=payload)

        self.assertEqual(code, 0)
        client.targets.assert_called_once_with("ws1", page=None)
        line = printed.call_args_list[0][0][0]
        for part in ("vroxy_web", "acme/web", "master", "always_pr", "Claude Code", "online"):
            self.assertIn(part, line)

    def test_json_flag_returns_the_raw_payload(self):
        payload = {"targets": [], "meta": {"total_pages": 1}}
        _, _, printed = self.run_cli(["--json", "targets", "ws1"], targets=payload)

        self.assertEqual(json.loads(printed.call_args[0][0]), payload)


class SessionCommandsTest(unittest.TestCase):
    def setUp(self):
        self.dir = __import__("pathlib").Path(tempfile.mkdtemp())
        for name, value in (("CONFIG_DIR", self.dir), ("CREDENTIALS", self.dir / "credentials.json")):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"VROXY_PASSWORD": "pw"})
        env.start()
        self.addCleanup(env.stop)

    def route(self, responses):
        calls = []

        def fake(req, timeout=None):
            path = req.full_url.split("/api/mobile/v1", 1)[1]
            calls.append((path, req.headers.get("Authorization")))
            outcome = responses[path]
            if isinstance(outcome, Exception):
                raise outcome
            return _response(outcome)

        return calls, mock.patch("urllib.request.urlopen", side_effect=fake)

    def test_logout_revokes_the_token_before_forgetting_it(self):
        config.save("https://x.test", "old-token")
        calls, patched = self.route({"/logout": {"ok": True}})
        with patched, mock.patch("builtins.print"):
            code = main(["logout"])

        self.assertEqual(code, 0)
        self.assertEqual(calls, [("/logout", "Bearer old-token")])
        self.assertFalse(config.CREDENTIALS.exists())

    def test_logout_still_forgets_the_token_when_the_server_is_unreachable_and_says_so(self):
        config.save("https://x.test", "old-token")
        _, patched = self.route({"/logout": urllib.error.URLError("down")})
        err = StringIO()
        with patched, mock.patch("sys.stdout", StringIO()), mock.patch("sys.stderr", err):
            code = main(["logout"])

        self.assertEqual(code, 0)
        self.assertFalse(config.CREDENTIALS.exists())
        self.assertIn("could not be revoked", err.getvalue())

    def test_logout_treats_an_already_revoked_token_as_revoked(self):
        config.save("https://x.test", "old-token")
        _, patched = self.route({"/logout": _http_error(401, {})})
        out, err = StringIO(), StringIO()
        with patched, mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            main(["logout"])

        self.assertIn("the token is revoked", out.getvalue())
        self.assertEqual("", err.getvalue())

    def test_logout_with_nothing_saved_calls_nothing(self):
        calls, patched = self.route({})
        with patched, mock.patch("builtins.print"):
            self.assertEqual(main(["logout"]), 0)
        self.assertEqual(calls, [])

    def test_login_revokes_the_previously_saved_token(self):
        config.save("https://x.test", "old-token")
        calls, patched = self.route({
            "/login": {"token": "new-token", "user": {"email": "a@b.co"}},
            "/logout": {"ok": True},
        })
        with patched, mock.patch("builtins.print"):
            code = main(["--host", "https://x.test", "login", "--email", "a@b.co"])

        self.assertEqual(code, 0)
        self.assertEqual(calls, [("/login", None), ("/logout", "Bearer old-token")])
        self.assertEqual(config.token_for("https://x.test"), "new-token")

    def test_a_failed_login_leaves_the_previous_token_alone(self):
        config.save("https://x.test", "old-token")
        calls, patched = self.route({"/login": _http_error(401, {"error": "Invalid email or password"})})
        with patched, mock.patch("sys.stderr", StringIO()):
            code = main(["--host", "https://x.test", "login", "--email", "a@b.co"])

        self.assertEqual(code, 1)
        self.assertEqual([path for path, _ in calls], ["/login"])
        self.assertEqual(config.token_for("https://x.test"), "old-token")

    def test_login_still_succeeds_when_the_old_token_cannot_be_revoked(self):
        config.save("https://x.test", "old-token")
        _, patched = self.route({
            "/login": {"token": "new-token", "user": {"email": "a@b.co"}},
            "/logout": urllib.error.URLError("down"),
        })
        err = StringIO()
        with patched, mock.patch("sys.stdout", StringIO()), mock.patch("sys.stderr", err):
            code = main(["--host", "https://x.test", "login", "--email", "a@b.co"])

        self.assertEqual(code, 0)
        self.assertEqual(config.token_for("https://x.test"), "new-token")
        self.assertIn("Couldn't revoke the previous token", err.getvalue())


class ReadClientTest(unittest.TestCase):
    def capture(self, call):
        seen = {}

        def fake(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data) if req.data else None
            return _response({})

        with mock.patch("urllib.request.urlopen", side_effect=fake):
            call(Client(host="https://x.test", token="t"))
        return seen

    def test_login_asks_for_an_agent_token_not_a_phone_one(self):
        seen = self.capture(lambda c: c.login("a@b.co", "pw", device="laptop"))

        self.assertEqual(seen["url"], "https://x.test/api/mobile/v1/login")
        self.assertEqual(seen["body"]["client"], "cli")
        self.assertEqual(seen["body"]["device_model"], "laptop")

    def test_read_endpoints_hit_the_workspace_paths(self):
        cases = [
            (lambda c: c.errors("ws1", source="js", page=2), "/workspaces/ws1/errors?source=js&page=2"),
            (lambda c: c.error("ws1", "a/b"), "/workspaces/ws1/errors/a%2Fb"),
            (lambda c: c.usage("ws1"), "/workspaces/ws1/usage"),
            (lambda c: c.visitors("ws1", active=True), "/workspaces/ws1/visitors?active=yes"),
            (lambda c: c.visitor("ws1", "vst1"), "/workspaces/ws1/visitors/vst1"),
            (lambda c: c.targets("ws1"), "/workspaces/ws1/dispatch_targets"),
        ]
        for call, path in cases:
            self.assertEqual(self.capture(call)["url"], f"https://x.test/api/mobile/v1{path}")


if __name__ == "__main__":
    unittest.main()
