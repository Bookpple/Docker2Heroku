import base64
import contextlib
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError


MODULE_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("kuma_deploy", MODULE_DIR / "deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)
CA = base64.b64encode((MODULE_DIR / "tests/fixtures/ca.pem").read_bytes()).decode("ascii")
DATABASE = {
    "UPTIME_KUMA_DB_TYPE": "mariadb",
    "UPTIME_KUMA_DB_SSL": "true",
    "UPTIME_KUMA_DB_HOSTNAME": "database.example.invalid",
    "UPTIME_KUMA_DB_PORT": "12345",
    "UPTIME_KUMA_DB_NAME": "kuma",
    "UPTIME_KUMA_DB_USERNAME": "test-user",
    "UPTIME_KUMA_DB_PASSWORD": " don't expand $HOME $(touch marker) `id` &=value ",
    "AIVEN_CA_BASE64": CA,
}


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        # Keep platform runtime settings (TEMP, SystemRoot, OpenSSL providers),
        # but isolate all Action inputs and GitHub output paths from the caller.
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("INPUT_", "GITHUB_"))}
        environment.update({
            "INPUT_HEROKU_API_KEY": "test-heroku-token",
            "INPUT_HEROKU_APP_NAME": "test-uptime-kuma",
            "INPUT_DOCKER_IMAGE": "louislam/uptime-kuma:2.5.6",
            "INPUT_HEROKU_CONFIG_VARS_JSON": json.dumps(DATABASE),
        })
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def instance(self):
        instance = deploy.Deployer()
        instance.config = deploy.effective_configuration({}, instance.changes)
        return instance

    def test_literal_configuration_never_runs_a_shell(self):
        text = "PASSWORD=don't expand $HOME $(touch marker) `id` &=value\nMESSAGE=hello world\nEMPTY=\n"
        with patch.object(deploy.subprocess, "run") as command:
            values = deploy.parse_config_vars(text)
        self.assertEqual(values["PASSWORD"], "don't expand $HOME $(touch marker) `id` &=value")
        self.assertEqual(values["MESSAGE"], "hello world")
        self.assertEqual(values["EMPTY"], "")
        command.assert_not_called()
        self.assertEqual(self.instance().config["UPTIME_KUMA_DB_PASSWORD"], DATABASE["UPTIME_KUMA_DB_PASSWORD"])

    def test_file_workflow_and_json_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "config.json").write_text(json.dumps({"CUSTOM": "file", "FILE_ONLY": "文件"}), encoding="utf-8-sig")
            os.environ.update(GITHUB_WORKSPACE=directory, INPUT_CONFIG_FILE="config.json",
                              INPUT_HEROKU_CONFIG_VARS="CUSTOM=workflow\nINLINE_ONLY=yes",
                              INPUT_HEROKU_CONFIG_VARS_JSON='{"CUSTOM":"secret-json"}')
            values = deploy.configuration()
        self.assertEqual(values["CUSTOM"], "secret-json")
        self.assertEqual(values["FILE_ONLY"], "文件")
        self.assertEqual(values["INLINE_ONLY"], "yes")

    def test_database_inputs_override_file_and_skip_empty_inputs(self):
        os.environ.update(INPUT_DB_PASSWORD="dedicated secret", INPUT_DB_HOSTNAME="")
        os.environ["INPUT_HEROKU_CONFIG_VARS_JSON"] = ""
        changes = deploy.configuration()
        result = deploy.effective_configuration(DATABASE, changes)
        self.assertEqual(result["UPTIME_KUMA_DB_PASSWORD"], "dedicated secret")
        self.assertEqual(result["UPTIME_KUMA_DB_HOSTNAME"], DATABASE["UPTIME_KUMA_DB_HOSTNAME"])

    def test_existing_config_survives_and_obsolete_flags_are_deleted(self):
        os.environ["INPUT_HEROKU_CONFIG_VARS_JSON"] = ""
        existing = dict(DATABASE, CUSTOM="keep", NODE_TLS_REJECT_UNAUTHORIZED="0",
                        UPTIME_KUMA_PORT="3001", UPTIME_KUMA_DB_CA="obsolete",
                        UPTIME_KUMA_DB_PASSWORD_FILE="/old/password")
        changes = deploy.configuration()
        result = deploy.effective_configuration(existing, changes)
        self.assertEqual(result["CUSTOM"], "keep")
        self.assertNotIn("NODE_TLS_REJECT_UNAUTHORIZED", result)
        self.assertTrue(all(key not in result for key in deploy.MANAGED_KEYS))
        self.assertIsNone(changes["UPTIME_KUMA_DB_CA"])

    def test_missing_database_fails_before_build_or_app_creation(self):
        os.environ.update(INPUT_CREATE_NEW_APP="true", INPUT_HEROKU_CONFIG_VARS_JSON="")
        instance = deploy.Deployer()
        instance.api = Mock(return_value=None)
        with self.assertRaisesRegex(deploy.DeployError, "UPTIME_KUMA_DB_HOSTNAME is required"):
            instance.preflight()
        instance.api.assert_called_once_with("GET", "/apps/test-uptime-kuma", missing_ok=True)

    def test_invalid_database_settings_fail(self):
        for key, value in (("UPTIME_KUMA_DB_TYPE", "sqlite"), ("UPTIME_KUMA_DB_SSL", "false"),
                           ("UPTIME_KUMA_DB_PORT", "0"), ("UPTIME_KUMA_DB_PORT", "65536"),
                           ("NODE_TLS_REJECT_UNAUTHORIZED", "0"), ("UPTIME_KUMA_DB_PASSWORD", None)):
            with self.subTest(key=key, value=value), self.assertRaises(deploy.DeployError):
                deploy.effective_configuration(DATABASE, {key: value})

    def test_ca_requires_real_pem_and_accepts_bundles(self):
        deploy.validate_ca(CA)
        pem = base64.b64decode(CA)
        deploy.validate_ca(base64.b64encode(pem + pem).decode("ascii"))
        deploy.validate_ca("\n" + CA + "\n")
        for value in ("not-base64!", base64.b64encode(b"not a cert").decode(),
                      base64.b64encode(b"-----BEGIN CERTIFICATE-----\nYWJj\n-----END CERTIFICATE-----").decode()):
            with self.subTest(value=value), self.assertRaises(deploy.DeployError):
                deploy.validate_ca(value)

    def test_reserved_configuration_cannot_override_entrypoint(self):
        for key in deploy.MANAGED_KEYS:
            with self.subTest(key=key), self.assertRaises(deploy.DeployError):
                deploy.parse_config_json(json.dumps({key: "override"}))
        for value in ('[]', '{"CUSTOM":true}', '{"BAD-KEY":"value"}', '{bad json}'):
            with self.subTest(value=value), self.assertRaises(deploy.DeployError):
                deploy.parse_config_json(value)

    def test_exact_v2_image_is_required(self):
        for image in ("louislam/uptime-kuma:2.5.6", "louislam/uptime-kuma:2.5.6-slim",
                      "louislam/uptime-kuma@sha256:" + "a" * 64):
            deploy.validate_image(image)
        for image in ("louislam/uptime-kuma:2", "louislam/uptime-kuma:latest", "louislam/uptime-kuma:3.0.0",
                      "--bad-option", "image@sha256:abc", "image:2.5.6;id", ""):
            with self.subTest(image=image), self.assertRaises(deploy.DeployError):
                deploy.validate_image(image)

    def test_eco_and_partial_docker_credentials_fail(self):
        for changes in ({"INPUT_DYNO_TYPE": "eco"}, {"INPUT_DOCKERHUB_USERNAME": "only-user"}):
            with patch.dict(os.environ, changes), self.assertRaises(deploy.DeployError):
                deploy.Deployer()

    def test_existing_non_container_or_worker_app_is_rejected(self):
        for app, formation in (({"stack": {"name": "heroku-24"}}, []),
                               ({"stack": {"name": "container"}}, [{"type": "worker", "quantity": 1}])):
            instance = self.instance()
            instance.api = Mock(side_effect=[app, formation])
            with self.assertRaises(deploy.DeployError):
                instance.preflight()

    def test_existing_app_is_reused_and_keeps_config(self):
        instance = self.instance()
        instance.api = Mock(side_effect=[{"stack": {"name": "container"}, "web_url": "https://app.invalid/"},
                                         [{"type": "web", "size": "basic", "quantity": 1}], DATABASE])
        instance.preflight()
        instance.run = Mock()
        instance.ensure_app()
        instance.run.assert_not_called()
        self.assertEqual(instance.app_url, "https://app.invalid/")

    def test_new_app_with_empty_dyno_type_uses_basic(self):
        os.environ.update(INPUT_CREATE_NEW_APP="true", INPUT_DYNO_TYPE="")
        instance = self.instance()
        instance.api = Mock(return_value=None)
        instance.preflight()
        self.assertEqual(instance.dyno_type, "basic")

    def test_build_smoke_is_offline_non_root_and_uses_environment(self):
        instance = self.instance()
        calls = []

        def run(args, **kwargs):
            calls.append((args, dict(instance.env)))
            if args[:2] == ["docker", "build"]:
                for name in ("Dockerfile", "heroku-entrypoint.sh"):
                    self.assertNotIn(b"\r\n", Path(args[-1], name).read_bytes())
            if args[:2] == ["docker", "run"]:
                return "KUMA_SMOKE_OK\n"
            if args[:2] == ["docker", "inspect"]:
                return "sha256:image-id\n"
            return ""

        instance.run = run
        original_env = instance.env
        instance.build()
        args, environment = next(call for call in calls if call[0][:2] == ["docker", "run"])
        self.assertIn("12345:12345", args)
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertEqual(environment["AIVEN_CA_BASE64"], CA)
        self.assertNotIn(CA, args)
        self.assertNotIn(DATABASE["UPTIME_KUMA_DB_PASSWORD"], args)
        self.assertEqual(environment["UPTIME_KUMA_DB_PASSWORD"], "smoke")
        self.assertEqual(environment["PORT"], "18543")
        self.assertEqual(environment["UPTIME_KUMA_PORT"], "3001")
        self.assertIs(instance.env, original_env)

    def test_release_pushes_then_stops_before_config_and_starts_one_dyno(self):
        instance = self.instance()
        calls = []
        instance.run = lambda args, **kwargs: calls.append(args)
        instance.stop_previous_web = lambda: calls.append(["stop"])

        def api(method, path, data=None, **kwargs):
            calls.append([method, path])
            return [{"id": "new-release"}] if path.endswith("/releases") else {}

        instance.api = api
        instance.deploy()
        self.assertEqual(calls[0][:2], ["docker", "push"])
        self.assertEqual(calls[1], ["stop"])
        self.assertEqual(calls[2][0], "PATCH")
        self.assertIn(["heroku", "ps:scale", "web=1:basic", "--app", instance.app_name], calls)
        self.assertEqual(instance.expected_release, "new-release")

    def test_stopping_waits_for_old_dyno_to_disappear(self):
        instance = self.instance()
        instance.formation = [{"type": "web", "quantity": 1}]
        instance.run = Mock()
        instance.api = Mock(side_effect=[[{"type": "web", "state": "stopping"}], []])
        with patch.object(deploy.time, "sleep"):
            instance.stop_previous_web()
        self.assertEqual(instance.api.call_count, 2)

    def test_health_requires_current_release_then_database_ready(self):
        instance = self.instance()
        instance.app_url = "https://app.invalid/"
        instance.expected_release = "new-release"
        instance.timeout = 30
        old = {"type": "web", "state": "up", "release": {"id": "old-release"}}
        new = {**old, "release": {"id": "new-release"}}
        instance.api = Mock(side_effect=[[old], [new], [new]])
        responses = []
        for payload in ({"runningSetup": False, "needSetup": True}, {"runningSetup": False, "needSetup": False}):
            response = Mock(status=200)
            response.read.return_value = json.dumps(payload).encode()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            responses.append(response)
        with patch.object(deploy.time, "sleep"), patch.object(deploy, "urlopen", side_effect=responses) as request:
            instance.wait()
        self.assertEqual(request.call_count, 2)
        self.assertEqual(instance.api.call_count, 3)
        self.assertEqual(request.call_args.args[0], "https://app.invalid/setup-database-info")

    def test_unhealthy_setup_or_multiple_web_dynos_times_out(self):
        for payload in ({"runningSetup": True, "needSetup": True}, {"runningSetup": 0, "needSetup": 0}, {}):
            self.assertFalse(deploy.database_ready(payload))
        instance = self.instance()
        instance.app_url = "https://app.invalid/"
        instance.expected_release = "new-release"
        instance.timeout = 10
        dyno = {"type": "web", "state": "up", "release": {"id": "new-release"}}
        instance.api = Mock(return_value=[dyno, dyno])
        with patch.object(deploy.time, "sleep"), patch.object(deploy.time, "monotonic", side_effect=itertools.count()), patch.object(deploy, "urlopen") as request:
            with self.assertRaisesRegex(deploy.DeployError, "did not become ready"):
                instance.wait()
        request.assert_not_called()

    def test_api_uses_json_and_never_prints_error_body(self):
        instance = self.instance()
        error = HTTPError("https://api.heroku.com", 422, "error", {}, io.BytesIO(b"sensitive response"))
        with patch.object(deploy, "urlopen", side_effect=error) as request:
            with self.assertRaisesRegex(deploy.DeployError, "HTTP 422") as raised:
                instance.api("PATCH", "/apps/test/config-vars", DATABASE)
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertEqual(json.loads(request.call_args.args[0].data), DATABASE)

    def test_diagnostics_mask_password_and_token(self):
        instance = self.instance()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            instance.log(DATABASE["UPTIME_KUMA_DB_PASSWORD"] + " test-heroku-token")
        self.assertNotIn(DATABASE["UPTIME_KUMA_DB_PASSWORD"], output.getvalue())
        self.assertNotIn("test-heroku-token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
