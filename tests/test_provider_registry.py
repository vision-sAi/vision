from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vision import config, providers
from vision.config import Config
from vision.ui import QuestionForm


def _cfg(*enabled: str, model: str = "opus") -> Config:
    cfg = Config()
    cfg.providers.enabled = list(enabled)
    cfg.brain.model = model
    return cfg


class RegistryTests(unittest.TestCase):
    def setUp(self):
        providers.forget_ready()
        self.addCleanup(providers.forget_ready)

    def ready(self, *names):
        return patch.object(providers, "ready", side_effect=lambda name, cfg=None: name in names)

    def test_model_tabs_follow_the_enabled_list_and_its_order(self):
        with self.ready("claude", "codex", "grok", "local"):
            tabs = providers.model_tabs(_cfg("local", "claude"))
        self.assertEqual([t[0] for t in tabs], ["Local", "Claude"])

    def test_a_provider_not_set_up_is_one_setup_row(self):
        with self.ready("claude"):
            tabs = dict((t[0], t[1]) for t in providers.model_tabs(_cfg("claude", "codex")))
            phone = [t[0] for t in providers.model_tabs(_cfg("claude", "codex"), setup_rows=False)]
        self.assertEqual(len(tabs["Codex"]), 1)
        value, label, desc = tabs["Codex"][0]
        self.assertEqual((value, label), ("setup:codex", "Not set up"))
        self.assertIn("npm i -g @openai/codex", desc)
        self.assertIn("isn't set up here yet", providers.setup_note(value))
        self.assertIsNone(providers.setup_note("opus"))
        self.assertEqual(phone, ["Claude"])  # the phone can't install anything: left out

    def test_unavailable_reason(self):
        cfg = _cfg("claude", "grok")
        with self.ready("claude"):
            self.assertIsNone(providers.unavailable_reason("claude", cfg))
            self.assertIn("switched off", providers.unavailable_reason("codex", cfg))
            self.assertIn("isn't set up", providers.unavailable_reason("grok", cfg))

    def test_settle_moves_a_fresh_install_to_what_it_has(self):
        cfg = _cfg("claude", "local", model="opus")
        with self.ready("local"), patch("vision.models.provider_default", return_value="local-model"):
            note = providers.settle(cfg)
        self.assertEqual(cfg.brain.model, "local-model")
        self.assertIn("Claude isn't set up here yet", note)
        self.assertIn("config.toml unchanged", note)

    def test_settle_leaves_a_working_model_alone(self):
        cfg = _cfg("claude", model="opus")
        with self.ready("claude"):
            self.assertEqual(providers.settle(cfg), "")
        self.assertEqual(cfg.brain.model, "opus")

    def test_settle_with_nothing_set_up_says_how(self):
        cfg = _cfg("claude", "codex", model="opus")
        with self.ready():
            note = providers.settle(cfg)
        self.assertEqual(cfg.brain.model, "opus")
        self.assertIn("No provider is set up yet", note)
        self.assertIn("claude.ai/install.sh", note)

    def test_create_brain_takes_the_class_from_the_registry(self):
        from vision.brain import create_brain

        with patch("vision.clis.refresh_models_soon"), patch("vision.local.LocalBrain.__init__", return_value=None) as init:
            brain = create_brain(config.BrainConfig(model="local-model", effort=""))
        self.assertEqual(type(brain).__name__, "LocalBrain")
        init.assert_called_once()


