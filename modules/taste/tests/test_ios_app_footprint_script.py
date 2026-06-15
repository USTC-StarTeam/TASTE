import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def load_ios_app_footprint_module():
    root = Path(__file__).resolve().parents[3]
    script = root / "scripts" / "ios_app_footprint.py"
    spec = importlib.util.spec_from_file_location("ios_app_footprint", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_bytes(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


class IOSAppFootprintScriptTests(unittest.TestCase):
    def test_footprint_accepts_small_app_and_reports_phone_storage_budget(self):
        module = load_ios_app_footprint_module()

        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "TASTEApp.app"
            write_bytes(app / "TASTEApp", 1024)
            write_bytes(app / "Assets.car", 2048)

            summary = module.analyze_ios_app_footprint(
                app,
                max_bundle_bytes=10 * 1024,
                mobile_cache_budget_bytes=20 * 1024,
                max_total_phone_bytes=40 * 1024,
            )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["bundle_bytes"], 3072)
        self.assertEqual(summary["mobile_cache_budget_bytes"], 20 * 1024)
        self.assertEqual(summary["estimated_max_phone_bytes"], 3072 + 20 * 1024)
        self.assertEqual(summary["blocking_items"], [])

    def test_footprint_blocks_large_bundle_and_large_single_file(self):
        module = load_ios_app_footprint_module()

        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "TASTEApp.app"
            write_bytes(app / "TASTEApp", 7 * 1024)
            write_bytes(app / "BigAsset.bin", 6 * 1024)

            summary = module.analyze_ios_app_footprint(
                app,
                max_bundle_bytes=10 * 1024,
                max_single_file_bytes=5 * 1024,
                mobile_cache_budget_bytes=1 * 1024,
                max_total_phone_bytes=20 * 1024,
            )

        self.assertFalse(summary["ok"])
        self.assertGreater(summary["bundle_bytes"], summary["max_bundle_bytes"])
        self.assertEqual(summary["large_files"][0]["relative_path"], "BigAsset.bin")
        self.assertIn("bundle", " ".join(summary["blocking_items"]).lower())
        self.assertIn("single file", " ".join(summary["blocking_items"]).lower())

    def test_cli_returns_nonzero_when_footprint_exceeds_budget(self):
        module = load_ios_app_footprint_module()

        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "TASTEApp.app"
            write_bytes(app / "TASTEApp", 12 * 1024)

            exit_code, output = module.run_cli([
                "--app",
                str(app),
                "--max-bundle-mb",
                "0.01",
                "--max-total-phone-mb",
                "1",
            ])

        payload = json.loads(output)
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
