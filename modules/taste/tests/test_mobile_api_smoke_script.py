import importlib.util
import unittest
from pathlib import Path


def load_mobile_smoke_module():
    root = Path(__file__).resolve().parents[3]
    script = root / "scripts" / "mobile_api_smoke.py"
    spec = importlib.util.spec_from_file_location("mobile_api_smoke", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RecordingClient:
    def __init__(self):
        self.calls = []
        self.job_detail_calls = 0

    def get(self, path, query=None):
        self.calls.append(("GET", path, query or {}))
        if path == "/health":
            return {"ok": True}
        if path == "/api/config/meta":
            return {
                "saved": True,
                "mobile_api_version": 1,
                "mobile_capabilities": [
                    "projects",
                    "jobs",
                    "runtime",
                    "llm_config",
                    "claude_latest_response",
                    "remote_artifacts",
                ],
            }
        if path == "/api/projects":
            return [{"project": "existing"}]
        if path == "/api/projects/ios_e2e_mobile_app":
            return {"project": "ios_e2e_mobile_app", "status": "ok"}
        if path == "/api/projects/ios_e2e_mobile_app/runtime":
            return {"project": "ios_e2e_mobile_app", "runtime": {}, "checks": {}}
        if path == "/api/projects/ios_e2e_mobile_app/claude/latest-response":
            return {
                "response_markdown": "Claude response tail",
                "response_chcount": 20,
                "returned_chcount": 20,
                "truncated": False,
            }
        if path == "/api/jobs":
            return []
        if path == "/api/jobs/status_123":
            self.job_detail_calls += 1
            if self.job_detail_calls == 1:
                return {
                    "job_id": "status_123",
                    "stage": "status",
                    "status": "running",
                    "progress": {"phase": "started", "message": "status started"},
                    "logs": ["status started"],
                }
            return {
                "job_id": "status_123",
                "stage": "status",
                "status": "done",
                "progress": {"phase": "complete", "message": "status complete"},
                "logs": ["status started", "status complete"],
            }
        raise AssertionError(f"Unexpected GET {path}")

    def post(self, path, payload=None):
        self.calls.append(("POST", path, payload or {}))
        if path == "/api/projects":
            return {"project": payload["id"], "topic": payload.get("topic", "")}
        if path == "/api/projects/ios_e2e_mobile_app/config":
            return {"project": "ios_e2e_mobile_app", "run_preferences": payload}
        if path == "/api/config/llm-probe":
            return {
                "ok": False,
                "error": "Connection reset by peer",
                "summary": {
                    "provider": "openai_compatible",
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-v4-pro",
                    "api_key_suffix": "ff04",
                },
                "api_key": "sk-secret-value",
                "authorization": "Bearer secret-token",
            }
        if path == "/api/jobs/project":
            return {"job_id": "status_123", "stage": payload["action"], "status": "queued"}
        raise AssertionError(f"Unexpected POST {path}")


class AuthFailureProbeClient(RecordingClient):
    def post(self, path, payload=None):
        if path == "/api/config/llm-probe":
            self.calls.append(("POST", path, payload or {}))
            return {
                "ok": False,
                "error": "LLM HTTP 401 via chat_completions: Invalid API Key",
                "summary": {
                    "provider": "openai_compatible",
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-v4-pro",
                },
            }
        return super().post(path, payload)


class MobileAPISmokeScriptTests(unittest.TestCase):
    def test_normalizes_server_url_and_builds_bearer_header(self):
        module = load_mobile_smoke_module()

        self.assertEqual(module.normalize_base_url(" http://taste.local:8765/ "), "http://taste.local:8765")
        self.assertEqual(module.auth_headers(" secret-token "), {"Authorization": "Bearer secret-token"})
        self.assertEqual(module.auth_headers(""), {})

    def test_smoke_sequence_covers_mobile_control_plane_without_heavy_workflow(self):
        module = load_mobile_smoke_module()
        client = RecordingClient()

        summary = module.run_smoke(
            client,
            project_id="ios_e2e_mobile_app",
            topic="Mobile control plane smoke",
            create_project=True,
            light_actions=["status"],
        )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["project_id"], "ios_e2e_mobile_app")
        self.assertTrue(summary["created_project"])
        self.assertTrue(summary["checks"]["claude_latest_response"])
        self.assertEqual(summary["checks"]["mobile_api_version"], 1)
        self.assertTrue(summary["checks"]["mobile_control_plane"])
        self.assertEqual(summary["checks"]["claude_latest_returned_chars"], 20)
        self.assertEqual(summary["light_actions"][0]["action"], "status")
        self.assertEqual(summary["light_actions"][0]["job_id"], "status_123")
        self.assertEqual(
            client.calls,
            [
                ("GET", "/health", {}),
                ("GET", "/api/config/meta", {}),
                ("GET", "/api/projects", {}),
                ("POST", "/api/projects", {"id": "ios_e2e_mobile_app", "topic": "Mobile control plane smoke"}),
                ("POST", "/api/projects/ios_e2e_mobile_app/config", {
                    "research_interest": "Mobile control plane smoke",
                    "researcher_profile": "iOS mobile smoke test; prefer lightweight server-side validation.",
                }),
                ("GET", "/api/projects/ios_e2e_mobile_app", {"compact": "1"}),
                ("GET", "/api/projects/ios_e2e_mobile_app/runtime", {}),
                ("GET", "/api/projects/ios_e2e_mobile_app/claude/latest-response", {"max_chars": "16000"}),
                ("GET", "/api/jobs", {"compact": "1", "limit": "12", "include_history": "1", "project": "ios_e2e_mobile_app"}),
                ("POST", "/api/jobs/project", {"project": "ios_e2e_mobile_app", "action": "status"}),
            ],
        )

    def test_optional_llm_probe_reports_sanitized_result_without_failing_smoke(self):
        module = load_mobile_smoke_module()
        client = RecordingClient()

        summary = module.run_smoke(
            client,
            project_id="ios_e2e_mobile_app",
            topic="Mobile control plane smoke",
            create_project=True,
            include_llm_probe=True,
        )

        self.assertTrue(summary["ok"])
        self.assertFalse(summary["checks"]["llm_probe"])
        self.assertEqual(summary["llm_probe"]["error"], "Connection reset by peer")
        self.assertEqual(summary["llm_probe"]["summary"]["api_key_suffix"], "ff04")
        self.assertEqual(summary["llm_probe"]["api_key"], "<redacted>")
        self.assertEqual(summary["llm_probe"]["authorization"], "<redacted>")
        self.assertEqual(summary["llm_probe_diagnostic"]["category"], "network_unreachable")
        self.assertEqual(summary["llm_probe_diagnostic"]["host"], "api.deepseek.com")
        self.assertIn("network", summary["llm_probe_diagnostic"]["message"])
        self.assertNotIn("sk-secret-value", repr(summary))
        self.assertIn(("POST", "/api/config/llm-probe", {}), client.calls)

    def test_optional_llm_probe_distinguishes_auth_failure_from_network_failure(self):
        module = load_mobile_smoke_module()
        client = AuthFailureProbeClient()

        summary = module.run_smoke(
            client,
            project_id="ios_e2e_mobile_app",
            topic="Mobile control plane smoke",
            create_project=True,
            include_llm_probe=True,
        )

        self.assertFalse(summary["checks"]["llm_probe"])
        self.assertEqual(summary["llm_probe_diagnostic"]["category"], "auth_failed")
        self.assertEqual(summary["llm_probe_diagnostic"]["host"], "api.deepseek.com")
        self.assertIn("API key", summary["llm_probe_diagnostic"]["message"])

    def test_parser_exposes_optional_llm_probe_flag(self):
        module = load_mobile_smoke_module()

        args = module.build_parser().parse_args(["--llm-probe"])

        self.assertTrue(args.llm_probe)

    def test_wait_light_action_polls_job_detail_until_terminal_status(self):
        module = load_mobile_smoke_module()
        client = RecordingClient()

        summary = module.run_smoke(
            client,
            project_id="ios_e2e_mobile_app",
            topic="Mobile control plane smoke",
            create_project=True,
            light_actions=["status"],
            wait_light_actions=True,
            action_wait_timeout=2,
            action_poll_interval=0,
        )

        action = summary["light_actions"][0]
        self.assertEqual(action["action"], "status")
        self.assertEqual(action["job_id"], "status_123")
        self.assertEqual(action["final_status"], "done")
        self.assertEqual(action["progress_phase"], "complete")
        self.assertEqual(action["progress_message"], "status complete")
        self.assertEqual(action["poll_count"], 2)
        self.assertEqual(action["log_tail"], ["status started", "status complete"])
        self.assertIn(("GET", "/api/jobs/status_123", {"compact": "1"}), client.calls)

    def test_smoke_fails_fast_when_server_does_not_advertise_mobile_api(self):
        module = load_mobile_smoke_module()
        client = RecordingClient()

        def legacy_meta(path, query=None):
            client.calls.append(("GET", path, query or {}))
            if path == "/health":
                return {"ok": True}
            if path == "/api/config/meta":
                return {"saved": True}
            raise AssertionError(f"Unexpected GET after incompatible meta: {path}")

        client.get = legacy_meta

        with self.assertRaisesRegex(RuntimeError, "mobile control-plane API"):
            module.run_smoke(client, project_id="ios_e2e_mobile_app")


if __name__ == "__main__":
    unittest.main()
