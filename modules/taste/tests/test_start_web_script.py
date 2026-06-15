import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class StartWebScriptTests(unittest.TestCase):
    def test_api_only_mode_skips_frontend_build_when_npm_is_unavailable(self):
        root = Path(__file__).resolve().parents[3]
        script = root / "scripts" / "start_web.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            tools = Path(temp_dir)
            (tools / "dirname").symlink_to("/usr/bin/dirname")
            env = {
                **os.environ,
                "PATH": str(tools),
                "WEB_API_ONLY": "1",
                "MANAGEMENT_PYTHON": "/bin/echo",
                "WEB_HOST": "127.0.0.1",
                "WEB_PORT": "9876",
            }

            result = subprocess.run(
                ["/bin/bash", str(script)],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-m uvicorn auto_research.web.server:app --host 127.0.0.1 --port 9876", result.stdout)
        self.assertNotIn("npm not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
