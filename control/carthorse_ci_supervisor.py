#!/usr/bin/env python3
"""Cart Horse CI persistent repository-scoped GitHub Actions runner supervisor.

The runner is registered once with a time-limited GitHub registration token.
Each invocation of run.sh uses --once so one job is accepted, then the job
workspace, HOME, temp directory, and tool cache are destroyed before the next
job. No long-lived repository-administration credential is required.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

API_ROOT = "https://api.github.com"
RUNNER_RELEASE_API = f"{API_ROOT}/repos/actions/runner/releases"
USER_AGENT = "carthorse-ci-runner/2"
SUPERVISOR_REVISION = "6"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
PR_SET_DUMPABLE = 4
BASE_TOOLS = ("bash", "git", "tar", "xz", "python3", "curl", "sha256sum", "zip", "unzip", "ps")

ROOT = Path.cwd().resolve()
CONTROL_DIR = ROOT / "control"
STATE_DIR = CONTROL_DIR / "state"
CACHE_DIR = CONTROL_DIR / "cache"
RUNNER_DIR = CONTROL_DIR / "runner"
WORK_DIR = RUNNER_DIR / "_work"
JOBS_DIR = CONTROL_DIR / "jobs"

STOP_REQUESTED = False
ACTIVE_PROCESS: subprocess.Popen[str] | None = None


class ConfigError(RuntimeError):
    pass


class ApiError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[CartHorseCI] {message}", flush=True)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _require_config() -> dict[str, Any]:
    repository = _env("CARTHORSE_GITHUB_REPOSITORY")
    registration_token = os.environ.get("CARTHORSE_REGISTRATION_TOKEN", "").strip()
    labels_raw = _env("CARTHORSE_RUNNER_LABELS", "carthorse-ci")
    prefix = _env("CARTHORSE_RUNNER_NAME_PREFIX", "carthorse-ci")
    version = _env("CARTHORSE_RUNNER_VERSION", "latest")
    restart_delay_raw = _env("CARTHORSE_RESTART_DELAY_SECONDS", "3")

    if not REPO_RE.fullmatch(repository):
        raise ConfigError("GitHub repository must be in owner/name form")
    labels = [item.strip() for item in labels_raw.split(",") if item.strip()]
    if not labels:
        raise ConfigError("At least one custom runner label is required")
    if any(not LABEL_RE.fullmatch(item) for item in labels):
        raise ConfigError("Runner labels may contain only letters, numbers, dot, underscore, and hyphen")
    if not PREFIX_RE.fullmatch(prefix):
        raise ConfigError("Runner name prefix may contain only letters, numbers, dot, underscore, and hyphen")
    if version != "latest" and not VERSION_RE.fullmatch(version):
        raise ConfigError("Runner version must be 'latest' or N.N.N")
    try:
        restart_delay = int(restart_delay_raw)
    except ValueError as exc:
        raise ConfigError("Restart delay must be an integer") from exc
    if not 1 <= restart_delay <= 60:
        raise ConfigError("Restart delay must be between 1 and 60 seconds")

    return {
        "repository": repository,
        "registration_token": registration_token,
        "labels": labels,
        "prefix": prefix,
        "version": version,
        "restart_delay": restart_delay,
    }


def _harden_supervisor_process() -> None:
    """Prevent same-UID job processes from reading supervisor memory/environment."""
    if sys.platform != "linux":
        raise RuntimeError("Cart Horse CI currently supports Linux only")
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    if result != 0:
        err = ctypes.get_errno()
        raise RuntimeError(f"Unable to harden supervisor process with prctl: errno={err}")


def _require_base_tools() -> None:
    missing = [tool for tool in BASE_TOOLS if shutil.which(tool) is None]
    if missing:
        raise ConfigError(
            "Missing required CI base tools: "
            + ", ".join(missing)
            + ". Fetch the latest AMP template and recreate/update the CI container before running jobs."
        )
    tools_text = ",".join(BASE_TOOLS)\n    log(f"Base tools OK revision={SUPERVISOR_REVISION} tools={tools_text}")


def _child_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key == "CARTHORSE_REGISTRATION_TOKEN" or key.endswith("_GITHUB_TOKEN"):
            env.pop(key, None)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    return env


def _api_request(url: str, *, timeout: int = 30) -> Any:
    """Public GitHub API request. No repository administration credential is used."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            if not body:
                return None
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise ApiError(f"GitHub API GET {url} failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"GitHub API request failed: {exc.reason}") from exc


def _runner_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "x64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    raise RuntimeError(f"Unsupported runner architecture: {machine}")


def _runner_release(version_setting: str) -> tuple[str, dict[str, Any]]:
    if version_setting == "latest":
        metadata = _api_request(f"{RUNNER_RELEASE_API}/latest")
    else:
        metadata = _api_request(f"{RUNNER_RELEASE_API}/tags/v{version_setting}")
    if not isinstance(metadata, dict):
        raise RuntimeError("Invalid GitHub Actions runner release metadata")
    tag = str(metadata.get("tag_name", ""))
    if not tag.startswith("v") or not VERSION_RE.fullmatch(tag[1:]):
        raise RuntimeError("Runner release metadata contains an invalid tag")
    return tag[1:], metadata


def _select_runner_asset(version: str, metadata: dict[str, Any]) -> tuple[str, str]:
    arch = _runner_arch()
    expected_name = f"actions-runner-linux-{arch}-{version}.tar.gz"
    for asset in metadata.get("assets", []):
        if asset.get("name") != expected_name:
            continue
        url = str(asset.get("browser_download_url", ""))
        digest = str(asset.get("digest", ""))
        if not url.startswith("https://github.com/actions/runner/releases/download/"):
            raise RuntimeError("Runner asset URL is not the official actions/runner release URL")
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise RuntimeError("Runner asset is missing an authoritative SHA-256 digest")
        return url, digest.split(":", 1)[1]
    raise RuntimeError(f"Could not find official runner asset {expected_name}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_verified(url: str, expected_sha256: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and _sha256(destination) == expected_sha256:
        return
    tmp_fd, tmp_name = tempfile.mkstemp(prefix="runner-", suffix=".download", dir=destination.parent)
    os.close(tmp_fd)
    tmp = Path(tmp_name)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        digest = hashlib.sha256()
        with urllib.request.urlopen(request, timeout=120) as response, tmp.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                output.write(chunk)
        got = digest.hexdigest()
        if got != expected_sha256:
            raise RuntimeError(f"Runner archive SHA-256 mismatch: expected {expected_sha256}, got {got}")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"Unsafe path in runner archive: {member.name}")
            if member.issym():
                link_target = (target.parent / member.linkname).resolve()
                if link_target != root and root not in link_target.parents:
                    raise RuntimeError(f"Unsafe symlink in runner archive: {member.name}")
            elif member.islnk():
                link_target = (destination / member.linkname).resolve()
                if link_target != root and root not in link_target.parents:
                    raise RuntimeError(f"Unsafe hardlink in runner archive: {member.name}")
        bundle.extractall(destination)


def _persistent_instance_id() -> str:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "instance_id"
    if path.is_symlink():
        raise RuntimeError("Refusing symlinked instance id state")
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{12}", value):
            return value
        raise RuntimeError("Invalid persisted instance id")
    value = uuid.uuid4().hex[:12]
    temp = path.with_suffix(".tmp")
    temp.write_text(value + "\n", encoding="utf-8")
    os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temp, path)
    return value


