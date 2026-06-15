import importlib.util
import json
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def load_ios_device_smoke_module():
    root = Path(__file__).resolve().parents[3]
    script = root / "scripts" / "ios_device_smoke.py"
    spec = importlib.util.spec_from_file_location("ios_device_smoke", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Completed:
    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = 0


class RecordingRunner:
    def __init__(self, *, devices_payload=None, api_payload=None, signing_identities="", xcode_team_defaults="", profile_payloads=None):
        self.calls = []
        self.devices_payload = devices_payload if devices_payload is not None else {
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
        }
        self.api_payload = api_payload if api_payload is not None else {
            "ok": True,
            "checks": {
                "mobile_api_version": 1,
                "mobile_control_plane": True,
            },
        }
        self.signing_identities = signing_identities
        self.xcode_team_defaults = xcode_team_defaults
        self.profile_payloads = profile_payloads or {}

    def run(self, args, cwd=None, check=True):
        self.calls.append((tuple(args), str(cwd) if cwd else "", check))
        if tuple(args) == ("security", "find-identity", "-v", "-p", "codesigning"):
            return Completed(self.signing_identities)
        if tuple(args) == ("defaults", "read", "com.apple.dt.Xcode", "IDEProvisioningTeamByIdentifier"):
            return Completed(self.xcode_team_defaults)
        if tuple(args[:4]) == ("security", "cms", "-D", "-i"):
            path = str(args[4])
            payload = self.profile_payloads.get(path, {})
            return Completed(plistlib.dumps(payload).decode("utf-8"))
        if tuple(args[:4]) == ("xcrun", "devicectl", "list", "devices"):
            return Completed(json.dumps(self.devices_payload))
        if tuple(args[:3]) == (sys.executable, "scripts/mobile_api_smoke.py", "--server-url"):
            return Completed(json.dumps(self.api_payload))
        return Completed("")


class IOSDeviceSmokeScriptTests(unittest.TestCase):
    def test_signing_readiness_reports_missing_matching_profile_before_build(self):
        module = load_ios_device_smoke_module()
        runner = RecordingRunner(
            signing_identities='  1) ABCDEF "Apple Development: Dev User (TEAM123456)"\n'
        )

        with tempfile.TemporaryDirectory() as tmp:
            readiness = module.collect_signing_readiness(
                runner,
                bundle_id="org.ustcstarteam.taste.mobile",
                development_team="",
                profiles_dir=Path(tmp),
            )

        self.assertFalse(readiness["ready"])
        self.assertTrue(readiness["identity_available"])
        self.assertEqual(readiness["development_team"], "TEAM123456")
        self.assertEqual(readiness["matching_profile_count"], 0)
        self.assertIn("provisioning profile", " ".join(readiness["blocking_items"]))

    def test_signing_readiness_reports_xcode_personal_team_but_still_requires_valid_identity(self):
        module = load_ios_device_smoke_module()
        runner = RecordingRunner(
            signing_identities="Policy: Code Signing\n  Matching identities\n     0 identities found\n",
            xcode_team_defaults='{"ANY" = ({teamID = 5B45TSV42B; teamName = "Hao Wang (Personal Team)"; teamType = "Personal Team";});}'
        )

        with tempfile.TemporaryDirectory() as tmp:
            readiness = module.collect_signing_readiness(
                runner,
                bundle_id="org.ustcstarteam.taste.mobile",
                development_team="",
                profiles_dir=Path(tmp),
            )

        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["identity_available"])
        self.assertEqual(readiness["development_team"], "5B45TSV42B")
        self.assertEqual(readiness["development_team_source"], "xcode_defaults")
        self.assertIn("valid Apple Development signing identity", " ".join(readiness["blocking_items"]))

    def test_signing_readiness_accepts_matching_development_profile(self):
        module = load_ios_device_smoke_module()
        runner = RecordingRunner(
            signing_identities='  1) ABCDEF "Apple Development: Dev User (TEAM123456)"\n'
        )

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "TASTE.mobileprovision"
            profile.touch()
            runner.profile_payloads[str(profile)] = {
                "Name": "TASTE Development",
                "TeamIdentifier": ["TEAM123456"],
                "Entitlements": {
                    "application-identifier": "TEAM123456.org.ustcstarteam.taste.mobile",
                    "get-task-allow": True,
                },
            }

            readiness = module.collect_signing_readiness(
                runner,
                bundle_id="org.ustcstarteam.taste.mobile",
                development_team="",
                profiles_dir=Path(tmp),
            )

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["development_team"], "TEAM123456")
        self.assertEqual(readiness["matching_profile_count"], 1)
        self.assertEqual(readiness["matching_profiles"][0]["name"], "TASTE Development")

    def test_signing_readiness_scans_xcode_user_profile_directory(self):
        module = load_ios_device_smoke_module()
        runner = RecordingRunner(
            signing_identities='  1) ABCDEF "Apple Development: Dev User (TEAM123456)"\n'
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_mobile_profiles = root / "MobileDevice" / "Provisioning Profiles"
            xcode_profiles = root / "Developer" / "Xcode" / "UserData" / "Provisioning Profiles"
            xcode_profiles.mkdir(parents=True)
            profile = xcode_profiles / "TASTE.mobileprovision"
            profile.touch()
            runner.profile_payloads[str(profile)] = {
                "Name": "TASTE Xcode Managed Profile",
                "TeamIdentifier": ["TEAM123456"],
                "Entitlements": {
                    "application-identifier": "TEAM123456.org.ustcstarteam.taste.mobile",
                    "get-task-allow": True,
                },
            }

            readiness = module.collect_signing_readiness(
                runner,
                bundle_id="org.ustcstarteam.taste.mobile",
                development_team="",
                profiles_dir=[missing_mobile_profiles, xcode_profiles],
            )

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["profile_count"], 1)
        self.assertEqual(readiness["matching_profiles"][0]["path"], str(profile))

    def test_device_smoke_builds_installs_launches_and_imports_connection_link(self):
        module = load_ios_device_smoke_module()
        runner = RecordingRunner()
        link = "taste://connect?server_url=http%3A%2F%2F192.168.1.42%3A8765&kind=computer&token=secret-token"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "apps" / "ios" / "TASTEApp"
            app_root.mkdir(parents=True)
            derived_data = root / "device-derived-data"

            summary = module.run_ios_device_smoke(
                runner,
                root=root,
                app_root=app_root,
                derived_data=derived_data,
                server_url="http://192.168.1.42:8765",
                token="server-token",
                project_id="ios_e2e_mobile_app",
                profile="Lab Mac",
                kind="computer",
                connection_link=link,
                development_team="TEAM123",
                allow_provisioning_updates=True,
            )

        commands = [call[0] for call in runner.calls]
        build_command = next(command for command in commands if command[:2] == ("xcodebuild", "-project"))
        install_command = next(command for command in commands if command[:5] == ("xcrun", "devicectl", "device", "install", "app"))
        launch_command = next(command for command in commands if command[:5] == ("xcrun", "devicectl", "device", "process", "launch"))

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["device"]["identifier"], "COREDEVICE-ID")
        self.assertEqual(summary["device"]["xcode_destination_id"], "HARDWARE-UDID")
        self.assertIn("-sdk", build_command)
        self.assertIn("iphoneos", build_command)
        self.assertIn("-destination", build_command)
        self.assertIn("id=HARDWARE-UDID", build_command)
        self.assertIn("-allowProvisioningUpdates", build_command)
        self.assertIn("DEVELOPMENT_TEAM=TEAM123", build_command)
        self.assertIn("--device", install_command)
        self.assertIn("COREDEVICE-ID", install_command)
        self.assertIn(str(derived_data / "Build" / "Products" / "Debug-iphoneos" / "TASTEApp.app"), install_command)
        self.assertIn("--payload-url", launch_command)
        self.assertIn(link, launch_command)
        self.assertIn(module.BUNDLE_ID, launch_command)
        self.assertNotIn("secret-token", json.dumps(summary))
        self.assertIn("token=REDACTED", summary["connection_link"])

    def test_device_smoke_can_return_signing_preflight_without_building(self):
        module = load_ios_device_smoke_module()
        runner = RecordingRunner(
            signing_identities='  1) ABCDEF "Apple Development: Dev User (TEAM123456)"\n'
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "apps" / "ios" / "TASTEApp"
            app_root.mkdir(parents=True)

            summary = module.run_ios_device_smoke(
                runner,
                root=root,
                app_root=app_root,
                derived_data=root / "device-derived-data",
                server_url="http://192.168.1.42:8765",
                project_id="ios_e2e_mobile_app",
                skip_api_smoke=True,
                signing_preflight_only=True,
                provisioning_profiles_dir=Path(tmp),
            )

        commands = [call[0] for call in runner.calls]
        self.assertFalse(summary["ok"])
        self.assertFalse(summary["signing_readiness"]["ready"])
        self.assertFalse(any(command[:2] == ("xcodebuild", "-project") for command in commands))

    def test_device_smoke_infers_development_team_from_codesigning_identity(self):
        module = load_ios_device_smoke_module()
        runner = RecordingRunner(
            signing_identities='  1) ABCDEF "Apple Development: Dev User (TEAM123456)"\n'
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "apps" / "ios" / "TASTEApp"
            app_root.mkdir(parents=True)

            summary = module.run_ios_device_smoke(
                runner,
                root=root,
                app_root=app_root,
                derived_data=root / "device-derived-data",
                server_url="http://192.168.1.42:8765",
                project_id="ios_e2e_mobile_app",
                skip_api_smoke=True,
            )

        commands = [call[0] for call in runner.calls]
        build_command = next(command for command in commands if command[:2] == ("xcodebuild", "-project"))
        self.assertEqual(summary["development_team"], "TEAM123456")
        self.assertEqual(summary["signing_readiness"]["development_team_source"], "code_signing_identity")
        self.assertIn("DEVELOPMENT_TEAM=TEAM123456", build_command)

    def test_device_smoke_allows_bundle_id_override_for_personal_signing(self):
        module = load_ios_device_smoke_module()
        runner = RecordingRunner()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "apps" / "ios" / "TASTEApp"
            app_root.mkdir(parents=True)

            summary = module.run_ios_device_smoke(
                runner,
                root=root,
                app_root=app_root,
                derived_data=root / "device-derived-data",
                server_url="http://192.168.1.42:8765",
                project_id="ios_e2e_mobile_app",
                bundle_id="com.example.TASTEApp.dev",
                skip_api_smoke=True,
            )

        commands = [call[0] for call in runner.calls]
        build_command = next(command for command in commands if command[:2] == ("xcodebuild", "-project"))
        launch_command = next(command for command in commands if command[:5] == ("xcrun", "devicectl", "device", "process", "launch"))
        self.assertEqual(summary["bundle_id"], "com.example.TASTEApp.dev")
        self.assertIn("PRODUCT_BUNDLE_IDENTIFIER=com.example.TASTEApp.dev", build_command)
        self.assertEqual(launch_command[-1], "com.example.TASTEApp.dev")

    def test_command_error_payload_summarizes_signing_failures_without_leaking_token(self):
        module = load_ios_device_smoke_module()
        exc = subprocess.CalledProcessError(
            65,
            [
                "xcodebuild",
                "build",
                "taste://connect?token=secret-token",
            ],
            output=(
                "TASTEApp.xcodeproj: error: No Account for Team \"TEAM123456\".\n"
                "TASTEApp.xcodeproj: error: No profiles for 'org.ustcstarteam.taste.mobile' were found.\n"
            ),
            stderr="",
        )

        payload = module.command_error_payload(exc, secrets=["secret-token"])

        serialized = json.dumps(payload)
        self.assertFalse(payload["ok"])
        self.assertIn("exit 65", payload["error"])
        self.assertIn("No Account for Team", payload["stdout_tail"])
        self.assertIn("provisioning", payload["signing_hint"])
        self.assertNotIn("secret-token", serialized)
        self.assertIn("REDACTED", serialized)

    def test_exit_code_tracks_summary_ok_status(self):
        module = load_ios_device_smoke_module()

        self.assertEqual(module.exit_code_for_summary({"ok": True}), 0)
        self.assertEqual(module.exit_code_for_summary({"ok": False}), 1)
        self.assertEqual(module.exit_code_for_summary({}), 1)

    def test_device_smoke_requires_a_physical_iphone(self):
        module = load_ios_device_smoke_module()
        runner = RecordingRunner(devices_payload={"result": {"devices": []}})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "apps" / "ios" / "TASTEApp"
            app_root.mkdir(parents=True)

            with self.assertRaisesRegex(RuntimeError, "physical iPhone"):
                module.run_ios_device_smoke(
                    runner,
                    root=root,
                    app_root=app_root,
                    derived_data=root / "device-derived-data",
                    server_url="http://192.168.1.42:8765",
                    project_id="ios_e2e_mobile_app",
                    skip_api_smoke=True,
                )


if __name__ == "__main__":
    unittest.main()
