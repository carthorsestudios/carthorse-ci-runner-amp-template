#!/usr/bin/env python3
"""Cart Horse CI self-hosted GitHub Actions runner supervisor.

The supervisor keeps exactly one repository-scoped ephemeral runner available.
Each accepted job runs from a fresh directory with a fresh HOME, temp directory,
work directory, and tool cache. The entire job directory is deleted afterwards.
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
USER_AGENT = "carthorse-ci-runner/1"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
PR_SET_DUMPABLE = 4

ROOT = Path.cwd().resolve()
CONTROL_DIR = ROOT / "control"
STATE_DIR = CONTROL_DIR / "state"
CACHE_DIR = CONTROL_DIR / "cache"
CYCLES_DIR = CONTROL_DIR / "cycles"

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
    token = os.environ.get("CARTHORSE_GITHUB_TOKEN", "").strip()
    labels_raw = _env("CARTHORSE_RUNNER_LABELS", "carthorse-ci")
    prefix = _env("CARTHORSE_RUNNER_NAME_PREFIX", "carthorse-ci")
    version = _env("CARTHORSE_RUNNER_VERSION", "latest")
    restart_delay_raw = _env("CARTHORSE_RESTART_DELAY_SECONDS", "3")

    if not REPO_RE.fullmatch(repository):
        raise ConfigError("GitHub repository must be in owner/name form")
    if not token:
        raise ConfigError("GitHub runner administration token is required")
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
        "token": token,
        "labels": labels,
        "prefix": prefix,
        "version": version,
        "restart_delay": restart_delay,
    }


def _harden_supervisor_process() -> None:
    """Prevent same-UID job processes from reading the supervisor's memory/environ."""
    if sys.platform != "linux":
        raise RuntimeError("Cart Horse CI currently supports Linux only")
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    if result != 0:
        err = ctypes.get_errno()
        raise RuntimeError(f"Unable to harden supervisor process with prctl: errno={err}")


def _child_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key == "CARTHORSE_GITHUB_TOKEN" or key.endswith("_GITHUB_TOKEN"):
            env.pop(key, None)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    return env


