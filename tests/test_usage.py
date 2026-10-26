"""/usage: the shared table, the live Codex app-server fetch, and the rollout fallback."""
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fake_exe import python_command
from rich.console import Console

from vision import usage as usage_ui
from vision.codex import CodexBrain, CodexError, fetch_rate_limits
from vision.config import BrainConfig, CodexConfig, GrokConfig


def _cfg(model="gpt-6-astra"):
    cfg = BrainConfig(model=model, effort="high")
    cfg.codex = CodexConfig()
    cfg.grok = GrokConfig()
    return cfg


def _render(r) -> str:
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=120, color_system=None).print(r)
    return buf.getvalue()


LIVE_REPLY = {
    "rateLimits": {
        "limitId": "codex",
        "primary": {"usedPercent": 7, "windowDurationMins": 300, "resetsAt": 1789907051},
        "secondary": {"usedPercent": 31, "windowDurationMins": 10080, "resetsAt": 1790370407},
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "planType": "plus",
        "rateLimitReachedType": None,
    }
}


def _fake_app_server(td: Path, reply: dict | None = LIVE_REPLY, hang: bool = False) -> str:
    """A stand-in `codex` whose app-server subcommand answers JSON-RPC on stdio."""
    return python_command(td / "codex", (
        "import json, sys, time\n"
        f"reply = {reply!r}\n"
        f"hang = {hang!r}\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    if msg.get('method') == 'initialize':\n"
        "        print(json.dumps({'id': msg['id'], 'result': {'userAgent': 'test'}}), flush=True)\n"
        "        print(json.dumps({'method': 'remoteControl/status/changed', 'params': {}}), flush=True)\n"
        "    elif msg.get('method') == 'account/rateLimits/read':\n"
        "        if hang:\n"
        "            time.sleep(30)\n"
        "        print(json.dumps({'id': msg['id'], 'result': reply}), flush=True)\n"
        "    elif msg.get('method') == 'account/rateLimitResetCredit/consume':\n"
        "        ok = bool(msg['params'].get('idempotencyKey'))\n"
        "        print(json.dumps({'id': msg['id'], 'result': {'outcome': 'reset'}} if ok else\n"
        "                         {'id': msg['id'], 'error': {'code': -32600, 'message': 'idempotencyKey must not be empty'}}), flush=True)\n"
    ))


class SharedLookTests(unittest.TestCase):
    def test_reset_times_use_claude_code_wording(self):
        self.assertEqual(usage_ui.resets_cell(time.mktime((2026, 9, 20, 3, 50, 0, 0, 0, -1))), "Sep 20 3:50am")
        self.assertEqual(usage_ui.resets_cell(time.mktime((2026, 9, 25, 23, 0, 0, 0, 0, -1))), "Sep 25 11pm")
        self.assertEqual(usage_ui.resets_cell(time.mktime((2026, 9, 25, 0, 5, 0, 0, 0, -1))), "Sep 25 12:05am")
        self.assertEqual(usage_ui.resets_cell(None), "?")

    def test_table_has_the_same_columns_for_every_provider(self):
        t = usage_ui.usage_table("Codex", "Plus")
        usage_ui.add_window(t, "Current session", 42.0, None)
        text = _render(t)
        self.assertIn("Codex subscription usage (Plus)", text)
        for col in ("window", "used", "resets"):
            self.assertIn(col, text)
        self.assertIn("42%", text)