class ConfigTests(unittest.TestCase):
    def test_enabled_providers_load_and_save(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text('[providers]\nenabled = ["local", "nope", "claude", "local"]\n', encoding="utf-8")
            with patch.object(config, "CONFIG_PATH", path), patch.object(config, "ensure_dirs"):
                self.assertEqual(config.load_config().providers.enabled, ["local", "claude"])
                config.save_enabled_providers(["codex"])
                self.assertEqual(config.load_config().providers.enabled, ["codex"])

    def test_default_is_every_provider(self):
        self.assertEqual(Config().providers.enabled, ["claude", "codex", "grok", "local"])


class PreselectedFormTests(unittest.TestCase):
    def test_ticks_start_on(self):
        form = QuestionForm([{"question": "Which?", "header": "P", "multiSelect": True, "preselected": [0, 2],
                              "options": [{"label": "A"}, {"label": "B"}, {"label": "C"}]}])
        form.toggle(0)  # untick A
        self.assertTrue(form.confirm())
        self.assertEqual(form.answers(), {"Which?": "C"})

    def test_enter_ticks_and_submit_finishes(self):
        form = QuestionForm([{"question": "Which?", "header": "P", "multiSelect": True, "preselected": [0], "other": False,
                              "options": [{"label": "A"}, {"label": "B"}]}])
        self.assertEqual(form.rows, 3)  # A, B, Submit: no ✎ row
        self.assertFalse(form.enter())  # untick A
        form.down()
        self.assertFalse(form.enter())  # tick B
        form.down()
        self.assertEqual(form.idx, form.submit)
        self.assertTrue(form.enter())
        self.assertEqual(form.answers(), {"Which?": "B"})

    def test_typed_text_in_a_multi_select_waits_for_submit(self):
        form = QuestionForm([{"question": "Which?", "header": "P", "multiSelect": True, "options": [{"label": "A"}]}])
        self.assertFalse(form.enter("my own"))
        self.assertEqual(form.idx, form.submit)
        self.assertTrue(form.enter())
        self.assertEqual(form.answers(), {"Which?": "my own"})

    def test_single_select_enter_still_confirms(self):
        form = QuestionForm([{"question": "One?", "header": "P", "options": [{"label": "A"}, {"label": "B"}]}])
        form.down()
        self.assertTrue(form.enter())
        self.assertEqual(form.answers(), {"One?": "B"})


if __name__ == "__main__":
    unittest.main()


# -- a provider Vision has never heard of, wired only through the registry ------------------------
_SEEN: dict = {}


def fake_paths():
    return ["/nowhere/a.json"]


def fake_parse(path):
    from vision.sessions import SessionInfo

    return SessionInfo(id="acme-1", provider="acme", title="from the fake", last_active=0.0)


def fake_history(session_id, limit, include_context=False):
    _SEEN["history"] = session_id
    return [{"role": "user", "text": "hi"}]


def fake_usage(brain):
    return {"plan": "Acme Max", "windows": []}


def fake_notes(tools, sandbox, denied_tools, weather):
    return ["ACME NOTE"]


class NewProviderTests(unittest.TestCase):
    """A fifth provider needs a registry entry and the functions its hooks name, nothing else."""

    def setUp(self):
        here = __name__  # the hooks must land in this very module (its _SEEN)
        self.p = providers.Provider(
            "acme", "Acme", "Acme CLI", "via Acme", "install acme", "vision.brain:Brain", "Acme's model",
            cli="acme", home_env="ACME_HOME", turn_env=(("ACME_QUIET", "1"),), has_usage=True,
            hooks={"session_paths": f"{here}:fake_paths", "parse_session": f"{here}:fake_parse",
                   "history": f"{here}:fake_history", "usage": f"{here}:fake_usage", "tool_notes": f"{here}:fake_notes"})
        providers.REGISTRY["acme"] = self.p
        self.addCleanup(providers.REGISTRY.pop, "acme", None)

    def test_sessions_history_usage_and_persona(self):
        from vision import persona, sessions, usage

        self.assertEqual([s.id for s in sessions.list_sessions("acme")], ["acme-1"])
        with patch("vision.agentlog.attach", side_effect=lambda turns, provider, sid: turns):
            self.assertEqual(sessions.session_history("acme", "acme-1")[0]["text"], "hi")
        self.assertEqual(_SEEN["history"], "acme-1")
        with patch("vision.usage.provider_default", return_value="x", create=True):
            row = usage.usage_data(Config(), type("B", (), {"provider": "acme"})(), "acme")
        self.assertEqual((row["plan"], row["error"]), ("Acme Max", None))
        prompt = persona.system_prompt(False, "", "/w", ["Bash"], provider="acme", memory="")
        self.assertIn("You are powered by Acme's model", prompt)
        self.assertIn("ACME NOTE", prompt)

    def test_its_cli_is_blocked_elsewhere_and_its_env_set_on_its_own_turns(self):
        from vision import brain

        with tempfile.TemporaryDirectory() as d, patch.object(brain, "DATA_DIR", Path(d)):
            own, other = brain.brain_env("acme"), brain.brain_env("claude")
            self.assertTrue((Path(d) / "shims" / "acme").exists())  # its CLI is blocked inside every turn
        self.assertEqual(own.get("ACME_QUIET"), "1")
        self.assertNotEqual(own.get("ACME_HOME"), other.get("ACME_HOME"))
        self.assertIn("no-credentials", other["ACME_HOME"])

    def test_capability_queries(self):
        self.assertIn("acme", providers.with_cli())
        self.assertIn("acme", providers.names(has_usage=True))
        self.assertNotIn("acme", providers.conversation_names())
        self.assertEqual(providers.cap("acme", "powered_by"), "Acme's model")
        self.assertIsNone(providers.cap("nope", "powered_by"))


class PluginAndTableTests(unittest.TestCase):
    def tearDown(self):
        providers.REGISTRY.pop("acme", None)
        providers._plugins_loaded = False

    def test_plugins_come_from_entry_points(self):
        made = providers.Provider("acme", "Acme", "Acme", "via Acme", "install acme", "vision.brain:Brain", "Acme")

        class EP:
            name, value = "acme", "acme_pkg:provider"

            def load(self):
                return lambda: [made]

        class Bad:
            name, value = "broken", "broken_pkg:x"

            def load(self):
                raise ImportError("no such module")

        providers._plugins_loaded = False
        with patch("importlib.metadata.entry_points", return_value=[EP(), Bad()]):
            notes = providers.load_plugins()
        self.assertEqual(providers.REGISTRY["acme"].source, "plugin")
        self.assertEqual(len(notes), 1)
        self.assertIn("broken", notes[0])
        self.assertEqual(providers.load_plugins(), [])  # once per process
        # a config table can't take a plugin's name
        self.assertIn("taken", providers.register_endpoints({"acme": {"base_url": "http://x/v1"}})[0])

    def test_provider_table_written_and_removed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text("[brain]\nmodel = \"opus\"\n\n[providers]\nenabled = [\"claude\"]\n", encoding="utf-8")
            with patch.object(config, "CONFIG_PATH", path), patch.object(config, "ensure_dirs"):
                config.save_provider_table("ollama", {"base_url": "http://h/v1", "label": "", "api_key_env": "K", "context": 4096, "models": ["a", "b"]})
                config.save_provider_table("ollama", {"base_url": "http://h2/v1", "context": None, "models": []})  # rewrite
                cfg = config.load_config()
                self.assertEqual(providers.REGISTRY["ollama"].endpoint.base_url, "http://h2/v1")
                self.assertEqual(cfg.providers.enabled, ["claude", "ollama"])  # an explicit list takes the new one
                self.assertNotIn("api_key_env", path.read_text())
                config.remove_provider_table("ollama")
                self.assertNotIn("[providers.ollama]", path.read_text())
                self.assertIn('enabled = ["claude"]', path.read_text())
        providers.register_endpoints({})

    def test_an_unchanged_provider_table_keeps_the_readiness_cache(self):
        """Every load_config re-registers the [providers.*] tables; clearing the readiness cache each
        time made every new server chat and every /models probe each configured server again."""
        table = {"acme": {"base_url": "http://127.0.0.1:9/v1", "models": ["m"]}}
        providers.register_endpoints(table)
        self.addCleanup(providers.register_endpoints, {})
        self.addCleanup(providers.forget_ready)
        with providers._ready_lock:
            providers._ready_cache[("probe", "")] = (providers.time.monotonic(), True)
        providers.register_endpoints(table)  # the same table again, as a config reload does
        self.assertIn(("probe", ""), providers._ready_cache)
        providers.register_endpoints({"acme": {"base_url": "http://127.0.0.1:9/v2", "models": ["m"]}})
        self.assertNotIn(("probe", ""), providers._ready_cache)  # the servers changed: probe afresh
        with providers._ready_lock:
            providers._ready_cache[("probe", "")] = (providers.time.monotonic(), True)
        providers.register_endpoints({})  # a table removed
        self.assertNotIn(("probe", ""), providers._ready_cache)

    def test_describe_for_the_phone(self):
        d = providers.REGISTRY["local"].describe()
        self.assertEqual((d["name"], d["icon"], d["tint"], d["conversation"], d["ready"]), ("local", "desktopcomputer", "#615CED", True, None))


class ModelServerTests(unittest.TestCase):
    """A provider's server is ready when a model server answers there, not when anything does."""

    def serve(self, status: int, body: bytes):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return providers.Endpoint(f"http://127.0.0.1:{httpd.server_address[1]}/v1")

    def test_a_model_list_counts_even_when_it_is_empty(self):
        self.assertTrue(providers._is_model_server(self.serve(200, b'{"object": "list", "data": [{"id": "qwen3"}]}')))
        self.assertTrue(providers._is_model_server(self.serve(200, b'{"object": "list", "data": []}')))  # up, nothing loaded yet

    def test_an_api_that_wants_a_key_counts(self):
        self.assertTrue(providers._is_model_server(self.serve(401, b'{"error": "no key"}')))

    def test_something_else_on_the_port_does_not(self):
        self.assertFalse(providers._is_model_server(self.serve(404, b"Not Found")))  # a web app on :8080
        self.assertFalse(providers._is_model_server(self.serve(200, b"<html>hello</html>")))
        self.assertFalse(providers._is_model_server(self.serve(200, b'{"status": "ok"}')))

    def test_local_is_not_ready_when_the_port_is_not_a_model_server(self):
        cfg = _cfg("local")
        cfg.local.base_url = self.serve(404, b"Not Found").base_url
        providers.forget_ready()
        self.addCleanup(providers.forget_ready)
        self.assertFalse(providers.ready("local", cfg))
        self.assertIn("isn't set up here yet", providers.unavailable_reason("local", cfg))


class AnswerFirstTests(unittest.TestCase):
    """Every brain is told to answer before acting and to say its plan, typed or spoken."""

    def test_the_persona_says_to_answer_first_and_to_say_the_plan(self):
        from vision.persona import system_prompt

        for provider in ("claude", "codex", "grok", "local"):
            for voice in (False, True):
                prompt = system_prompt(voice, provider=provider, memory="")
                self.assertIn("Answer before you act.", prompt)
                self.assertIn("an error they pasted", prompt)
                self.assertIn("say the plan in one line before", prompt)
                self.assertIn("Ask before anything that runs for minutes", prompt)

    def test_the_factory_effort_is_medium(self):
        from vision.models import coerce_effort

        self.assertEqual(Config().brain.effort, "medium")
        self.assertEqual(coerce_effort("opus", "")[0], "medium")
        self.assertIn('effort = "medium"', config.DEFAULT_CONFIG.split("[brain]")[1].split("[", 1)[0])

