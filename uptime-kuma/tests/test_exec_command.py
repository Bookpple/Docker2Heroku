"""Exercise the deployed command launcher with a real POSIX shell and Node."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


LAUNCHER = Path(__file__).resolve().parents[1] / "exec-command.sh"
SHELL = os.environ.get("KUMA_TEST_SHELL") or shutil.which("sh")
NODE = shutil.which("node")
PASSWORD = " literal $HOME $(printf expanded) `printf expanded` & quote'\"=value "


@unittest.skipUnless(SHELL and NODE, "A POSIX shell and Node are required for launcher integration tests")
class CommandLauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="kuma-command-test-")
        self.addCleanup(self.directory.cleanup)
        server = Path(self.directory.name, "server")
        server.mkdir()
        (server / "server.js").write_text(
            "console.log(JSON.stringify({args: process.argv.slice(2), "
            "port: process.env.PORT, password: process.env.UPTIME_KUMA_DB_PASSWORD}));\n",
            encoding="utf-8",
        )

    def run_command(self, *args):
        return subprocess.run(
            [SHELL, LAUNCHER.as_posix(), *args], cwd=self.directory.name,
            env=dict(os.environ, PORT="39189", UPTIME_KUMA_DB_PASSWORD=PASSWORD),
            text=True, encoding="utf-8", capture_output=True, timeout=15, check=False,
        )

    def assert_started(self, result, args):
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["args"], args)
        self.assertEqual(payload["port"], "39189")
        self.assertEqual(payload["password"], PASSWORD)

    def test_heroku_default_single_command_string(self):
        self.assert_started(self.run_command("node server/server.js"), [])

    def test_docker_argument_vector_stays_literal(self):
        self.assert_started(self.run_command("node", "server/server.js", PASSWORD, ""), [PASSWORD, ""])

    def test_heroku_quoted_arguments_stay_literal(self):
        command = shlex.join(["node", "server/server.js", PASSWORD, ""])
        self.assert_started(self.run_command(command), [PASSWORD, ""])

    def test_command_failure_exit_code_is_preserved(self):
        for args in (("/bin/sh -c 'exit 23'",), ("/bin/sh", "-c", "exit 23")):
            with self.subTest(args=args):
                result = self.run_command(*args)
                self.assertEqual(result.returncode, 23, result.stderr)

    def test_missing_command_fails_explicitly(self):
        for args in ((), ("",)):
            with self.subTest(args=args):
                result = self.run_command(*args)
                self.assertEqual(result.returncode, 64)
                self.assertIn("startup command is required", result.stderr)


if __name__ == "__main__":
    unittest.main()
