"""Deploy an AxonHub wrapper without passing config values through shell code."""

import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


DEFAULT_IMAGE = "looplj/axonhub@sha256:581c334e7d03f62acdf9f987181c17d18d01f18aca896522e6e826c34038f123"
CONFIG_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
IMAGE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*\Z")
DB_DIALECTS = {"postgres", "postgresql", "postgresdb", "pg", "pgx", "mysql", "tidb"}


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


def validate_config_key(key):
    if not isinstance(key, str) or not CONFIG_KEY.fullmatch(key):
        raise DeployError("Invalid configuration variable name")
    if key in {"PORT", "AXONHUB_SERVER_PORT", "AXONHUB_SERVER_HOST"}:
        raise DeployError(f"{key} is managed by the Heroku entrypoint; omit it from config variables")


def parse_config_vars(text):
    result = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # Original docker2heroku accepts several quoted KEY=VALUE pairs on
        # one line. Tokenize that format without expanding or executing it.
        try:
            assignments = shlex.split(line, comments=False, posix=True)
        except ValueError:
            # A single literal value may contain an unmatched apostrophe.
            # Preserve the existing one-assignment-per-line behavior below.
            assignments = []
        if len(assignments) > 1 and all("=" in assignment for assignment in assignments):
            for assignment in assignments:
                key, value = assignment.split("=", 1)
                validate_config_key(key)
                result[key] = value
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise DeployError(f"heroku_config_vars line {number} must use KEY=VALUE")
        key, value = key.strip(), value.strip()
        validate_config_key(key)
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = value[1:-1]
        result[key] = value
    return result


def parse_config_json(text):
    if not text.strip():
        return {}
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        raise DeployError("heroku_config_vars_json must be valid JSON") from None
    if not isinstance(result, dict):
        raise DeployError("heroku_config_vars_json must be a JSON object")
    for key, value in result.items():
        validate_config_key(key)
        if value is not None and not isinstance(value, str):
            raise DeployError(f"JSON value for {key} must be a string or null")
    return result


def configuration():
    values = {
        "AXONHUB_DB_DIALECT": read_input("db_dialect", "postgres").strip(),
        "AXONHUB_LOG_LEVEL": read_input("log_level", "info").strip(),
        "AXONHUB_LOG_ENCODING": read_input("log_encoding", "console").strip(),
        "AXONHUB_LOG_OUTPUT": "stdio",
    }
    if read_input("db_dsn"):
        values["AXONHUB_DB_DSN"] = read_input("db_dsn")
    values.update(parse_config_vars(read_input("heroku_config_vars")))
    values.update(parse_config_json(read_input("heroku_config_vars_json")))
    return values


def effective_configuration(existing, changes):
    result = dict(existing)
    for key, value in changes.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    dialect = result.get("AXONHUB_DB_DIALECT")
    if dialect in {"sqlite", "sqlite3"}:
        raise DeployError("SQLite is not persistent on Heroku; use a supported remote database")
    if dialect not in DB_DIALECTS:
        raise DeployError("AXONHUB_DB_DIALECT must be postgres/postgresql, a PostgreSQL alias, mysql or tidb; "
                          "check the heroku_config_vars format")
    if not result.get("AXONHUB_DB_DSN", "").strip():
        raise DeployError("Set db_dsn or AXONHUB_DB_DSN to a writable remote database before deploying")
    return result


