import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


def load_ios_smoke_module():
    root = Path(__file__).resolve().parents[3]
    script = root / "scripts" / "ios_app_smoke.py"
    spec = importlib.util.spec_from_file_location("ios_app_smoke", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Completed:
    def __init__(self, stdout=""):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


class RecordingRunner:
    def __init__(self, device_state="Shutdown"):
        self.calls = []
        self.device_state = device_state

    def run(self, args, cwd=None, check=True):
        self.calls.append((tuple(args), str(cwd) if cwd else "", check))
        if tuple(args[:5]) == ("xcrun", "simctl", "list", "devices", "available"):
            return Completed(json.dumps({
                "devices": {
                    "com.apple.CoreSimulator.SimRuntime.iOS-26-5": [
                        {"name": "iPhone 17", "udid": "PHONE-UDID", "state": self.device_state, "isAvailable": True},
                    ],
                    "com.apple.CoreSimulator.SimRuntime.watchOS-26-5": [
                        {"name": "Apple Watch", "udid": "WATCH-UDID", "state": "Booted", "isAvailable": True},
                    ],
                }
            }))
        if tuple(args[:3]) == (sys.executable, "scripts/mobile_api_smoke.py", "--server-url"):
            return Completed('{"ok": true, "checks": {"health": true}}')
        return Completed("")


class IOSAppSmokeScriptTests(unittest.TestCase):
    def test_ios_app_smoke_builds_installs_launches_and_uses_explicit_iphone_udid(self):
        module = load_ios_smoke_module()
        runner = RecordingRunner()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "apps" / "ios" / "TASTEApp"
            app_root.mkdir(parents=True)
            screenshot = root / "taste-ios-smoke.png"

            summary = module.run_ios_app_smoke(
                runner,
                root=root,
                app_root=app_root,
                derived_data=root / ".ios-derived-data",
                device_name="iPhone 17",
                screenshot=screenshot,
                server_url="http://127.0.0.1:8765",
                token="server-token",
                project_id="ios_e2e_mobile_app",
            )

        commands = [call[0] for call in runner.calls]
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["device_udid"], "PHONE-UDID")
        self.assertEqual(summary["screenshot"], str(screenshot))
        self.assertIn(("xcrun", "simctl", "boot", "PHONE-UDID"), commands)
        self.assertIn(("xcrun", "simctl", "bootstatus", "PHONE-UDID", "-b"), commands)
        self.assertTrue(any(command[:4] == ("xcrun", "simctl", "install", "PHONE-UDID") for command in commands))
        self.assertTrue(any(command[:4] == ("xcrun", "simctl", "launch", "PHONE-UDID") for command in commands))
        self.assertTrue(any(command[:5] == ("xcrun", "simctl", "io", "PHONE-UDID", "screenshot") for command in commands))
        self.assertTrue(any(command[:2] == ("xcodebuild", "-project") for command in commands))
        self.assertTrue(any(command[:3] == (sys.executable, "scripts/mobile_api_smoke.py", "--server-url") for command in commands))
        self.assertFalse(any("booted" in command for command in commands))

    def test_ios_app_smoke_opens_connection_deep_link_without_echoing_secret(self):
        module = load_ios_smoke_module()
        runner = RecordingRunner()
        link = "taste://connect?server_url=http%3A%2F%2F127.0.0.1%3A8765&token=secret-token&project=ios_e2e_mobile_app"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "apps" / "ios" / "TASTEApp"
            app_root.mkdir(parents=True)

            summary = module.run_ios_app_smoke(
                runner,
                root=root,
                app_root=app_root,
                derived_data=root / ".ios-derived-data",
                device_name="iPhone 17",
                screenshot=root / "taste-ios-smoke.png",
                server_url="http://127.0.0.1:8765",
                project_id="ios_e2e_mobile_app",
                connection_link=link,
                skip_api_smoke=True,
            )

        commands = [call[0] for call in runner.calls]
        self.assertIn(("xcrun", "simctl", "openurl", "PHONE-UDID", link), commands)
        self.assertTrue(summary["connection_link_dispatched"])
        self.assertIn("token=REDACTED", summary["connection_link"])
        self.assertNotIn("secret-token", json.dumps(summary))

    def test_ios_app_smoke_can_import_connection_link_on_launch_without_url_prompt(self):
        module = load_ios_smoke_module()
        runner = RecordingRunner()
        link = "taste://connect?server_url=http%3A%2F%2F127.0.0.1%3A8765&token=secret-token&project=ios_e2e_mobile_app"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "apps" / "ios" / "TASTEApp"
            app_root.mkdir(parents=True)

            summary = module.run_ios_app_smoke(
                runner,
                root=root,
                app_root=app_root,
                derived_data=root / ".ios-derived-data",
                device_name="iPhone 17",
                screenshot=root / "taste-ios-smoke.png",
                server_url="http://127.0.0.1:8765",
                project_id="ios_e2e_mobile_app",
                connection_link=link,
                connection_link_dispatch="launch_argument",
                skip_api_smoke=True,
            )

        commands = [call[0] for call in runner.calls]
        launch_command = next(command for command in commands if command[:4] == ("xcrun", "simctl", "launch", "PHONE-UDID"))
        self.assertIn("--taste-connection-link", launch_command)
        self.assertIn(link, launch_command)
        self.assertFalse(any(command[:4] == ("xcrun", "simctl", "openurl", "PHONE-UDID") for command in commands))
        self.assertTrue(summary["connection_link_dispatched"])
        self.assertEqual(summary["connection_link_dispatch"], "launch_argument")
        self.assertIn("token=REDACTED", summary["connection_link"])
        self.assertNotIn("secret-token", json.dumps(summary))

    def test_ios_app_smoke_reboots_booted_simulator_before_launch_argument_import(self):
        module = load_ios_smoke_module()
        runner = RecordingRunner(device_state="Booted")
        link = "taste://connect?server_url=http%3A%2F%2F127.0.0.1%3A8765&project=ios_e2e_mobile_app"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "apps" / "ios" / "TASTEApp"
            app_root.mkdir(parents=True)

            module.run_ios_app_smoke(
                runner,
                root=root,
                app_root=app_root,
                derived_data=root / ".ios-derived-data",
                device_name="iPhone 17",
                screenshot=root / "taste-ios-smoke.png",
                server_url="http://127.0.0.1:8765",
                project_id="ios_e2e_mobile_app",
                connection_link=link,
                connection_link_dispatch="launch_argument",
                skip_api_smoke=True,
            )

        commands = [call[0] for call in runner.calls]
        self.assertIn(("xcrun", "simctl", "shutdown", "PHONE-UDID"), commands)
        self.assertIn(("xcrun", "simctl", "boot", "PHONE-UDID"), commands)

    def test_ios_app_smoke_can_wait_for_lightweight_remote_action(self):
        module = load_ios_smoke_module()
        runner = RecordingRunner()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "apps" / "ios" / "TASTEApp"
            app_root.mkdir(parents=True)

            summary = module.run_ios_app_smoke(
                runner,
                root=root,
                app_root=app_root,
                derived_data=root / ".ios-derived-data",
                device_name="iPhone 17",
                screenshot=root / "taste-ios-smoke.png",
                server_url="http://127.0.0.1:8765",
                token="server-token",
                project_id="ios_e2e_mobile_app",
                light_actions=["healthcheck"],
                wait_light_actions=True,
                action_wait_timeout=45,
                action_poll_interval=0.5,
            )

        commands = [call[0] for call in runner.calls]
        api_command = next(command for command in commands if command[:3] == (sys.executable, "scripts/mobile_api_smoke.py", "--server-url"))
        self.assertTrue(summary["ok"])
        self.assertIn("--light-action", api_command)
        self.assertIn("healthcheck", api_command)
        self.assertIn("--wait-light-actions", api_command)
        self.assertIn("--action-wait-timeout", api_command)
        self.assertIn("45", api_command)
        self.assertIn("--action-poll-interval", api_command)
        self.assertIn("0.5", api_command)


if __name__ == "__main__":
    unittest.main()