def _api_request(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            if not body:
                return None
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise ApiError(f"GitHub API {method} {url} failed with HTTP {exc.code}: {detail}") from exc
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


def _cleanup_cycle_directories() -> None:
    CYCLES_DIR.mkdir(parents=True, exist_ok=True)
    for child in CYCLES_DIR.iterdir():
        if child.is_symlink():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _repo_api(repository: str, suffix: str) -> str:
    return f"{API_ROOT}/repos/{repository}/{suffix.lstrip('/')}"


def _registration_token(repository: str, token: str) -> str:
    payload = _api_request(_repo_api(repository, "actions/runners/registration-token"), token=token, method="POST")
    value = str((payload or {}).get("token", ""))
    if not value:
        raise ApiError("GitHub did not return a runner registration token")
    return value


def _delete_runner(repository: str, token: str, runner_id: int) -> None:
    _api_request(_repo_api(repository, f"actions/runners/{runner_id}"), token=token, method="DELETE")


def _cleanup_offline_registrations(repository: str, token: str, owned_prefix: str) -> int:
    deleted = 0
    page = 1
    while page <= 10:
        payload = _api_request(
            _repo_api(repository, f"actions/runners?per_page=100&page={page}"),
            token=token,
        )
        runners = list((payload or {}).get("runners", []))
        for runner in runners:
            name = str(runner.get("name", ""))
            if name.startswith(owned_prefix) and runner.get("status") == "offline":
                _delete_runner(repository, token, int(runner["id"]))
                deleted += 1
        if len(runners) < 100:
            break
        page += 1
    return deleted


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


def _prepare_cycle(
    archive: Path,
    *,
    runner_name: str,
    repository: str,
    registration_token: str,
    labels: list[str],
) -> tuple[Path, dict[str, str]]:
    cycle = CYCLES_DIR / runner_name
    if cycle.exists() or cycle.is_symlink():
        raise RuntimeError(f"Cycle directory already exists: {cycle}")
    runner_dir = cycle / "runner"
    _safe_extract_tar(archive, runner_dir)

    home = cycle / "home"
    tmp = cycle / "tmp"
    tool_cache = cycle / "toolcache"
    work = cycle / "work"
    for path in (home, tmp, tool_cache, work):
        path.mkdir(parents=True, exist_ok=True)

    child_env = _child_environment()
    child_env.update(
        {
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "RUNNER_TEMP": str(tmp),
            "RUNNER_TOOL_CACHE": str(tool_cache),
            "AGENT_TOOLSDIRECTORY": str(tool_cache),
        }
    )
    if os.geteuid() == 0:
        child_env["RUNNER_ALLOW_RUNASROOT"] = "1"

    config_args = [
        "./config.sh",
        "--unattended",
        "--ephemeral",
        "--disableupdate",
        "--url",
        f"https://github.com/{repository}",
        "--token",
        registration_token,
        "--name",
        runner_name,
        "--labels",
        ",".join(labels),
        "--work",
        str(work),
    ]
    rc = _run_command(config_args, cwd=runner_dir, env=child_env)
    if rc != 0:
        raise RuntimeError(f"Runner configuration failed with exit code {rc}")
    return cycle, child_env


def _remove_cycle(cycle: Path) -> None:
    if not cycle.exists() and not cycle.is_symlink():
        return
    resolved_parent = cycle.parent.resolve()
    if resolved_parent != CYCLES_DIR.resolve():
        raise RuntimeError("Refusing to clean a cycle outside the controlled directory")
    if cycle.is_symlink():
        cycle.unlink()
    else:
        shutil.rmtree(cycle)


def main() -> int:
    global STOP_REQUESTED
    config = _require_config()
    _harden_supervisor_process()

    # Remove the long-lived PAT from child environments immediately. The local
    # Python variable remains protected by PR_SET_DUMPABLE while the supervisor lives.
    os.environ.pop("CARTHORSE_GITHUB_TOKEN", None)

    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_cycle_directories()
    instance_id = _persistent_instance_id()
    owned_prefix = f"{config['prefix']}-{instance_id}-"

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    deleted = _cleanup_offline_registrations(config["repository"], config["token"], owned_prefix)
    if deleted:
        log(f"Removed {deleted} stale offline runner registration(s)")

    counter = 0
    release_cache: tuple[str, dict[str, Any]] | None = None
    release_cache_at = 0.0
    while not STOP_REQUESTED:
        cycle: Path | None = None
        try:
            now = time.monotonic()
            if (
                release_cache is None
                or (config["version"] == "latest" and now - release_cache_at >= 3600)
            ):
                release_cache = _runner_release(config["version"])
                release_cache_at = now
            version, metadata = release_cache
            asset_url, asset_sha = _select_runner_asset(version, metadata)
            archive = CACHE_DIR / "runner" / version / Path(asset_url).name
            _download_verified(asset_url, asset_sha, archive)

            counter += 1
            runner_name = f"{owned_prefix}{counter:06d}"
            registration_token = _registration_token(config["repository"], config["token"])
            cycle, child_env = _prepare_cycle(
                archive,
                runner_name=runner_name,
                repository=config["repository"],
                registration_token=registration_token,
                labels=config["labels"],
            )
            runner_dir = cycle / "runner"
            log(
                f"Ready repository={config['repository']} runner={runner_name} "
                f"version={version} labels={','.join(config['labels'])}"
            )
            rc = _run_command(["./run.sh"], cwd=runner_dir, env=child_env)
            if STOP_REQUESTED:
                break
            log(f"Ephemeral runner exited with code {rc}; preparing a fresh worker")
        except (ConfigError, ApiError, RuntimeError, OSError, tarfile.TarError) as exc:
            if STOP_REQUESTED:
                break
            log(f"ERROR: {exc}")
        finally:
            if cycle is not None:
                try:
                    _remove_cycle(cycle)
                except Exception as cleanup_exc:  # last-resort visibility; do not hide primary failure
                    log(f"ERROR: cycle cleanup failed: {cleanup_exc}")

        if not STOP_REQUESTED:
            time.sleep(config["restart_delay"])

    # Ephemeral runners normally deregister themselves after one job. On a stop,
    # remove only offline registrations owned by this AMP instance.
    try:
        _cleanup_offline_registrations(config["repository"], config["token"], owned_prefix)
    except Exception as exc:
        log(f"WARNING: final stale-runner cleanup failed: {exc}")
    log("Stopped")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        log(f"CONFIG ERROR: {exc}")
        raise SystemExit(2)
