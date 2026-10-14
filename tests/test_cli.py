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


class _HeaderResponse(BytesIO):
    def __init__(self, payload, headers):
        super().__init__(json.dumps(payload).encode())
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class VersionHeaderTest(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        self.dir = Path(tempfile.mkdtemp())
        for name, value in (("CONFIG_DIR", self.dir),
                            ("CREDENTIALS", self.dir / "credentials.json")):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.seen = []

    def serve(self, latest):
        def fake(req, timeout=None):
            self.seen.append(req)
            headers = {"X-Vroxy-Client-Latest": latest} if latest else {}
            return _HeaderResponse({"workspaces": []}, headers)
        return mock.patch("urllib.request.urlopen", side_effect=fake)

    def run_cli(self, latest, now=None):
        out, err = StringIO(), StringIO()
        config.save("https://x.test", "tok")
        clock = mock.patch("vroxy_cli.client.time.time", return_value=now or 1_000_000.0)
        with self.serve(latest), clock, mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            main(["--json", "workspaces"])
        return out.getvalue(), err.getvalue()

    def test_every_request_names_the_client_and_its_version(self):
        with self.serve(None):
            Client(host="https://x.test", token="t").workspaces()
        from vroxy_cli.version import VERSION
        self.assertEqual(self.seen[0].get_header("X-vroxy-client"), f"vroxy-cli/{VERSION}")

    def test_the_header_rides_unauthenticated_calls_too(self):
        with self.serve(None):
            Client(host="https://x.test").login("a@b.co", "pw")
        self.assertTrue(self.seen[0].get_header("X-vroxy-client").startswith("vroxy-cli/"))

    def test_a_newer_release_is_announced_once_on_stderr_never_stdout(self):
        out, err = self.run_cli("99.0.0")

        self.assertIn("vroxy 99.0.0 is available (you have", err)
        self.assertIn("update the vroxy_dispatch checkout", err)
        self.assertEqual(1, len(err.strip().splitlines()))
        self.assertNotIn("available", out)
        json.loads(out)

        _, again = self.run_cli("99.0.0", now=1_000_000.0 + 3600)
        self.assertEqual("", again, "once per 24h, not once per command")

        _, next_day = self.run_cli("99.0.0", now=1_000_000.0 + 24 * 3600 + 1)
        self.assertIn("99.0.0 is available", next_day)

    def test_the_remembered_notice_is_not_world_readable(self):
        self.run_cli("99.0.0")
        mode = stat.S_IMODE(os.stat(config.notice_path()).st_mode)
        self.assertEqual(mode, 0o600, f"notice file was {oct(mode)}")

    def test_same_or_older_or_garbage_says_nothing(self):
        from vroxy_cli.version import VERSION
        for latest in (VERSION, "0.0.1", "not-a-version", "1.2", ""):
            _, err = self.run_cli(latest)
            self.assertEqual("", err, latest)
        self.assertFalse(config.notice_path().exists())

    def test_a_malformed_header_is_ignored_not_a_crash(self):
        for latest in ("1.2.\u00b2", "\u0661.\u0662.\u0663", "9" * 5000 + ".0.0x", "1..2", " "):
            out, err = self.run_cli(latest)
            self.assertEqual("", err, repr(latest[:20]))
            json.loads(out)

    def test_a_failure_writing_the_notice_never_breaks_the_command(self):
        with mock.patch.object(config, "save_notice", side_effect=RuntimeError("disk")):
            out, err = self.run_cli("99.0.0")
        json.loads(out)
        self.assertNotIn("Traceback", err)

    def test_an_error_response_can_still_carry_the_notice(self):
        headers = {"X-Vroxy-Client-Latest": "99.0.0"}
        error = urllib.error.HTTPError("http://x", 403, "err", headers,
                                       BytesIO(json.dumps({"error": "nope"}).encode()))
        err = StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=error), mock.patch("sys.stderr", err):
            with self.assertRaises(VroxyError):
                Client(host="https://x.test", token="t").me()
        self.assertIn("99.0.0 is available", err.getvalue())



