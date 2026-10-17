"""Build and deploy Uptime Kuma without interpolating configuration into shell code."""

import base64
import binascii
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


CONFIG_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
IMAGE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*\Z")
MANAGED_KEYS = {
    "PORT", "UPTIME_KUMA_PORT", "UPTIME_KUMA_HOST", "DATA_DIR",
    "UPTIME_KUMA_DB_CA", "UPTIME_KUMA_DB_CA_FILE", "UPTIME_KUMA_DB_SSL_FILE",
    "UPTIME_KUMA_DB_USERNAME_FILE", "UPTIME_KUMA_DB_PASSWORD_FILE", "UPTIME_KUMA_DB_SOCKET",
}
DATABASE_INPUTS = {
    "db_hostname": "UPTIME_KUMA_DB_HOSTNAME",
    "db_port": "UPTIME_KUMA_DB_PORT",
    "db_name": "UPTIME_KUMA_DB_NAME",
    "db_username": "UPTIME_KUMA_DB_USERNAME",
    "db_password": "UPTIME_KUMA_DB_PASSWORD",
    "aiven_ca_base64": "AIVEN_CA_BASE64",
}
SMOKE_SCRIPT = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const tls = require('node:tls');
const config = require('./server/config');
assert.equal(config.port, 18543);
assert.equal(config.hostname, '0.0.0.0');
assert.match(require('./package.json').version, /^2\./);
assert.equal(process.env.DATA_DIR, '/tmp/uptime-kuma');
assert.equal(process.env.UPTIME_KUMA_DB_SSL, 'true');
assert.equal(process.env.NODE_TLS_REJECT_UNAUTHORIZED, undefined);
assert.equal(process.env.UPTIME_KUMA_DB_CA, undefined);
const ca = fs.readFileSync(process.env.UPTIME_KUMA_DB_CA_FILE, 'utf8');
tls.createSecureContext({ca});
const Database = require('./server/database');
const { SetupDatabase } = require('./server/setup-database');
Database.initDataDir({});
assert.equal(new SetupDatabase({}, {}).isNeedSetup(), false);
const saved = Database.readDBConfig();
assert.equal(saved.type, 'mariadb');
assert.equal(saved.ssl, true);
assert.equal(saved.ca.trim(), ca.trim());
fs.writeFileSync(process.env.DATA_DIR + '/permission-check', 'ok');
assert.ok(fs.statSync(process.env.DATA_DIR + '/upload').isDirectory());
console.log('KUMA_SMOKE_OK');
"""


class DeployError(Exception):
    pass


def read_input(name, default=""):
    return os.environ.get("INPUT_" + name.upper(), default)


def boolean(name, default):
    value = read_input(name, default).strip().lower()
    if value not in {"true", "false"}:
        raise DeployError(f"{name} must be true or false")
    return value == "true"


def integer(name, default, minimum, maximum):
    try:
        value = int(read_input(name, default))
    except ValueError:
        raise DeployError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise DeployError(f"{name} must be between {minimum} and {maximum}")
    return value


def validate_key(key):
    if not isinstance(key, str) or not CONFIG_KEY.fullmatch(key):
        raise DeployError("Invalid configuration variable name")
    if key in MANAGED_KEYS:
        raise DeployError(f"{key} is managed by the entrypoint; use AIVEN_CA_BASE64 for the CA")


def parse_config_json(text, source="heroku_config_vars_json"):
    if not text.strip():
        return {}
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        raise DeployError(f"{source} must be valid JSON") from None
    if not isinstance(result, dict):
        raise DeployError(f"{source} must be a JSON object")
    for key, value in result.items():
        validate_key(key)
        if value is not None and not isinstance(value, str):
            raise DeployError(f"JSON value for {key} must be a string or null")
    return result


def parse_config_vars(text):
    result = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise DeployError(f"heroku_config_vars line {number} must use KEY=VALUE")
        key, value = key.strip(), value.strip()
        validate_key(key)
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = value[1:-1]
        result[key] = value
    return result


def configuration():
    values = {"NODE_ENV": "production", "UPTIME_KUMA_DB_TYPE": "mariadb",
              "UPTIME_KUMA_DB_SSL": "true", "NODE_TLS_REJECT_UNAUTHORIZED": None}
    config_file = read_input("config_file").strip()
    if config_file:
        path = Path(config_file)
        if not path.is_absolute():
            path = Path(os.environ.get("GITHUB_WORKSPACE", os.getcwd())) / path
        try:
            contents = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            raise DeployError("Could not read config_file as UTF-8 JSON") from None
        values.update(parse_config_json(contents, "config_file"))
    for input_name, key in DATABASE_INPUTS.items():
        value = read_input(input_name)
        if value:
            values[key] = value
    values.update(parse_config_vars(read_input("heroku_config_vars")))
    values.update(parse_config_json(read_input("heroku_config_vars_json")))
    # Delete obsolete file/socket/port settings left over from previous deployments.
    values.update({key: None for key in MANAGED_KEYS})
    return values


def validate_ca(value):
    try:
        pem = base64.b64decode("".join(value.split()), validate=True).decode("ascii")
        if "-----BEGIN CERTIFICATE-----" not in pem:
            raise ValueError
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=pem)
    except (ValueError, binascii.Error, UnicodeError, ssl.SSLError):
        raise DeployError("AIVEN_CA_BASE64 must decode to a valid PEM CA certificate or CA bundle") from None


def effective_configuration(existing, changes):
    result = dict(existing)
    for key, value in changes.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    if result.get("UPTIME_KUMA_DB_TYPE") != "mariadb":
        raise DeployError("UPTIME_KUMA_DB_TYPE must be mariadb for the external Aiven MySQL database")
    if result.get("UPTIME_KUMA_DB_SSL", "").lower() != "true":
        raise DeployError("UPTIME_KUMA_DB_SSL must be true")
    if result.get("NODE_TLS_REJECT_UNAUTHORIZED") not in {None, "1"}:
        raise DeployError("Remove NODE_TLS_REJECT_UNAUTHORIZED=0; the Aiven CA enables certificate verification")
    for key in DATABASE_INPUTS.values():
        if not result.get(key, "").strip():
            raise DeployError(f"{key} is required; supply it in workflow inputs, config, or existing Heroku vars")
    port = result["UPTIME_KUMA_DB_PORT"]
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise DeployError("UPTIME_KUMA_DB_PORT must be between 1 and 65535")
    validate_ca(result["AIVEN_CA_BASE64"])
    return result


def validate_image(image):
    if not IMAGE_REF.fullmatch(image):
        raise DeployError("docker_image must be an exact Uptime Kuma v2 tag or sha256 digest")
    if re.search(r"@sha256:[a-fA-F0-9]{64}\Z", image):
        return
    if not re.search(r":2\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?\Z", image):
        raise DeployError("Use an exact v2 version tag or sha256 digest, not a floating :2 or :latest")


def database_ready(payload):
    return (isinstance(payload, dict) and payload.get("runningSetup") is False
            and payload.get("needSetup") is False)


class Deployer:
    def __init__(self):
        self.masked_values = set()
        self.api_key = read_input("heroku_api_key").strip()
        if not self.api_key:
            raise DeployError("heroku_api_key is required")
        self.mask(self.api_key)
        self.create = boolean("create_new_app", "false")
        self.app_name = read_input("heroku_app_name").strip()
        if not self.app_name:
            if not self.create:
                raise DeployError("heroku_app_name is required unless create_new_app=true")
            self.app_name = "uptime-kuma-" + secrets.token_hex(3)
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,28}[a-z0-9]", self.app_name):
            raise DeployError("heroku_app_name must be 3-30 lowercase letters, digits or hyphens")
        self.source_image = read_input("docker_image").strip()
        validate_image(self.source_image)
        self.region = read_input("heroku_region", "eu").strip()
        if self.region not in {"eu", "us"}:
            raise DeployError("heroku_region must be eu or us")
        self.team = read_input("heroku_team").strip()
        self.dyno_type = read_input("dyno_type", "basic").strip()
        for name, value in (("heroku_team", self.team), ("dyno_type", self.dyno_type)):
            if value and not re.fullmatch(r"[A-Za-z0-9-]+", value):
                raise DeployError(f"Invalid {name}")
        if self.dyno_type.lower() == "eco":
            raise DeployError("Eco dynos sleep and cannot run continuous Uptime Kuma monitoring; use basic or higher")
        self.wait_for_health = boolean("wait_for_health", "true")
        self.timeout = integer("healthcheck_timeout", "300", 10, 1800)
        self.show_logs = boolean("show_logs", "true")
        self.log_lines = integer("log_lines", "100", 1, 1500)
        self.docker_username = read_input("dockerhub_username").strip()
        self.docker_token = read_input("dockerhub_token")
        if bool(self.docker_username) != bool(self.docker_token):
            raise DeployError("Pass both dockerhub_username and dockerhub_token, or neither")
        self.mask(self.docker_token)
        self.changes = configuration()
        self.mask_config(self.changes)
        self.env = dict(os.environ, HEROKU_API_KEY=self.api_key, HEROKU_DISABLE_ANALYTICS="1")
        self.image = f"registry.heroku.com/{self.app_name}/web"
        self.image_id = ""
        self.app_available = False
        self.app_url = ""
        self.expected_release = None
        self.formation = []

    def mask(self, value):
        if not value or value in self.masked_values:
            return
        self.masked_values.add(value)
        escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print("::add-mask::" + escaped, flush=True)
        if "://" in value:
            try:
                password = urlsplit(value).password
                if password:
                    self.mask(password)
                    self.mask(unquote(password))
            except ValueError:
                pass

    def mask_config(self, config):
        for key, value in config.items():
            if value and any(part in key.upper() for part in ("SECRET", "PASSWORD", "TOKEN", "KEY", "BASE64", "USERNAME", "DSN")):
                self.mask(value)

    def log(self, message):
        for value in sorted(self.masked_values, key=len, reverse=True):
            message = message.replace(value, "***")
        print(message, flush=True)

    def run(self, args, *, stdin=None, capture=False, timeout=900):
        try:
            result = subprocess.run(args, input=stdin, text=True, encoding="utf-8", errors="replace",
                                    capture_output=True, env=self.env, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise DeployError(f"Could not complete {args[0]} {args[1]}") from None
        if result.returncode:
            self.log(result.stdout + result.stderr)
            raise DeployError(f"{args[0]} {args[1]} failed with exit code {result.returncode}")
        if not capture:
            self.log(result.stdout + result.stderr)
        return result.stdout

    def api(self, method, path, data=None, *, missing_ok=False, headers=None, timeout=30):
        body = None if data is None else json.dumps(data).encode("utf-8")
        request_headers = {"Authorization": "Bearer " + self.api_key,
                           "Accept": "application/vnd.heroku+json; version=3",
                           "Content-Type": "application/json"}
        request_headers.update(headers or {})
        request = Request("https://api.heroku.com" + path, data=body, method=method, headers=request_headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                content = response.read()
                return json.loads(content) if content else None
        except HTTPError as error:
            error.close()
            if missing_ok and error.code == 404:
                return None
            raise DeployError(f"Heroku API {method} {path} failed with HTTP {error.code}") from None
        except (URLError, TimeoutError, json.JSONDecodeError):
            raise DeployError(f"Heroku API {method} {path} could not be completed") from None

    def preflight(self):
        app = self.api("GET", f"/apps/{self.app_name}", missing_ok=True)
        if not app and not self.create:
            raise DeployError("App not found; use create_new_app=true for a new deployment")
        if app:
            stack = (app.get("stack") or {}).get("name")
            if stack != "container":
                raise DeployError("The existing app must use the container stack; create a dedicated container app")
            self.formation = self.api("GET", f"/apps/{self.app_name}/formation")
            if any(item.get("type") != "web" and item.get("quantity", 0) for item in self.formation):
                raise DeployError("Use a dedicated app without active worker or other process types")
            if not self.dyno_type:
                web = next((item for item in self.formation if item.get("type") == "web"), {})
                if web.get("size", "").lower() == "eco":
                    raise DeployError("The existing web dyno is Eco; set dyno_type=basic or higher")
                if not web:
                    self.dyno_type = "basic"
        elif not self.dyno_type:
            self.dyno_type = "basic"
        existing = self.api("GET", f"/apps/{self.app_name}/config-vars") if app else {}
        self.mask_config(existing)
        self.config = effective_configuration(existing, self.changes)
        self.mask_config(self.config)
        self.app_available = app is not None
        self.app_url = (app.get("web_url") or "") if app else ""
        self.log(("Updating existing app: " if app else "Preparing new app: ") + self.app_name)

    def build(self):
        self.run(["docker", "login", "registry.heroku.com", "--username=_", "--password-stdin"], stdin=self.api_key + "\n")
        if self.docker_username:
            self.run(["docker", "login", "docker.io", "--username", self.docker_username,
                      "--password-stdin"], stdin=self.docker_token + "\n")
        self.log("Building linux/amd64 image from " + self.source_image)
        with tempfile.TemporaryDirectory(prefix="uptime-kuma-heroku-") as directory:
            for name in ("Dockerfile", "heroku-entrypoint.sh", "exec-command.sh"):
                source = Path(__file__).parent / name
                # Normalize CRLF even when a caller has core.autocrlf enabled.
                Path(directory, name).write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
            self.run(["docker", "build", "--pull", "--platform", "linux/amd64",
                      "--build-arg", "UPTIME_KUMA_IMAGE=" + self.source_image, "-t", self.image, directory])
        smoke_env = dict(self.env, PORT="18543", UPTIME_KUMA_PORT="3001",
                         UPTIME_KUMA_DB_HOSTNAME="offline.invalid", UPTIME_KUMA_DB_PORT="3306",
                         UPTIME_KUMA_DB_NAME="smoke", UPTIME_KUMA_DB_USERNAME="smoke",
                         UPTIME_KUMA_DB_PASSWORD="smoke", UPTIME_KUMA_DB_TYPE="mariadb",
                         AIVEN_CA_BASE64=self.config["AIVEN_CA_BASE64"],
                         NODE_TLS_REJECT_UNAUTHORIZED="0", UPTIME_KUMA_DB_CA="obsolete")
        args = ["docker", "run", "--rm", "--user", "12345:12345", "--network", "none"]
        for key in ("PORT", "UPTIME_KUMA_PORT", *DATABASE_INPUTS.values(), "UPTIME_KUMA_DB_TYPE",
                    "NODE_TLS_REJECT_UNAUTHORIZED", "UPTIME_KUMA_DB_CA"):
            args.extend(["-e", key])
        command = ["node", "-e", SMOKE_SCRIPT]
        original_env = self.env
        try:
            self.env = smoke_env
            for mode, startup in (("Docker argv", command), ("Heroku command string", [shlex.join(command)])):
                output = self.run([*args, self.image, *startup], capture=True, timeout=60)
                if "KUMA_SMOKE_OK" not in output.splitlines():
                    raise DeployError(f"{mode} startup, CA, writable-directory or dynamic-port smoke check failed")
        finally:
            self.env = original_env
        self.log("Docker argv and Heroku command string smoke checks passed: non-root v2 runtime, dynamic port, writable directory and CA (offline)")
        self.image_id = self.run(["docker", "inspect", "--format={{.Id}}", self.image], capture=True).strip()

    def ensure_app(self):
        if self.app_available:
            return
        command = ["heroku", "create", self.app_name, "--stack", "container", "--region", self.region]
        if self.team:
            command.extend(["--team", self.team])
        self.run(command)
        self.app_available = True
        app = self.api("GET", f"/apps/{self.app_name}")
        self.app_url = app.get("web_url") or ""

    def stop_previous_web(self):
        if not any(item.get("type") == "web" for item in self.formation):
            return
        self.log("Stopping the previous web process before updating configuration; monitoring will briefly pause")
        self.run(["heroku", "ps:scale", "web=0", "--app", self.app_name])
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            dynos = self.api("GET", f"/apps/{self.app_name}/dynos")
            if not any(dyno.get("type") == "web" for dyno in dynos):
                return
            time.sleep(2)
        raise DeployError("Previous web dynos did not stop; no configuration or release was changed")

    def deploy(self):
        # Push and smoke-check before stopping the running monitor.
        self.run(["docker", "push", self.image])
        self.stop_previous_web()
        self.api("PATCH", f"/apps/{self.app_name}/config-vars", self.changes)
        self.log("Configured Heroku variables; values are omitted")
        self.run(["heroku", "container:release", "web", "--app", self.app_name])
        scale = "web=1" + (":" + self.dyno_type if self.dyno_type else "")
        self.run(["heroku", "ps:scale", scale, "--app", self.app_name])
        releases = self.api("GET", f"/apps/{self.app_name}/releases",
                            headers={"Range": "version ..; order=desc,max=1;"})
        if not releases or not releases[0].get("id"):
            raise DeployError("Could not identify the deployment release")
        self.expected_release = releases[0]["id"]

    def wait(self):
        if not self.wait_for_health:
            self.log("Health check skipped; release success does not confirm database connectivity")
            return
        if not self.app_url or not self.expected_release:
            raise DeployError("App URL and current release are required for the health check")
        url = self.app_url.rstrip("/") + "/setup-database-info"
        self.log(f"Waiting up to {self.timeout}s for the current release and database initialization")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            dynos = self.api("GET", f"/apps/{self.app_name}/dynos",
                             timeout=max(1, min(10, deadline - time.monotonic())))
            web = [dyno for dyno in dynos if dyno.get("type") == "web"]
            if (len(web) == 1 and web[0].get("state") == "up"
                    and web[0].get("release", {}).get("id") == self.expected_release):
                try:
                    with urlopen(url, timeout=max(1, min(10, deadline - time.monotonic()))) as response:
                        if response.status == 200 and database_ready(json.loads(response.read(16384))):
                            self.log("Deployment health check passed: current release and database initialization ready")
                            return
                except HTTPError as error:
                    error.close()
                except (URLError, TimeoutError, ValueError, UnicodeError):
                    pass
            time.sleep(max(0, min(5, deadline - time.monotonic())))
        raise DeployError("Deployment did not become ready; inspect logs for TLS, database permissions or R10 boot timeout")

    def outputs(self):
        values = {"app_name": self.app_name, "app_url": self.app_url,
                  "image": self.image, "image_id": self.image_id}
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                for key, value in values.items():
                    if "\n" in value or "\r" in value:
                        raise DeployError(f"Invalid {key} output")
                    output.write(f"{key}={value}\n")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
                summary.write(f"### Uptime Kuma deployment\n\nApp: `{self.app_name}`\n\nURL: {self.app_url}\n\n")
                summary.write(f"Source: `{self.source_image}`\n\nImage: `{self.image_id}`\n\n")
                summary.write("Health: " + ("database initialization ready" if self.wait_for_health else "not checked") + "\n")

    def diagnostics(self):
        if not self.show_logs or not self.app_available:
            return
        for args in (["heroku", "ps", "--app", self.app_name],
                     ["heroku", "logs", "--app", self.app_name, "--num", str(self.log_lines)]):
            try:
                self.run(args, timeout=60)
            except DeployError as error:
                self.log(f"Diagnostics unavailable: {error}")


def main():
    deployer = None
    try:
        if sys.platform != "linux":
            raise DeployError("Run this action on an Ubuntu/Linux runner with Docker and Python 3")
        deployer = Deployer()
        deployer.preflight()
        deployer.build()
        deployer.ensure_app()
        deployer.deploy()
        deployer.wait()
        deployer.outputs()
        return 0
    except DeployError as error:
        message = f"::error::{error}"
        if deployer:
            deployer.log(message)
        else:
            print(message, flush=True)
        return 1
    finally:
        if deployer:
            deployer.diagnostics()


if __name__ == "__main__":
    sys.exit(main())