class CodexLiveUsageTests(unittest.TestCase):
    def test_fetch_rate_limits_talks_jsonrpc_and_normalises_keys(self):
        with tempfile.TemporaryDirectory() as td:
            exe = _fake_app_server(Path(td))
            rl = fetch_rate_limits(exe, timeout=10)
        self.assertEqual(rl["plan_type"], "plus")
        self.assertEqual(rl["primary"], {"used_percent": 7, "window_minutes": 300, "resets_at": 1789907051})
        self.assertEqual(rl["secondary"]["window_minutes"], 10080)

    def test_fetch_rate_limits_gives_up_on_a_slow_server(self):
        with tempfile.TemporaryDirectory() as td:
            exe = _fake_app_server(Path(td), hang=True)
            start = time.monotonic()
            self.assertIsNone(fetch_rate_limits(exe, timeout=1))
            self.assertLess(time.monotonic() - start, 5)

    def test_fetch_rate_limits_handles_missing_binary(self):
        self.assertIsNone(fetch_rate_limits("/nonexistent/codex", timeout=1))

    def test_usage_prefers_live_and_falls_back_to_rollout(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("vision.codex.find_codex", return_value="codex"), \
             patch("vision.codex.STATE_DIR", Path(td)), \
             patch("vision.codex.USAGE_FILE", Path(td) / "usage"):
            brain = CodexBrain(_cfg())
            brain.session_id = "thread-1"
            rollout_dir = Path(td) / "sessions" / "2026" / "09" / "20"
            rollout_dir.mkdir(parents=True)
            (rollout_dir / "rollout-2026-09-20T02-31-00-thread-1.jsonl").write_text(
                json.dumps({"timestamp": "2026-09-20T06:31:29.000Z", "payload": {"type": "token_count", "rate_limits": {
                    "plan_type": "plus",
                    "primary": {"used_percent": 98.0, "window_minutes": 300, "resets_at": 1789888081},
                    "secondary": {"used_percent": 31.0, "window_minutes": 10080, "resets_at": 1790370408},
                }}}) + "\n"
            )
            with patch("vision.codex.CODEX_SESSIONS", str(Path(td) / "sessions")):
                with patch("vision.codex.fetch_rate_limits", return_value={
                    "plan_type": "plus",
                    "primary": {"used_percent": 7, "window_minutes": 300, "resets_at": 1789907051},
                    "secondary": {"used_percent": 31, "window_minutes": 10080, "resets_at": 1790370407},
                }):
                    live = brain.rate_limits()
                    live_text = _render(brain.usage_renderable())
                with patch("vision.codex.fetch_rate_limits", return_value=None):
                    stale = brain.rate_limits()
                    stale_text = _render(brain.usage_renderable())
        self.assertEqual(live["source"], "live")
        self.assertEqual(live["rate_limits"]["primary"]["used_percent"], 7)
        self.assertIn("Current session", live_text)
        self.assertIn("Current week", live_text)
        self.assertIn("7%", live_text)
        self.assertNotIn("live from", live_text)
        self.assertNotIn("snapshot", live_text)
        self.assertEqual(stale["source"], "rollout")
        self.assertAlmostEqual(stale["at"], 1789885889.0, delta=1)  # 2026-09-20T06:31:29Z
        self.assertIn("98%", stale_text)
        self.assertIn("from the last reply's rate-limit snapshot at", stale_text)

    def test_usage_without_any_data_says_so(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("vision.codex.find_codex", return_value="codex"), \
             patch("vision.codex.STATE_DIR", Path(td)), \
             patch("vision.codex.USAGE_FILE", Path(td) / "usage"), \
             patch("vision.codex.LAST_SESSION_FILE", Path(td) / "last"), \
             patch("vision.codex.fetch_rate_limits", return_value=None):
            brain = CodexBrain(_cfg())
            text = _render(brain.usage_renderable())
        self.assertIn("No Codex usage yet", text)


if __name__ == "__main__":
    unittest.main()


GRANT = {
    "id": "opus55-launch-promax-20260921",
    "label": "Claude Opus 5.5 launch: one usage-limit reset for Pro and Max",
    "resets_total": 1,
    "resets_left": 1,
    "starts_at": "2026-09-22T16:00:00+00:00",
    "ends_at": "2099-10-22T16:00:00+00:00",
    "clears": ["five_hour", "seven_day", "seven_day_overage_included"],
    "paused": False,
}


class BankedResetTests(unittest.TestCase):
    def test_claude_grants_count_uses_left_and_skip_dead_ones(self):
        body = {"cedar_ember": {"eligible": True, "grants": [
            GRANT,
            {**GRANT, "id": "b", "resets_left": 2, "ends_at": "2099-12-01T00:00:00Z"},
            {**GRANT, "id": "paused", "paused": True},
            {**GRANT, "id": "spent", "resets_left": 0},
            {**GRANT, "id": "expired", "ends_at": "2020-01-01T00:00:00+00:00"},
        ]}}
        b = usage_ui.claude_banked_from(body)
        self.assertEqual(b["count"], 3)
        self.assertEqual(b["expires_at"], usage_ui._iso_ts("2099-10-22T16:00:00+00:00"))
        self.assertIn("Opus 5.5 launch", b["label"])
        self.assertEqual([(g["count"], g["expires_at"], g["kind"]) for g in b["resets"]],
                         [(1, usage_ui._iso_ts("2099-10-22T16:00:00+00:00"), "Full Reset"), (2, usage_ui._iso_ts("2099-12-01T00:00:00Z"), "Full Reset")])

    def test_claude_reset_kind_follows_what_it_clears(self):
        kind = usage_ui.claude_reset_kind
        self.assertEqual(kind(["five_hour", "seven_day", "seven_day_overage_included"]), "Full Reset")
        self.assertEqual(kind(["five_hour"]), "Session Reset")
        self.assertEqual(kind(["seven_day", "seven_day_overage_included"]), "Weekly Reset")
        self.assertEqual(kind(["seven_day_fable"]), "Fable Reset")
        self.assertIsNone(kind([]))

    def test_claude_ineligible_or_missing_block_is_none(self):
        self.assertIsNone(usage_ui.claude_banked_from({}))
        self.assertIsNone(usage_ui.claude_banked_from({"cedar_ember": {"eligible": False, "ineligible_reason": "cli_version", "grants": [GRANT]}}))
        self.assertIsNone(usage_ui.claude_banked_from({"cedar_ember": {"eligible": True, "grants": []}}))

    def test_claude_expired_login_is_not_used(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 1000}}))
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": td}), patch("urllib.request.urlopen") as urlopen, \
                    patch.object(usage_ui, "_claude_banked_file", return_value=Path(td, "banked.json")):
                self.assertIsNone(usage_ui.claude_banked())
            urlopen.assert_not_called()

    def test_claude_rate_limit_falls_back_to_a_recent_answer_only(self):
        import urllib.error
        def throttled(*a, **k):
            raise urllib.error.HTTPError(usage_ui.CLAUDE_USAGE_URL, 429, "Too Many Requests", {}, None)
        body = {"cedar_ember": {"eligible": True, "grants": [GRANT]}}
        with tempfile.TemporaryDirectory() as td:
            Path(td, ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 9e12}}))
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": td}), patch.object(usage_ui, "_claude_throttled_until", 0.0), \
                    patch.object(usage_ui, "_claude_banked_file", return_value=Path(td, "banked.json")):
                with patch("urllib.request.urlopen", side_effect=throttled) as urlopen:
                    self.assertIsNone(usage_ui.claude_banked())  # nothing saved yet
                    usage_ui._claude_banked_save(body)
                    self.assertEqual(usage_ui.claude_banked(), usage_ui.claude_banked_from(body))
                    with patch.object(usage_ui, "CLAUDE_BANKED_MAX_AGE", -1):
                        self.assertIsNone(usage_ui.claude_banked())  # too old to trust
                self.assertEqual(urlopen.call_count, 1)  # the 429 left it alone for the cooldown

    def test_claude_spends_the_next_usable_grant(self):
        now = {**GRANT, "usable_now": True}
        block = {"eligible": True, "next_grant_id": "later", "grants": [now, {**now, "id": "later"}]}
        self.assertEqual(usage_ui._pick_claude_grant(block)[0]["id"], "later")
        self.assertEqual(usage_ui._pick_claude_grant(block, GRANT["id"])[0]["id"], GRANT["id"])  # the row tapped
        self.assertIsNone(usage_ui._pick_claude_grant(block, "gone")[0])
        two = usage_ui.claude_banked_from({"cedar_ember": {**block, "grants": [{**now, "resets_left": 2}]}})
        self.assertEqual([r["id"] for r in two["items"]], [GRANT["id"], GRANT["id"]])  # one row per reset
        waiting = {**GRANT, "usable_now": False, "use_requires_limit": True}
        grant, why = usage_ui._pick_claude_grant({"eligible": True, "grants": [waiting]})
        self.assertIsNone(grant)
        self.assertIn("hit a limit", why)

    def test_claude_reset_posts_the_claim_with_claude_codes_login(self):
        sent = {}

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            if req.get_method() == "GET":
                return Reply(json.dumps({"cedar_ember": {"eligible": True, "next_grant_id": GRANT["id"],
                                                         "grants": [{**GRANT, "usable_now": True}]}}).encode())
            sent.update(url=req.full_url, body=json.loads(req.data), auth=req.get_header("Authorization"))
            return Reply(json.dumps({"result": "reset", "resets_left": 0}).encode())

        with tempfile.TemporaryDirectory() as td:
            Path(td, ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 9e12}}))
            Path(td, ".claude.json").write_text(json.dumps({"oauthAccount": {"organizationUuid": "org-1"}}))
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": td}), patch.object(usage_ui, "_claude_throttled_until", 0.0), \
                    patch.object(usage_ui, "_claude_banked_file", return_value=Path(td, "banked.json")), \
                    patch("urllib.request.urlopen", side_effect=urlopen):
                out = usage_ui.use_claude_reset()
                self.assertFalse(Path(td, "banked.json").exists())  # the old count is not trusted after a claim
        self.assertEqual(out, {"ok": True, "outcome": "reset", "message": "Claude's limits are reset."})
        self.assertEqual(sent["url"], "https://api.anthropic.com/api/organizations/org-1/reset_rate_limits")
        self.assertEqual(sent["body"]["program"], "cedar_ember")
        self.assertEqual(sent["body"]["grant_id"], GRANT["id"])
        self.assertTrue(sent["body"]["request_id"])
        self.assertEqual(sent["auth"], "Bearer t")

    def test_codex_reset_goes_through_the_app_server(self):
        from vision.codex import consume_reset_credit

        with tempfile.TemporaryDirectory() as td:
            exe = _fake_app_server(Path(td))
            self.assertEqual(consume_reset_credit(exe, "key-1", "c1", timeout=10), "reset")
            with self.assertRaises(CodexError):
                consume_reset_credit(exe, "", timeout=10)

    def test_codex_reset_credits_ride_along_from_the_app_server(self):
        reply = {**LIVE_REPLY, "rateLimitResetCredits": {"availableCount": 2, "credits": [
            {"id": "used", "status": "consumed", "expiresAt": 1790000000},
            {"id": "c1", "status": "available", "resetType": "full", "title": "Full reset (Weekly + 5 hr)", "grantedAt": 1789000000, "expiresAt": 1792600000},
        ]}}
        with tempfile.TemporaryDirectory() as td:
            rl = fetch_rate_limits(_fake_app_server(Path(td), reply=reply), timeout=10)
        self.assertEqual(usage_ui.codex_banked(rl), {"count": 2, "expires_at": 1792600000, "label": "Full reset (Weekly + 5 hr)", "resets": [
            {"count": 1, "expires_at": 1792600000, "kind": "Full Reset", "label": "Full reset (Weekly + 5 hr)"},
            {"count": 1, "expires_at": None, "kind": None, "label": None},  # the list left one out
        ], "items": [
            {"id": "c1", "kind": "Full Reset", "expires_at": 1792600000, "label": "Full reset (Weekly + 5 hr)"},
            {"id": None, "kind": None, "expires_at": None, "label": None},  # spent by the backend's own pick
        ]})
        self.assertIsNone(usage_ui.codex_banked({"reset_credits": {"available_count": 0}}))
        self.assertIsNone(usage_ui.codex_banked({}))

    def test_banked_row_reads_naturally(self):
        t = usage_ui.usage_table("Claude")
        usage_ui.add_banked(t, {"count": 1, "expires_at": time.mktime((2026, 10, 22, 12, 0, 0, 0, 0, -1)), "label": None})
        text = _render(t)
        self.assertIn("Banked resets", text)
        self.assertIn("1 reset", text)
        self.assertIn("expires Oct 22", text)
        t = usage_ui.usage_table("Claude")
        usage_ui.add_banked(t, None)
        self.assertNotIn("Banked", _render(t))

    def test_each_expiry_shows_on_its_own(self):
        oct22, nov3 = (time.mktime((2026, m, d, 12, 0, 0, 0, 0, -1)) for m, d in ((10, 22), (11, 3)))
        banked = usage_ui._banked([(2, nov3, "Session Reset", None), (1, oct22, "Full Reset", None)])
        self.assertEqual(usage_ui.banked_text(banked), "3 resets banked: 1 Full Reset expires Oct 22, 2 Session Resets expire Nov 3")
        t = usage_ui.usage_table("Claude")
        usage_ui.add_banked(t, banked)
        text = _render(t)
        self.assertIn("1 Full Reset", text)
        self.assertIn("expires Oct 22", text)
        self.assertIn("2 Session Resets", text)
        self.assertIn("expire Nov 3", text)
        self.assertEqual(usage_ui.banked_text(usage_ui._banked([(2, nov3, "Full Reset", None)])), "2 Full Resets banked, expire Nov 3")
        self.assertEqual(usage_ui.banked_text(usage_ui._banked([(1, nov3, None, None)])), "1 reset banked, expires Nov 3")


