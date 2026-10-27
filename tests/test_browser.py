"""The `vroxy-browser` headless-browser tool — argument surface and the
missing-playwright exit, which are the parts that don't need a live
Chromium. The `shot`/`text`/`login` bodies are exercised against real
pages on the box, not in CI."""

import unittest
from unittest import mock

import vroxy_browser


class BrowserToolTest(unittest.TestCase):
    def test_parser_exposes_shot_text_login(self):
        p = vroxy_browser.build_parser()

        args = p.parse_args(["shot", "https://vroxy.ai", "--out", "/tmp/x.png"])
        self.assertEqual(args.command, "shot")
        self.assertEqual(args.url, "https://vroxy.ai")
        self.assertEqual(args.out, "/tmp/x.png")

        self.assertEqual(p.parse_args(["text", "https://vroxy.ai"]).url, "https://vroxy.ai")
        self.assertEqual(
            p.parse_args(["login", "https://vroxy.ai/users/sign_in/token?x=1"]).link,
            "https://vroxy.ai/users/sign_in/token?x=1",
        )

    def test_missing_playwright_exits_with_instructions(self):
        with mock.patch("builtins.__import__", side_effect=ImportError("no playwright")):
            with self.assertRaises(SystemExit) as ctx:
                vroxy_browser._sync_playwright()
        self.assertIn("playwright is not installed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