def _configured() -> bool:
    required = [
        RUNNER_DIR / "run.sh",
        RUNNER_DIR / "config.sh",
        RUNNER_DIR / ".runner",
        RUNNER_DIR / ".credentials",
        RUNNER_DIR / ".credentials_rsaparams",
    ]
    return all(path.is_file() and not path.is_symlink() for path in required)


def _registration_fingerprint() -> dict[str, str]:
    if not _configured():
        raise RuntimeError("Runner registration state is incomplete")
    result: dict[str, str] = {}
    for name in (".runner", ".credentials", ".credentials_rsaparams"):
        result[name] = _sha256(RUNNER_DIR / name)
    return result


def _assert_registration_unchanged(expected: dict[str, str]) -> None:
    current = _registration_fingerprint()
    if current != expected:
        raise RuntimeError(
            "Runner registration files changed during a job. Refusing to continue; "
            "delete/recreate this CI instance and register it again."
        )


def _remove_tree(path: Path, *, allowed_parent: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    resolved_parent = path.parent.resolve()
    if resolved_parent != allowed_parent.resolve():
        raise RuntimeError(f"Refusing to clean path outside controlled parent: {path}")
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _scrub_work() -> None:
    _remove_tree(WORK_DIR, allowed_parent=RUNNER_DIR)
    WORK_DIR.mkdir(parents=True, exist_ok=True)


def _scrub_jobs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    for child in list(JOBS_DIR.iterdir()):
        _remove_tree(child, allowed_parent=JOBS_DIR)


def _signal_handler(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    log(f"Stop requested by signal {signum}")
    process = ACTIVE_PROCESS
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass


def _run_command(args: list[str], *, cwd: Path, env: dict[str, str]) -> int:
    global ACTIVE_PROCESS
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        text=True,
        start_new_session=True,
    )
    ACTIVE_PROCESS = process
    try:
        return process.wait()
    finally:
        ACTIVE_PROCESS = None


def _install_and_register(config: dict[str, Any], runner_name: str) -> None:
    token = config["registration_token"]
    if not token:
        raise ConfigError(
            "Runner is not registered. Paste the one-hour registration token from "
            "GitHub repository Settings > Actions > Runners > New self-hosted runner "
            "into AMP's Initial Registration Token field, save, and start again."
        )

    if RUNNER_DIR.exists() or RUNNER_DIR.is_symlink():
        if RUNNER_DIR.is_symlink():
            raise RuntimeError("Refusing symlinked runner directory")
        shutil.rmtree(RUNNER_DIR)

    version, metadata = _runner_release(config["version"])
    asset_url, asset_sha = _select_runner_asset(version, metadata)
    archive = CACHE_DIR / "runner" / version / Path(asset_url).name
    _download_verified(asset_url, asset_sha, archive)
    _safe_extract_tar(archive, RUNNER_DIR)

    child_env = _child_environment()
    if os.geteuid() == 0:
        child_env["RUNNER_ALLOW_RUNASROOT"] = "1"
    rc = _run_command(
        [
            "./config.sh",
            "--unattended",
            "--replace",
            "--url",
            f"https://github.com/{config['repository']}",
            "--token",
            token,
            "--name",
            runner_name,
            "--labels",
            ",".join(config["labels"]),
            "--work",
            "_work",
        ],
        cwd=RUNNER_DIR,
        env=child_env,
    )
    if rc != 0 or not _configured():
        raise RuntimeError(f"Runner registration failed with exit code {rc}")
    log(
        f"Registered repository={config['repository']} runner={runner_name} "
        f"version={version} labels={','.join(config['labels'])}"
    )
    log("Registration complete. The one-hour setup token is no longer needed; clear it from AMP.")


def _fresh_job_environment(sequence: int) -> tuple[Path, dict[str, str]]:
    cycle = JOBS_DIR / f"job-{sequence:06d}"
    if cycle.exists() or cycle.is_symlink():
        raise RuntimeError(f"Job directory already exists: {cycle}")
    home = cycle / "home"
    tmp = cycle / "tmp"
    tool_cache = cycle / "toolcache"
    for path in (home, tmp, tool_cache):
        path.mkdir(parents=True, exist_ok=True)

    env = _child_environment()
    env.update(
        {
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "RUNNER_TEMP": str(tmp),
            "RUNNER_TOOL_CACHE": str(tool_cache),
            "AGENT_TOOLSDIRECTORY": str(tool_cache),
        }
    )
    if os.geteuid() == 0:
        env["RUNNER_ALLOW_RUNASROOT"] = "1"
    return cycle, env


def main() -> int:
    global STOP_REQUESTED
    config = _require_config()
    _harden_supervisor_process()
    _require_base_tools()

    # A GitHub registration token is single-purpose and time-limited. Remove it
    # from the process environment before any runner/job process is started.
    os.environ.pop("CARTHORSE_REGISTRATION_TOKEN", None)

    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _scrub_jobs()
    instance_id = _persistent_instance_id()
    runner_name = f"{config['prefix']}-{instance_id}"

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    if not _configured():
        _install_and_register(config, runner_name)
    config["registration_token"] = ""

    # The persistent registration credential files are not repository-admin
    # credentials. Still, fail closed if a job tampers with them.
    registration_fingerprint = _registration_fingerprint()
    _scrub_work()

    sequence = 0
    while not STOP_REQUESTED:
        cycle: Path | None = None
        try:
            sequence += 1
            cycle, child_env = _fresh_job_environment(sequence)
            log(
                f"Ready repository={config['repository']} runner={runner_name} "
                f"labels={','.join(config['labels'])}"
            )
            rc = _run_command(["./run.sh", "--once"], cwd=RUNNER_DIR, env=child_env)
            _assert_registration_unchanged(registration_fingerprint)
            _scrub_work()
            if STOP_REQUESTED:
                break
            log(f"Single-job runner exited with code {rc}; workspace scrubbed")
        except (ApiError, RuntimeError, OSError, tarfile.TarError) as exc:
            if STOP_REQUESTED:
                break
            log(f"ERROR: {exc}")
            # Registration tampering is fail-closed: do not automatically resume.
            if "registration" in str(exc).lower():
                return 3
        finally:
            if cycle is not None:
                try:
                    _remove_tree(cycle, allowed_parent=JOBS_DIR)
                except Exception as cleanup_exc:
                    log(f"ERROR: job environment cleanup failed: {cleanup_exc}")
                    return 4

        if not STOP_REQUESTED:
            time.sleep(config["restart_delay"])

    try:
        _scrub_work()
        _scrub_jobs()
    except Exception as exc:
        log(f"WARNING: final workspace cleanup failed: {exc}")
    log("Stopped")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        log(f"CONFIG ERROR: {exc}")
        raise SystemExit(2)