class Deployer:
    def __init__(self):
        self.api_key = read_input("heroku_api_key").strip()
        if not self.api_key:
            raise DeployError("heroku_api_key is required")
        self.masked_values = set()
        self.mask(self.api_key)
        self.create = boolean("create_new_app", "false")
        self.app_name = read_input("heroku_app_name").strip()
        if not self.app_name:
            if not self.create:
                raise DeployError("heroku_app_name is required unless create_new_app is true")
            self.app_name = "axonhub-" + secrets.token_hex(3)
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,28}[a-z0-9]", self.app_name):
            raise DeployError("heroku_app_name must be 3-30 lowercase letters, digits or hyphens")
        self.source_image = read_input("docker_image", DEFAULT_IMAGE).strip()
        self.runtime_image = read_input("runtime_image", "debian:bookworm-slim").strip()
        for name, value in (("docker_image", self.source_image), ("runtime_image", self.runtime_image)):
            if not IMAGE_REF.fullmatch(value):
                raise DeployError(f"Invalid {name} image reference")
        self.region = read_input("heroku_region", "us").strip()
        if self.region not in {"us", "eu"}:
            raise DeployError("heroku_region must be us or eu")
        self.team = read_input("heroku_team").strip()
        if self.team and not re.fullmatch(r"[A-Za-z0-9-]+", self.team):
            raise DeployError("Invalid heroku_team")
        self.dyno_type = read_input("dyno_type").strip()
        if self.dyno_type and not re.fullmatch(r"[A-Za-z0-9-]+", self.dyno_type):
            raise DeployError("Invalid dyno_type")
        self.quantity = integer("web_dynos", "1", 1, 100)
        self.wait_for_health = boolean("wait_for_health", "true")
        self.timeout = integer("healthcheck_timeout", "180", 10, 1800)
        self.show_logs = boolean("show_logs", "true")
        self.log_lines = integer("log_lines", "200", 1, 1500)
        self.health_path = read_input("healthcheck_path", "/health").strip()
        if not self.health_path.startswith("/") or self.health_path.startswith("//"):
            raise DeployError("healthcheck_path must be a relative path starting with one slash")
        self.docker_username = read_input("dockerhub_username").strip()
        self.docker_token = read_input("dockerhub_token")
        if bool(self.docker_username) != bool(self.docker_token):
            raise DeployError("Pass both dockerhub_username and dockerhub_token, or neither")
        self.mask(self.docker_token)
        self.changes = configuration()
        for key, value in self.changes.items():
            if value and any(part in key.upper() for part in ("DSN", "SECRET", "PASSWORD", "TOKEN", "KEY")):
                self.mask(value)
        self.env = dict(os.environ, HEROKU_API_KEY=self.api_key, HEROKU_DISABLE_ANALYTICS="1")
        self.image = f"registry.heroku.com/{self.app_name}/web"
        self.app_available = False
        self.app_url = ""
        self.expected_release = None

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

    def log(self, text):
        for value in sorted(self.masked_values, key=len, reverse=True):
            text = text.replace(value, "***")
        print(text, flush=True)

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
        request = Request("https://api.heroku.com" + path, data=body, method=method,
                          headers=request_headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                content = response.read()
                return json.loads(content) if content else None
        except HTTPError as error:
            error.close()
            if missing_ok and error.code == 404:
                return None
            # Config API error bodies may contain user values; never print them.
            raise DeployError(f"Heroku API {method} {path} failed with HTTP {error.code}") from None
        except (URLError, TimeoutError, json.JSONDecodeError):
            raise DeployError(f"Heroku API {method} {path} could not be completed") from None

    def preflight(self):
        app = self.api("GET", f"/apps/{self.app_name}", missing_ok=True)
        self.app_available = app is not None
        if not app and not self.create:
            raise DeployError("App was not found. Set create_new_app=true for a new deployment")
        existing = self.api("GET", f"/apps/{self.app_name}/config-vars") if app else {}
        self.config = effective_configuration(existing, self.changes)
        self.mask(self.config["AXONHUB_DB_DSN"])
        if app:
            self.app_url = app.get("web_url") or ""
            self.log(f"Updating existing app: {self.app_name}")
        else:
            self.log(f"Preparing new app: {self.app_name}")

    def build(self):
        self.run(["docker", "login", "registry.heroku.com", "--username=_", "--password-stdin"],
                 stdin=self.api_key + "\n")
        if self.docker_username:
            self.run(["docker", "login", "docker.io", "--username", self.docker_username,
                      "--password-stdin"], stdin=self.docker_token + "\n")
        self.log(f"Building linux/amd64 image from {self.source_image}")
        with tempfile.TemporaryDirectory(prefix="axonhub-heroku-") as directory:
            for name in ("Dockerfile", "heroku-entrypoint.sh"):
                shutil.copyfile(Path(__file__).parent / name, Path(directory) / name)
            self.run(["docker", "build", "--pull", "--platform", "linux/amd64",
                      "--build-arg", "AXONHUB_IMAGE=" + self.source_image,
                      "--build-arg", "RUNTIME_IMAGE=" + self.runtime_image,
                      "-t", self.image, directory])
        output = self.run(["docker", "run", "--rm", "--user", "12345:12345", "--network", "none",
                           "-e", "PORT=18543", "-e", "AXONHUB_SERVER_PORT=8090",
                           self.image, "config", "get", "server.port"], capture=True, timeout=60)
        if not output.strip().splitlines() or output.strip().splitlines()[-1] != "18543":
            raise DeployError("Non-root smoke check failed: PORT was not mapped to AXONHUB_SERVER_PORT")
        self.log("Non-root startup and dynamic port smoke check passed")
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

    def deploy(self):
        self.api("PATCH", f"/apps/{self.app_name}/config-vars", self.changes)
        self.log(f"Configured {len(self.changes)} variables; values are omitted")
        self.run(["docker", "push", self.image])
        self.run(["heroku", "container:release", "web", "--app", self.app_name])
        scale = f"web={self.quantity}" + (":" + self.dyno_type if self.dyno_type else "")
        self.run(["heroku", "ps:scale", scale, "--app", self.app_name])
        # Only accept dynos from the current release, not an old healthy instance.
        releases = self.api("GET", f"/apps/{self.app_name}/releases",
                            headers={"Range": "version ..; order=desc,max=1;"})
        if not releases or not releases[0].get("id"):
            raise DeployError("Could not determine the release to check")
        self.expected_release = releases[0]["id"]

    def wait(self):
        if not self.wait_for_health:
            self.log("Health check skipped by wait_for_health=false")
            return
        if not self.app_url:
            raise DeployError("Heroku did not return an app URL for the health check")
        base = self.config.get("AXONHUB_SERVER_BASE_PATH", "").strip("/")
        url = self.app_url.rstrip("/") + ("/" + base if base else "") + self.health_path
        self.log(f"Waiting up to {self.timeout}s for web dynos and {url}")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            dynos = self.api("GET", f"/apps/{self.app_name}/dynos",
                             timeout=max(1, min(10, deadline - time.monotonic())))
            web = [dyno for dyno in dynos if dyno.get("type") == "web"]
            ready = len(web) >= self.quantity and all(
                dyno.get("state") == "up" and (
                    not self.expected_release or dyno.get("release", {}).get("id") == self.expected_release
                ) for dyno in web
            )
            if ready:
                try:
                    with urlopen(url, timeout=max(1, min(10, deadline - time.monotonic()))) as response:
                        if response.status == 200:
                            self.log("Deployment health check passed")
                            return
                except HTTPError as error:
                    error.close()
                except (URLError, TimeoutError):
                    pass
            time.sleep(max(0, min(5, deadline - time.monotonic())))
        raise DeployError("Deployment did not become healthy before the timeout; inspect the runtime logs")

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
                summary.write(f"### AxonHub deployment\n\nApp: `{self.app_name}`\n\nURL: {self.app_url}\n\n")

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
