import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deploy


DSN = "postgres://user:pa%24ss@db.example/app?sslmode=require"


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.environ = patch.dict(os.environ, {
            "INPUT_HEROKU_API_KEY": "test-heroku-token",
            "INPUT_HEROKU_APP_NAME": "test-axonhub",
            "INPUT_DB_DSN": DSN,
        }, clear=True)
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def test_config_values_remain_literal(self):
        values = deploy.parse_config_vars(
            '# comment\nMESSAGE="spaces $HOME $(touch sentinel) & a=b"\nEMPTY=\n'
            "DSN='postgres://u:p@host/db?sslmode=require&x=y'\n"
        )
        self.assertEqual(values["MESSAGE"], "spaces $HOME $(touch sentinel) & a=b")
        self.assertEqual(values["EMPTY"], "")
        self.assertEqual(values["DSN"], "postgres://u:p@host/db?sslmode=require&x=y")

    def test_original_action_single_line_format_passes_database_preflight(self):
        os.environ["INPUT_CREATE_NEW_APP"] = "true"
        os.environ["INPUT_DB_DSN"] = ""
        os.environ["INPUT_HEROKU_CONFIG_VARS"] = (
            f'AXONHUB_DB_DIALECT="postgresql" AXONHUB_DB_DSN="{DSN}" '
            'AXONHUB_LOG_LEVEL="info" AXONHUB_LOG_OUTPUT="stdio" AXONHUB_LOG_ENCODING="console"'
        )
        instance = deploy.Deployer()
        instance.api = Mock(return_value=None)
        instance.preflight()
        self.assertEqual(instance.config["AXONHUB_DB_DIALECT"], "postgresql")
        self.assertEqual(instance.config["AXONHUB_DB_DSN"], DSN)
        self.assertEqual(len(instance.changes), 5)

    def test_legacy_values_preserve_quoted_special_characters_without_execution(self):
        text = 'MESSAGE="spaces $HOME $(touch sentinel) & a=b" TOKEN=\'a "quote" $value\' EMPTY=""'
        with patch.object(deploy.subprocess, "run") as command:
            values = deploy.parse_config_vars(text)
        self.assertEqual(values["MESSAGE"], "spaces $HOME $(touch sentinel) & a=b")
        self.assertEqual(values["TOKEN"], 'a "quote" $value')
        self.assertEqual(values["EMPTY"], "")
        command.assert_not_called()

    def test_legacy_and_multiline_configuration_can_be_mixed(self):
        values = deploy.parse_config_vars('A=one B="two words"\nA=final\nMESSAGE=unquoted words\n')
        self.assertEqual(values, {"A": "final", "B": "two words", "MESSAGE": "unquoted words"})

    def test_single_assignment_keeps_literal_backslashes_and_apostrophes(self):
        values = deploy.parse_config_vars("PATH_VALUE=C:\\folder\\file\nMESSAGE=don't change this\n")
        self.assertEqual(values["PATH_VALUE"], "C:\\folder\\file")
        self.assertEqual(values["MESSAGE"], "don't change this")

    def test_legacy_assignments_also_validate_every_key(self):
        for text in ('A=one PORT=8090', 'A=one 2BAD=value'):
            with self.subTest(text=text), self.assertRaises(deploy.DeployError):
                deploy.parse_config_vars(text)

    def test_invalid_database_type_has_specific_message_without_echoing_values(self):
        with self.assertRaises(deploy.DeployError) as raised:
            deploy.effective_configuration({}, {"AXONHUB_DB_DIALECT": "not-a-dialect " + DSN})
        message = str(raised.exception)
        self.assertIn("AXONHUB_DB_DIALECT", message)
        self.assertNotIn("SQLite", message)
        self.assertNotIn(DSN, message)

    def test_invalid_config_does_not_echo_secret_line(self):
        with self.assertRaises(deploy.DeployError) as raised:
            deploy.parse_config_vars("secret-without-an-equals-sign")
        self.assertNotIn("secret-without", str(raised.exception))

    def test_json_supports_multiline_values_and_deletions(self):
        values = deploy.parse_config_json('{"MESSAGE":"line1\\nline2", "OLD":null}')
        self.assertEqual(values, {"MESSAGE": "line1\nline2", "OLD": None})
        for invalid in ('[]', '{"COUNT":1}', '{"FLAG":true}', '{"NESTED":{}}'):
            with self.subTest(invalid=invalid), self.assertRaises(deploy.DeployError):
                deploy.parse_config_json(invalid)

    def test_runtime_port_and_host_cannot_be_overridden(self):
        for key in ("PORT", "AXONHUB_SERVER_PORT", "AXONHUB_SERVER_HOST"):
            with self.subTest(key=key), self.assertRaises(deploy.DeployError):
                deploy.parse_config_vars(key + "=8090")
            with self.subTest(key=key), self.assertRaises(deploy.DeployError):
                deploy.parse_config_json(json.dumps({key: None}))

    def test_config_precedence_and_unrelated_existing_values(self):
        os.environ["INPUT_LOG_LEVEL"] = "warn"
        os.environ["INPUT_HEROKU_CONFIG_VARS"] = "AXONHUB_LOG_LEVEL=debug\nREMOVE_ME=old"
        os.environ["INPUT_HEROKU_CONFIG_VARS_JSON"] = '{"AXONHUB_LOG_LEVEL":"error","REMOVE_ME":null}'
        changes = deploy.configuration()
        config = deploy.effective_configuration({"KEEP_ME": "yes", "REMOVE_ME": "previous"}, changes)
        self.assertEqual(config["AXONHUB_LOG_LEVEL"], "error")
        self.assertEqual(config["KEEP_ME"], "yes")
        self.assertNotIn("REMOVE_ME", config)

    def test_database_is_required_and_cannot_be_deleted(self):
        with self.assertRaises(deploy.DeployError):
            deploy.effective_configuration({}, {"AXONHUB_DB_DIALECT": "postgres"})
        with self.assertRaises(deploy.DeployError):
            deploy.effective_configuration({"AXONHUB_DB_DSN": DSN},
                                           {"AXONHUB_DB_DIALECT": "postgres", "AXONHUB_DB_DSN": None})
        with self.assertRaises(deploy.DeployError):
            deploy.effective_configuration({}, {"AXONHUB_DB_DIALECT": "sqlite3", "AXONHUB_DB_DSN": "file:app.db"})

    def test_existing_app_reuses_dsn_and_is_not_created_again(self):
        os.environ["INPUT_DB_DSN"] = ""
        os.environ["INPUT_CREATE_NEW_APP"] = "true"
        instance = deploy.Deployer()
        instance.api = Mock(side_effect=[{"web_url": "https://app.example/"}, {"AXONHUB_DB_DSN": DSN}])
        instance.run = Mock()
        instance.preflight()
        instance.ensure_app()
        self.assertEqual(instance.config["AXONHUB_DB_DSN"], DSN)
        instance.run.assert_not_called()

    def test_new_app_requires_opt_in(self):
        instance = deploy.Deployer()
        instance.api = Mock(return_value=None)
        with self.assertRaises(deploy.DeployError):
            instance.preflight()

    def test_new_app_is_created_with_region_and_team(self):
        os.environ["INPUT_CREATE_NEW_APP"] = "true"
        os.environ["INPUT_HEROKU_REGION"] = "eu"
        os.environ["INPUT_HEROKU_TEAM"] = "example-team"
        instance = deploy.Deployer()
        instance.api = Mock(side_effect=[None, {"web_url": "https://app.example/"}])
        instance.run = Mock()
        instance.preflight()
        instance.ensure_app()
        instance.run.assert_called_once_with([
            "heroku", "create", "test-axonhub", "--stack", "container", "--region", "eu",
            "--team", "example-team",
        ])

    def test_missing_database_fails_before_app_creation(self):
        os.environ["INPUT_CREATE_NEW_APP"] = "true"
        os.environ["INPUT_DB_DSN"] = ""
        instance = deploy.Deployer()
        instance.api = Mock(return_value=None)
        instance.run = Mock()
        with self.assertRaises(deploy.DeployError):
            instance.preflight()
        instance.run.assert_not_called()

    def test_image_newline_injection_is_rejected(self):
        os.environ["INPUT_DOCKER_IMAGE"] = "looplj/axonhub:latest\nRUN echo injection"
        with self.assertRaises(deploy.DeployError):
            deploy.Deployer()

    def test_config_api_sends_json_and_does_not_echo_response_values(self):
        instance = deploy.Deployer()
        response = Mock()
        response.read.return_value = json.dumps({"AXONHUB_DB_DSN": DSN}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        stream = io.StringIO()
        with patch.object(deploy, "urlopen", return_value=response) as request, contextlib.redirect_stdout(stream):
            instance.api("PATCH", "/apps/test-axonhub/config-vars", {"AXONHUB_DB_DSN": DSN})
        sent = request.call_args.args[0]
        self.assertEqual(json.loads(sent.data), {"AXONHUB_DB_DSN": DSN})
        self.assertEqual(sent.method, "PATCH")
        self.assertNotIn(DSN, stream.getvalue())

    def test_api_404_is_optional_but_403_is_not(self):
        instance = deploy.Deployer()
        for status in (404, 403):
            error = HTTPError("https://api.heroku.com/apps/test-axonhub", status, "error", {},
                              io.BytesIO(b"a body containing test-heroku-token"))
            with patch.object(deploy, "urlopen", side_effect=error):
                if status == 404:
                    self.assertIsNone(instance.api("GET", "/apps/test-axonhub", missing_ok=True))
                else:
                    with self.assertRaises(deploy.DeployError) as raised:
                        instance.api("GET", "/apps/test-axonhub", missing_ok=True)
                    self.assertNotIn("test-heroku-token", str(raised.exception))

    def test_logs_redact_dsn_and_decoded_password(self):
        instance = deploy.Deployer()
        instance.mask(DSN)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            instance.log("DSN=" + DSN + " password=pa$ss token=test-heroku-token")
        self.assertNotIn(DSN, stream.getvalue())
        self.assertNotIn("pa$ss", stream.getvalue())
        self.assertNotIn("test-heroku-token", stream.getvalue())

    def test_subprocess_does_not_use_a_shell_or_put_login_token_in_argv(self):
        instance = deploy.Deployer()
        result = subprocess.CompletedProcess([], 0, "login ok", "")
        with patch.object(deploy.subprocess, "run", return_value=result) as command:
            instance.run(["docker", "login", "--password-stdin"], stdin=instance.api_key + "\n")
        self.assertNotIn(instance.api_key, " ".join(command.call_args.args[0]))
        self.assertFalse(command.call_args.kwargs.get("shell", False))
        self.assertEqual(command.call_args.kwargs["input"], "test-heroku-token\n")

    def test_bad_smoke_port_stops_before_inspection_or_publication(self):
        instance = deploy.Deployer()
        instance.run = Mock(side_effect=["login ok", "build ok", "shell ready\n8090\n"])
        with patch.object(deploy.tempfile, "TemporaryDirectory") as directory, \
                patch.object(deploy.shutil, "copyfile"), self.assertRaises(deploy.DeployError):
            directory.return_value.__enter__.return_value = "/tmp/axonhub-smoke-test"
            instance.build()
        self.assertEqual(instance.run.call_count, 3)
        smoke = instance.run.call_args.args[0]
        self.assertIn("12345:12345", smoke)
        self.assertIn("PORT=18543", smoke)
        self.assertIn("AXONHUB_SERVER_PORT=8090", smoke)
        self.assertEqual(smoke[-3:], ["config", "get", "server.port"])

    def test_release_scales_and_checks_newest_release(self):
        os.environ["INPUT_DYNO_TYPE"] = "basic"
        instance = deploy.Deployer()
        instance.run = Mock()
        instance.api = Mock(side_effect=[instance.changes, [{"version": 999, "id": "new-release"}]])
        instance.deploy()
        self.assertEqual(instance.expected_release, "new-release")
        self.assertEqual(instance.api.call_args.kwargs["headers"],
                         {"Range": "version ..; order=desc,max=1;"})
        calls = [call.args[0] for call in instance.run.call_args_list]
        self.assertEqual(calls[-1], ["heroku", "ps:scale", "web=1:basic", "--app", "test-axonhub"])
        self.assertNotIn(DSN, repr(calls))

    def test_health_http_failure_is_retried(self):
        instance = deploy.Deployer()
        instance.app_url = "https://app.example/"
        instance.config = {}
        instance.api = Mock(return_value=[{"type": "web", "state": "up"}])
        error = HTTPError("https://app.example/health", 503, "starting", {}, io.BytesIO(b"starting"))
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(deploy.time, "monotonic", return_value=0), \
                patch.object(deploy.time, "sleep"), \
                patch.object(deploy, "urlopen", side_effect=[error, response]) as request:
            instance.wait()
        self.assertEqual(request.call_count, 2)
        self.assertTrue(error.fp.closed)

    def test_health_check_waits_for_current_release_and_honors_base_path(self):
        instance = deploy.Deployer()
        instance.app_url = "https://app.example/"
        instance.config = {"AXONHUB_SERVER_BASE_PATH": "/gateway"}
        instance.expected_release = "new-release"
        old = [{"type": "web", "state": "up", "release": {"id": "old-release"}}]
        new = [{"type": "web", "state": "up", "release": {"id": "new-release"}}]
        instance.api = Mock(side_effect=[old, new])
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(deploy.time, "monotonic", return_value=0), \
                patch.object(deploy.time, "sleep") as sleep, \
                patch.object(deploy, "urlopen", return_value=response) as request:
            instance.wait()
        sleep.assert_called_once()
        self.assertEqual(request.call_args.args[0], "https://app.example/gateway/health")
        self.assertEqual(request.call_count, 1)

    def test_health_timeout_fails_deployment(self):
        os.environ["INPUT_HEALTHCHECK_TIMEOUT"] = "10"
        instance = deploy.Deployer()
        instance.app_url = "https://app.example/"
        instance.config = {}
        instance.api = Mock(return_value=[])
        with patch.object(deploy.time, "monotonic", side_effect=[0, 0, 0, 0, 11]), \
                patch.object(deploy.time, "sleep"), self.assertRaises(deploy.DeployError):
            instance.wait()

    def test_health_failure_still_collects_diagnostics_and_returns_failure(self):
        instance = Mock()
        instance.wait.side_effect = deploy.DeployError("health timeout")
        with patch.object(deploy.sys, "platform", "linux"), patch.object(deploy, "Deployer", return_value=instance):
            self.assertEqual(deploy.main(), 1)
        instance.outputs.assert_not_called()
        instance.diagnostics.assert_called_once()


if __name__ == "__main__":
    unittest.main()