CLAUDE_ROW = {"provider": "claude", "label": "Claude", "plan": None, "banked": {"count": 1, "expires_at": time.mktime((2026, 10, 22, 12, 0, 0, 0, 0, -1)), "label": None},
              "windows": [{"label": "Current session", "used_percent": 18.0, "resets_at": None, "resets": "Sep 23 2:10am"},
                          {"label": "Current week", "used_percent": 49.0, "resets_at": None, "resets": "Sep 25 11pm"}],
              "notes": [], "source": "live", "at": None, "error": None}


class UsageQuestionTests(unittest.TestCase):
    """The conversation model's `usage` field: which requests get it, and what it says."""

    def test_usage_questions_are_recognised(self):
        for text in ("how much Claude usage do I have left", "am I close to my limit?", "what's my weekly limit at",
                     "when does my week reset", "how much usage have I got", "check my Codex usage"):
            self.assertTrue(usage_ui.is_usage_request(text), text)
        for text in ("what's my memory usage", "what's the CPU usage", "what's the speed limit on the A3",
                     "how do I use the usage module", "what's the weather"):
            self.assertFalse(usage_ui.is_usage_request(text), text)

    def test_providers_default_to_claude(self):
        self.assertEqual(usage_ui.requested_providers("how much usage have I got left"), ("claude",))
        self.assertEqual(usage_ui.requested_providers("what's my Codex usage"), ("codex",))
        self.assertEqual(usage_ui.requested_providers("usage left on all of them"), usage_ui.PROVIDERS)

    def test_text_carries_the_phone_page_figures(self):
        text = usage_ui.usage_text([CLAUDE_ROW, {"provider": "grok", "label": "Grok", "error": "Grok usage unavailable."}])
        self.assertIn("Claude subscription usage:", text)
        self.assertIn("- Current session: 18% used, 82% left, resets Sep 23 2:10am", text)
        self.assertIn("- Current week: 49% used, 51% left, resets Sep 25 11pm", text)
        self.assertIn("1 reset banked, expires Oct 22", text)
        self.assertIn("Grok subscription usage: Grok usage unavailable.", text)

    def test_prefetch_uses_the_phone_endpoints_fetch(self):
        brain = object()
        with patch.object(usage_ui, "usage_data", return_value=CLAUDE_ROW) as data:
            t = usage_ui.prefetch("cfg", brain, "how much Claude usage do I have left")
            t.join(5)
        data.assert_called_once_with("cfg", brain, "claude")
        self.assertEqual(t.result, usage_ui.usage_text([CLAUDE_ROW]))
        self.assertIsNone(t.error)
        with patch.object(usage_ui, "usage_data") as data:
            self.assertIsNone(usage_ui.prefetch("cfg", brain, "tell me a joke"))
        data.assert_not_called()