class _IsolatedConfig(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        self.dir = Path(tempfile.mkdtemp())
        for name, value in (("CONFIG_DIR", self.dir),
                            ("CREDENTIALS", self.dir / "credentials.json")):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in ("VROXY_HOST", "VROXY_TOKEN", "VROXY_PASSWORD"):
            os.environ.pop(key, None)
        self.requests = []

    def serve(self, payload=None, error=None):
        def fake(req, timeout=None):
            self.requests.append(req)
            if error is not None:
                raise error
            return _response(payload if payload is not None else {})
        return mock.patch("urllib.request.urlopen", side_effect=fake)

    def run_main(self, argv, payload=None, error=None, stdin=None):
        out, err = StringIO(), StringIO()
        patches = [self.serve(payload, error), mock.patch("sys.stdout", out),
                   mock.patch("sys.stderr", err)]
        if stdin is not None:
            patches.append(mock.patch("sys.stdin", StringIO(stdin)))
        for p in patches:
            p.start()
        try:
            code = main(argv)
        finally:
            for p in reversed(patches):
                p.stop()
        return code, out.getvalue(), err.getvalue()


class AgentTokenRefusedTest(_IsolatedConfig):
    REFUSED = {"error": "Agent tokens can't use this endpoint", "code": "agent_token_refused"}

    def test_a_refused_agent_token_says_so_on_stderr_and_exits_nonzero(self):
        config.save("https://x.test", "agent-tok")
        code, out, err = self.run_main(["members", "ws1", "list"],
                                       error=_http_error(403, self.REFUSED))
        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertEqual("vroxy: Agent tokens can't use this endpoint\n", err)

    def test_json_mode_keeps_stdout_empty_on_a_refusal(self):
        config.save("https://x.test", "agent-tok")
        code, out, err = self.run_main(["--json", "members", "ws1", "list"],
                                       error=_http_error(403, self.REFUSED))
        self.assertEqual(1, code)
        self.assertEqual("", out, "a script parsing stdout must never read an error as data")
        self.assertIn("Agent tokens can't use this endpoint", err)

    def test_the_refusal_code_survives_for_callers_of_the_client(self):
        with self.serve(error=_http_error(403, self.REFUSED)):
            with self.assertRaises(VroxyError) as cm:
                Client(host="https://x.test", token="t").members("ws1")
        self.assertEqual(403, cm.exception.status)
        self.assertEqual("agent_token_refused", cm.exception.code)

    def test_a_refused_logout_is_not_mistaken_for_a_revoked_token(self):
        config.save("https://x.test", "agent-tok")
        code, out, err = self.run_main(["logout"], error=_http_error(403, self.REFUSED))
        self.assertEqual(0, code)
        self.assertFalse(config.CREDENTIALS.exists())
        self.assertIn("could not be revoked", err)
        self.assertNotIn("the token is revoked", out)


class RequestShapeTest(unittest.TestCase):
    def capture(self, call):
        seen = {}

        def fake(req, timeout=None):
            seen["method"] = req.get_method()
            seen["path"] = req.full_url.split("/api/mobile/v1", 1)[1]
            seen["body"] = json.loads(req.data) if req.data else None
            seen["auth"] = req.get_header("Authorization")
            seen["timeout"] = timeout
            return _response({})

        with mock.patch("urllib.request.urlopen", side_effect=fake):
            call(Client(host="https://x.test/", token="t"))
        return seen

    def test_every_client_call_hits_the_documented_route(self):
        cases = [
            (lambda c: c.logout(), "POST", "/logout", None),
            (lambda c: c.me(), "GET", "/me", None),
            (lambda c: c.workspaces(), "GET", "/workspaces", None),
            (lambda c: c.rooms("ws1"), "GET", "/workspaces/ws1/rooms", None),
            (lambda c: c.messages("ws1", "rm1", limit="5"), "GET",
             "/workspaces/ws1/rooms/rm1/messages?limit=5", None),
            (lambda c: c.post_message("ws1", "rm1", "hi"), "POST",
             "/workspaces/ws1/rooms/rm1/messages", {"body": "hi"}),
            (lambda c: c.dispatch_status("ws1"), "GET", "/workspaces/ws1/dispatch_status", None),
            (lambda c: c.docs("ws1"), "GET", "/workspaces/ws1/docs", None),
            (lambda c: c.docs("ws1", status="draft", q="refund policy"), "GET",
             "/workspaces/ws1/docs?status=draft&q=refund+policy", None),
            (lambda c: c.doc("ws1", "d1"), "GET", "/workspaces/ws1/docs/d1", None),
            (lambda c: c.create_doc("ws1", title="T", body_md="B"), "POST",
             "/workspaces/ws1/docs", {"doc": {"title": "T", "body_md": "B"}}),
            (lambda c: c.update_doc("ws1", "d1", title=None, body_md="B"), "PATCH",
             "/workspaces/ws1/docs/d1", {"doc": {"body_md": "B"}}),
            (lambda c: c.publish_doc("ws1", "d1"), "POST", "/workspaces/ws1/docs/d1/publish", None),
            (lambda c: c.publish_doc("ws1", "d1", published=False), "POST",
             "/workspaces/ws1/docs/d1/unpublish", None),
            (lambda c: c.delete_doc("ws1", "d1"), "DELETE", "/workspaces/ws1/docs/d1", None),
            (lambda c: c.members("ws1"), "GET", "/workspaces/ws1/members", None),
            (lambda c: c.tools("ws1"), "GET", "/workspaces/ws1/tools", None),
            (lambda c: c.tool("ws1", "t1"), "GET", "/workspaces/ws1/tools/t1", None),
            (lambda c: c.create_tool("ws1", name="n", kind="link", follow_origin=None), "POST",
             "/workspaces/ws1/tools", {"tool": {"name": "n", "kind": "link"}}),
            (lambda c: c.update_tool("ws1", "t1", label="L"), "PATCH",
             "/workspaces/ws1/tools/t1", {"tool": {"label": "L"}}),
            (lambda c: c.toggle_tool("ws1", "t1"), "POST", "/workspaces/ws1/tools/t1/toggle", None),
            (lambda c: c.toggle_tool("ws1", "t1", enabled=True), "POST",
             "/workspaces/ws1/tools/t1/toggle", {"enabled": True}),
            (lambda c: c.toggle_tool("ws1", "t1", enabled=False), "POST",
             "/workspaces/ws1/tools/t1/toggle", {"enabled": False}),
            (lambda c: c.delete_tool("ws1", "t1"), "DELETE", "/workspaces/ws1/tools/t1", None),
            (lambda c: c.search_rooms("ws1", "a&b=c #d"), "GET",
             "/workspaces/ws1/rooms/search?q=a%26b%3Dc+%23d&limit=20", None),
            (lambda c: c.chats("ws1"), "GET", "/workspaces/ws1/chats?filter=open", None),
            (lambda c: c.chats("ws1", filter="all", q="x", page=2), "GET",
             "/workspaces/ws1/chats?filter=all&q=x&page=2", None),
            (lambda c: c.chat("ws1", "c1"), "GET", "/workspaces/ws1/chats/c1", None),
            (lambda c: c.error("ws1", "fp 1", page=3), "GET",
             "/workspaces/ws1/errors/fp%201?page=3", None),
            (lambda c: c.visitors("ws1", identity="anonymous"), "GET",
             "/workspaces/ws1/visitors?identity=anonymous", None),
            (lambda c: c.targets("ws1", page=2), "GET",
             "/workspaces/ws1/dispatch_targets?page=2", None),
        ]
        for call, method, path, body in cases:
            with self.subTest(path=path, method=method):
                seen = self.capture(call)
                self.assertEqual(method, seen["method"])
                self.assertEqual(path, seen["path"])
                self.assertEqual(body, seen["body"])
                self.assertEqual("Bearer t", seen["auth"])

    def test_no_call_waits_longer_than_the_ninety_second_ceiling(self):
        self.assertLessEqual(self.capture(lambda c: c.me())["timeout"], 90)

    def test_a_non_numeric_limit_is_refused_before_any_request(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(ValueError):
                Client(host="https://x.test", token="t").messages("ws1", "rm1", limit="5; drop")
        urlopen.assert_not_called()

    def test_login_is_the_one_call_that_carries_no_bearer(self):
        seen = self.capture(lambda c: c.login("a@b.co", "pw"))
        self.assertIsNone(seen["auth"])
        self.assertEqual({"email": "a@b.co", "password": "pw", "client": "cli"}, seen["body"])

    def test_an_unreachable_host_is_named_in_the_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(VroxyError) as cm:
                Client(host="https://down.test", token="t").me()
        self.assertIn("Could not reach https://down.test", str(cm.exception))
        self.assertIn("refused", str(cm.exception))

    def test_a_json_array_body_is_wrapped_not_crashed_on(self):
        from vroxy_cli.client import _safe_json
        self.assertEqual({"data": [1, 2]}, _safe_json("[1, 2]"))
        self.assertEqual({}, _safe_json(""))
        self.assertEqual({}, _safe_json("<html>"))


class HostAndTokenResolutionTest(_IsolatedConfig):
    def test_the_saved_host_and_token_are_used_by_default(self):
        config.save("https://saved.test", "saved-tok")
        self.run_main(["workspaces"], payload={"workspaces": []})
        self.assertTrue(self.requests[0].full_url.startswith("https://saved.test/api/mobile/v1/"))
        self.assertEqual("Bearer saved-tok", self.requests[0].get_header("Authorization"))

    def test_a_token_is_never_sent_to_a_host_it_was_not_minted_for(self):
        config.save("https://saved.test", "saved-tok")
        code, out, err = self.run_main(["--host", "https://other.test", "workspaces"])
        self.assertEqual(1, code)
        self.assertEqual([], self.requests, "no request at all, let alone one with the token")
        self.assertIn("vroxy login", err)

    def test_vroxy_host_env_is_honoured_the_same_way(self):
        config.save("https://saved.test", "saved-tok")
        os.environ["VROXY_HOST"] = "https://other.test"
        code, _, _ = self.run_main(["workspaces"])
        self.assertEqual(1, code)
        self.assertEqual([], self.requests)

    def test_an_explicit_vroxy_token_wins_over_the_saved_one(self):
        config.save("https://saved.test", "saved-tok")
        os.environ["VROXY_TOKEN"] = "env-tok"
        self.run_main(["workspaces"], payload={"workspaces": []})
        self.assertEqual("Bearer env-tok", self.requests[0].get_header("Authorization"))

    def test_nothing_saved_and_nothing_in_the_env_asks_you_to_log_in(self):
        code, out, err = self.run_main(["whoami"])
        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertEqual("vroxy: Not signed in. Run: vroxy login\n", err)


class ConfigFileModeTest(_IsolatedConfig):
    def test_an_existing_world_readable_credentials_file_is_tightened(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        config.CREDENTIALS.write_text("{}")
        os.chmod(config.CREDENTIALS, 0o644)
        config.save("https://x.test", "tok")
        self.assertEqual(0o600, stat.S_IMODE(os.stat(config.CREDENTIALS).st_mode))

    def test_an_existing_world_readable_notice_file_is_tightened(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        config.notice_path().write_text("{}")
        os.chmod(config.notice_path(), 0o644)
        config.save_notice({"shown_at": 1})
        self.assertEqual(0o600, stat.S_IMODE(os.stat(config.notice_path()).st_mode))

    def test_a_corrupt_credentials_file_reads_as_signed_out(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        config.CREDENTIALS.write_text("{not json")
        self.assertEqual({}, config.load())
        self.assertIsNone(config.token_for("https://x.test"))

    def test_a_notice_file_that_is_not_an_object_is_ignored(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        config.notice_path().write_text("[1, 2]")
        self.assertEqual({}, config.load_notice())

    def test_clear_with_nothing_saved_is_not_an_error(self):
        self.assertFalse(config.clear())


class LoginSafetyTest(_IsolatedConfig):
    def test_a_password_flag_does_not_exist(self):
        with mock.patch("sys.stderr", StringIO()):
            with self.assertRaises(SystemExit):
                main(["login", "--email", "a@b.co", "--password", "hunter2"])

    def test_the_password_is_read_from_the_prompt_not_argv(self):
        with mock.patch("getpass.getpass", return_value="pw-from-prompt") as prompt:
            self.run_main(["--host", "https://x.test", "login", "--email", "a@b.co"],
                          payload={"token": "t", "user": {"email": "a@b.co"}})
        prompt.assert_called_once()
        self.assertEqual("pw-from-prompt", json.loads(self.requests[0].data)["password"])

    def test_a_login_that_returns_no_token_keeps_the_old_one_and_revokes_nothing(self):
        config.save("https://x.test", "old-tok")
        os.environ["VROXY_PASSWORD"] = "pw"
        code, _, err = self.run_main(["--host", "https://x.test", "login", "--email", "a@b.co"],
                                     payload={"user": {"email": "a@b.co"}})
        self.assertEqual(1, code)
        self.assertIn("no token", err)
        self.assertEqual(1, len(self.requests))
        self.assertEqual("old-tok", config.token_for("https://x.test"))

    def test_logging_in_again_with_the_same_token_does_not_revoke_it(self):
        config.save("https://x.test", "same-tok")
        os.environ["VROXY_PASSWORD"] = "pw"
        code, _, _ = self.run_main(["--host", "https://x.test", "login", "--email", "a@b.co"],
                                   payload={"token": "same-tok", "user": {"email": "a@b.co"}})
        self.assertEqual(0, code)
        self.assertEqual(["/login"], [r.full_url.split("/api/mobile/v1")[1] for r in self.requests])

    def test_the_old_token_is_revoked_on_the_host_that_minted_it(self):
        config.save("https://old.test", "old-tok")
        os.environ["VROXY_PASSWORD"] = "pw"
        self.run_main(["--host", "https://new.test", "login", "--email", "a@b.co"],
                      payload={"token": "new-tok", "user": {"email": "a@b.co"}})
        revoke = self.requests[1]
        self.assertTrue(revoke.full_url.startswith("https://old.test/"))
        self.assertEqual("Bearer old-tok", revoke.get_header("Authorization"))
        self.assertEqual("new-tok", config.token_for("https://new.test"))

    def test_the_saved_file_never_holds_the_password(self):
        os.environ["VROXY_PASSWORD"] = "hunter2-secret"
        self.run_main(["--host", "https://x.test", "login", "--email", "a@b.co"],
                      payload={"token": "t", "user": {"email": "a@b.co"}})
        self.assertNotIn("hunter2-secret", config.CREDENTIALS.read_text())

    def test_logout_revokes_on_the_saved_host_even_when_another_is_given(self):
        config.save("https://saved.test", "tok")
        self.run_main(["--host", "https://elsewhere.test", "logout"], payload={"ok": True})
        self.assertTrue(self.requests[0].full_url.startswith("https://saved.test/"))


class PostCommandTest(_IsolatedConfig):
    def setUp(self):
        super().setUp()
        config.save("https://x.test", "tok")

    def test_a_dash_reads_the_body_from_stdin(self):
        code, out, _ = self.run_main(["post", "ws1", "rm1", "-"], payload={"message": {}},
                                     stdin="  multi\nline  \n")
        self.assertEqual(0, code)
        self.assertEqual({"body": "multi\nline"}, json.loads(self.requests[0].data))
        self.assertEqual("Posted.\n", out)

    def test_empty_stdin_posts_nothing(self):
        code, _, err = self.run_main(["post", "ws1", "rm1", "-"], stdin="   \n")
        self.assertEqual(1, code)
        self.assertEqual([], self.requests)
        self.assertIn("Nothing to post", err)

    def test_an_empty_argument_posts_nothing(self):
        code, _, _ = self.run_main(["post", "ws1", "rm1", ""])
        self.assertEqual(1, code)
        self.assertEqual([], self.requests)


class JsonPurityTest(_IsolatedConfig):
    COMMANDS = [
        (["whoami"], {"user": {"email": "a@b.co"}, "workspaces": []}),
        (["workspaces"], {"workspaces": [{"hashid": "ws1", "name": "Acme"}]}),
        (["rooms", "ws1"], {"rooms": [{"id": "rm1", "name": "general"}]}),
        (["read", "ws1", "rm1"], {"messages": [{"body": "hi", "created_at": "2026-01-01T00:00"}]}),
        (["dispatch", "ws1"], {"online": True, "summary": "ok"}),
        (["docs", "ws1", "list"], {"docs": [{"hashid": "d1", "title": "T"}]}),
        (["docs", "ws1", "show", "d1"], {"doc": {"title": "T", "body_md": "B"}}),
        (["members", "ws1", "list"], {"members": [], "invitations": []}),
        (["tools", "ws1", "list"], {"tools": [{"hashid": "t1", "name": "n"}]}),
        (["search", "ws1", "refund"], {"results": [{"body": "refund"}]}),
        (["chats", "ws1", "list"], {"chats": [{"id": "c1"}]}),
        (["errors", "ws1", "list"], {"errors": [], "meta": {"total_pages": 3}}),
        (["usage", "ws1"], {"plan": {"name": "Free"}, "metrics": []}),
        (["visitors", "ws1", "list"], {"visitors": [], "meta": {"total_pages": 2}}),
        (["targets", "ws1"], {"targets": []}),
    ]

    def test_json_mode_prints_one_parseable_document_and_nothing_else(self):
        config.save("https://x.test", "tok")
        for argv, payload in self.COMMANDS:
            with self.subTest(argv=argv):
                code, out, err = self.run_main(["--json", *argv], payload=payload)
                self.assertEqual(0, code, err)
                json.loads(out)
                self.assertEqual("", err)

    def test_plain_mode_renders_every_command_without_crashing(self):
        config.save("https://x.test", "tok")
        for argv, payload in self.COMMANDS:
            with self.subTest(argv=argv):
                code, out, err = self.run_main(argv, payload=payload)
                self.assertEqual(0, code, err)
                self.assertNotIn("Traceback", err)

    def test_a_multi_page_list_says_how_to_get_the_next_page(self):
        config.save("https://x.test", "tok")
        _, out, _ = self.run_main(["errors", "ws1", "list"], payload={
            "errors": [{"fingerprint": "fp1", "occurrences": 2, "message": "boom"}],
            "meta": {"current_page": 1, "total_pages": 3, "total_count": 70}})
        self.assertIn("page 1 of 3 (70 total)", out)


class InterruptTest(unittest.TestCase):
    def test_ctrl_c_exits_130_without_a_traceback(self):
        with mock.patch("vroxy_cli.cli._client") as fake:
            fake.return_value.workspaces.side_effect = KeyboardInterrupt
            self.assertEqual(130, main(["workspaces"]))


if __name__ == "__main__":
    unittest.main()
