"""
Unit tests for the pure-Python bits of feedback_agent.py — prompt
building and Claude-output parsing.  No websocket or subprocess
here; those get exercised in integration when we point the agent
at a real ctovibe_web instance.
"""

import json
import unittest

import feedback_agent as fa


class BuildPromptTest(unittest.TestCase):
    def test_full_payload_renders_all_sections(self):
        payload = {
            "type": "feedback.created",
            "feedback": {
                "hashid":            "abc12345",
                "note":              "make this bigger",
                "page_url":          "https://ctovibe.ai/dashboard",
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
                         "page_url": "https://ctovibe.ai/"},
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
        # ctovibe stores rendered_partials as an ordered list; the
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


if __name__ == "__main__":
    unittest.main()
