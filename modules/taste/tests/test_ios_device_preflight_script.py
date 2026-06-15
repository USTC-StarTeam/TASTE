import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


def load_ios_device_preflight_module():
    root = Path(__file__).resolve().parents[3]
    script = root / "scripts" / "ios_device_preflight.py"
    spec = importlib.util.spec_from_file_location("ios_device_preflight", script)
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
    def __init__(self, *, devices_payload=None, api_payload=None):
        self.calls = []
        self.devices_payload = devices_payload if devices_payload is not None else {
            "result": {
                "devices": [
                    {
                        "identifier": "REAL-IPHONE-UDID",
                        "name": "Hao iPhone",
                        "deviceType": "iPhone",
                        "state": "available",
                        "connectionProperties": {"transportType": "wired"},
                    },
                    {
                        "identifier": "WATCH-UDID",
                        "name": "Apple Watch",
                        "deviceType": "Apple Watch",
                        "state": "available",
                    },
                ],
            },
        }
        self.api_payload = api_payload if api_payload is not None else {
            "ok": True,
            "checks": {
                "mobile_api_version": 1,
                "mobile_control_plane": True,
            },
        }

    def run(self, args, cwd=None, check=True):
        self.calls.append((tuple(args), str(cwd) if cwd else "", check))
        if tuple(args[:4]) == ("xcrun", "devicectl", "list", "devices"):
            return Completed(json.dumps(self.devices_payload))
        if tuple(args[:3]) == (sys.executable, "scripts/mobile_api_smoke.py", "--server-url"):
            return Completed(json.dumps(self.api_payload))
        return Completed("")


class IOSDevicePreflightScriptTests(unittest.TestCase):
    def test_physical_phone_reachability_rejects_loopback_urls(self):
        module = load_ios_device_preflight_module()

        self.assertFalse(module.is_physical_phone_reachable_url("http://127.0.0.1:8765"))
        self.assertFalse(module.is_physical_phone_reachable_url("http://localhost:8765"))
        self.assertFalse(module.is_physical_phone_reachable_url("http://[::1]:8765"))
        self.assertTrue(module.is_physical_phone_reachable_url("http://192.168.1.42:8765"))
        self.assertTrue(module.is_physical_phone_reachable_url("https://taste.example.com"))

    def test_preflight_reports_real_iphone_api_handshake_and_token_free_connect_page(self):
        module = load_ios_device_preflight_module()
        runner = RecordingRunner()

        with tempfile.TemporaryDirectory() as tmp:
            summary = module.run_ios_device_preflight(
                runner,
                root=Path(tmp),
                server_url="http://192.168.1.42:8765",
                token="server-token",
                project_id="ios_e2e_mobile_app",
                profile="Lab Mac",
                kind="computer",
            )

        commands = [call[0] for call in runner.calls]
        self.assertTrue(summary["ok"])
        self.assertTrue(summary["server_url_reachable_for_phone"])
        self.assertTrue(summary["physical_device_available"])
        self.assertEqual(summary["device_count"], 1)
        self.assertEqual(summary["devices"][0]["identifier"], "REAL-IPHONE-UDID")
        self.assertEqual(summary["api_smoke"]["checks"]["mobile_api_version"], 1)
        self.assertTrue(summary["api_smoke"]["checks"]["mobile_control_plane"])
        self.assertIn("/mobile/connect?", summary["connect_page_url"])
        self.assertNotIn("server-token", json.dumps(summary))
        self.assertNotIn("token=", summary["connect_page_url"])
        self.assertTrue(any(command[:4] == ("xcrun", "devicectl", "list", "devices") for command in commands))
        self.assertTrue(any(command[:3] == (sys.executable, "scripts/mobile_api_smoke.py", "--server-url") for command in commands))

    def test_preflight_blocks_when_no_physical_iphone_is_connected(self):
        module = load_ios_device_preflight_module()
        runner = RecordingRunner(devices_payload={"result": {"devices": []}})

        with tempfile.TemporaryDirectory() as tmp:
            summary = module.run_ios_device_preflight(
                runner,
                root=Path(tmp),
                server_url="http://192.168.1.42:8765",
                project_id="ios_e2e_mobile_app",
                skip_api_smoke=True,
            )

        self.assertFalse(summary["ok"])
        self.assertFalse(summary["physical_device_available"])
        self.assertIn("physical iPhone", " ".join(summary["blocking_items"]))

    def test_preflight_blocks_loopback_url_for_physical_iphone(self):
        module = load_ios_device_preflight_module()
        runner = RecordingRunner()

        with tempfile.TemporaryDirectory() as tmp:
            summary = module.run_ios_device_preflight(
                runner,
                root=Path(tmp),
                server_url="http://127.0.0.1:8765",
                project_id="ios_e2e_mobile_app",
                skip_api_smoke=True,
            )

        self.assertFalse(summary["ok"])
        self.assertFalse(summary["server_url_reachable_for_phone"])
        self.assertIn("127.0.0.1/localhost", " ".join(summary["blocking_items"]))

    def test_coredevice_iphone_with_disconnected_tunnel_is_still_available(self):
        module = load_ios_device_preflight_module()

        devices = module.parse_physical_ios_devices({
            "result": {
                "devices": [
                    {
                        "identifier": "COREDEVICE-ID",
                        "connectionProperties": {
                            "pairingState": "paired",
                            "transportType": "localNetwork",
                            "tunnelState": "disconnected",
                        },
                        "deviceProperties": {
                            "developerModeStatus": "enabled",
                            "name": "Hao iPhone",
                        },
                        "hardwareProperties": {
                            "deviceType": "iPhone",
                            "platform": "iOS",
                            "udid": "HARDWARE-UDID",
                        },
                    },
                ],
            },
        })

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["identifier"], "COREDEVICE-ID")
        self.assertEqual(devices[0]["udid"], "HARDWARE-UDID")
        self.assertEqual(devices[0]["xcode_destination_id"], "HARDWARE-UDID")
        self.assertEqual(devices[0]["name"], "Hao iPhone")


if __name__ == "__main__":
    unittest.main()
