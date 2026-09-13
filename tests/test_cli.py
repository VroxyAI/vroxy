import json
import os
import stat
import tempfile
import unittest
import urllib.error
from io import BytesIO
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
        client = Client(host="https://x.test", token="tok123")
        seen = {}

        def capture(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            return _response({"user": {}})

        with mock.patch("urllib.request.urlopen", side_effect=capture):
            client.me()

        self.assertEqual(seen["auth"], "Bearer tok123")


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


if __name__ == "__main__":
    unittest.main()
