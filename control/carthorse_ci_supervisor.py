#!/usr/bin/env python3
"""Cart Horse CI persistent repository-scoped GitHub Actions runner supervisor.

The runner is registered once with a time-limited GitHub registration token.
Each invocation of run.sh uses --once so one job is accepted, then ordinary job
state is destroyed before the next job. Optional persistent trusted tools are
downloaded only by the supervisor, content-addressed, verified before use, and
fingerprinted again after every job.
"""

from __future__ import annotations

import base64
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
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

API_ROOT = "https://api.github.com"
RUNNER_RELEASE_API = f"{API_ROOT}/repos/actions/runner/releases"
USER_AGENT = "carthorse-ci-runner/3"
SUPERVISOR_REVISION = "9"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
TOOL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
TOOL_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
PLATFORM_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
PR_SET_DUMPABLE = 4
BASE_TOOLS = (
    "bash", "git", "tar", "xz", "gzip", "python3", "curl", "sha256sum",
    "zip", "unzip", "zstd", "ps", "find", "head", "install", "cp", "mv",
    "rm", "chmod", "mkdir", "stdbuf", "go",
)
MAX_TOOL_CONFIG_BYTES = 128 * 1024
MAX_TOOL_COUNT = 16
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
MAX_UNPACKED_BYTES = 16 * 1024 * 1024 * 1024

ROOT = Path.cwd().resolve()
CONTROL_DIR = ROOT / "control"
STATE_DIR = CONTROL_DIR / "state"
CACHE_DIR = CONTROL_DIR / "cache"
RUNNER_DIR = CONTROL_DIR / "runner"
WORK_DIR = RUNNER_DIR / "_work"
JOBS_DIR = CONTROL_DIR / "jobs"
TRUSTED_DIR = CONTROL_DIR / "trusted-tools"
TRUSTED_OBJECTS_DIR = TRUSTED_DIR / "objects" / "sha256"
TRUSTED_REFS_DIR = TRUSTED_DIR / "refs"
TRUSTED_DOWNLOADS_DIR = TRUSTED_DIR / "downloads"
TRUSTED_STAGING_DIR = TRUSTED_DIR / "staging"
TRUSTED_QUARANTINE_DIR = TRUSTED_DIR / "quarantine"

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
    trusted_tools_b64 = _env("CARTHORSE_TRUSTED_TOOLS_B64")
    trusted_retention_raw = _env("CARTHORSE_TRUSTED_TOOL_RETENTION", "2")

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
    try:
        trusted_retention = int(trusted_retention_raw)
    except ValueError as exc:
        raise ConfigError("Trusted tool retention must be an integer") from exc
    if not 1 <= trusted_retention <= 5:
        raise ConfigError("Trusted tool retention must be between 1 and 5")

    trusted_tools = _parse_trusted_tools(trusted_tools_b64)
    return {
        "repository": repository,
        "registration_token": registration_token,
        "labels": labels,
        "prefix": prefix,
        "version": version,
        "restart_delay": restart_delay,
        "trusted_tools": trusted_tools,
        "trusted_retention": trusted_retention,
    }


def _parse_trusted_tools(encoded: str) -> list[dict[str, Any]]:
    if not encoded:
        return []
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ConfigError(f"Trusted tools value is not valid base64: {exc}") from exc
    if len(raw) > MAX_TOOL_CONFIG_BYTES:
        raise ConfigError("Trusted tools configuration is too large")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Trusted tools value is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(parsed, list) or len(parsed) > MAX_TOOL_COUNT:
        raise ConfigError(f"Trusted tools must be a JSON array with at most {MAX_TOOL_COUNT} entries")

    result: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ConfigError(f"Trusted tool entry {index} must be an object")
        required = {"name", "version", "platform", "url", "sha256", "size", "archive", "max_unpacked_bytes"}
        unknown = set(item) - (required | {"executables"})
        missing = required - set(item)
        if missing or unknown:
            raise ConfigError(
                f"Trusted tool entry {index} has missing={sorted(missing)} unknown={sorted(unknown)} fields"
            )
        name = str(item["name"])
        version = str(item["version"])
        target_platform = str(item["platform"])
        url = str(item["url"])
        digest = str(item["sha256"]).lower()
        archive = str(item["archive"])
        if not TOOL_NAME_RE.fullmatch(name):
            raise ConfigError(f"Trusted tool entry {index} has invalid name")
        if not TOOL_VERSION_RE.fullmatch(version):
            raise ConfigError(f"Trusted tool entry {index} has invalid version")
        if not PLATFORM_RE.fullmatch(target_platform):
            raise ConfigError(f"Trusted tool entry {index} has invalid platform")
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme != "https" or not parsed_url.netloc or parsed_url.username or parsed_url.password:
            raise ConfigError(f"Trusted tool entry {index} must use a credential-free HTTPS URL")
        if not SHA256_RE.fullmatch(digest):
            raise ConfigError(f"Trusted tool entry {index} has invalid SHA-256")
        try:
            size = int(item["size"])
            max_unpacked = int(item["max_unpacked_bytes"])
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Trusted tool entry {index} size fields must be integers") from exc
        if not 1 <= size <= MAX_ARCHIVE_BYTES:
            raise ConfigError(f"Trusted tool entry {index} archive size is outside allowed bounds")
        if not 1 <= max_unpacked <= MAX_UNPACKED_BYTES:
            raise ConfigError(f"Trusted tool entry {index} unpacked size ceiling is outside allowed bounds")
        if archive not in {"zip", "tar.gz", "tar.xz"}:
            raise ConfigError(f"Trusted tool entry {index} archive must be zip, tar.gz, or tar.xz")
        executables_raw = item.get("executables", [])
        if not isinstance(executables_raw, list):
            raise ConfigError(f"Trusted tool entry {index} executables must be an array")
        executables = [_safe_relative_path(str(value), f"trusted tool entry {index} executable") for value in executables_raw]

        if name in seen_names:
            raise ConfigError(f"Trusted tools contain duplicate name {name}")
        seen_names.add(name)
        result.append(
            {
                "name": name,
                "version": version,
                "platform": target_platform,
                "url": url,
                "sha256": digest,
                "size": size,
                "archive": archive,
                "max_unpacked_bytes": max_unpacked,
                "executables": executables,
            }
        )
    return result


def _safe_relative_path(value: str, label: str) -> str:
    if not value or "\\" in value:
        raise ConfigError(f"{label} must be a non-empty POSIX relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ConfigError(f"{label} must remain below the tool payload root")
    return pure.as_posix()


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
    python_version = subprocess.run(
        ["python3", "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    match = re.search(r"Python (\d+)\.(\d+)", python_version)
    if not match or (int(match.group(1)), int(match.group(2))) < (3, 12):
        raise ConfigError(f"Python 3.12 or newer is required; found {python_version or 'unknown'}")
    go_version = subprocess.run(
        ["go", "version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    go_match = re.search(r"go(\d+)\.(\d+)", go_version)
    if not go_match or (int(go_match.group(1)), int(go_match.group(2))) < (1, 22):
        raise ConfigError(f"Go 1.22 or newer is required; found {go_version or 'unknown'}")
    tools_text = ",".join(BASE_TOOLS)
    log(
        f"Base tools OK revision={SUPERVISOR_REVISION} "
        f"python={python_version} go={go_version} tools={tools_text}"
    )


def _child_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if (
            key in {"CARTHORSE_REGISTRATION_TOKEN", "CARTHORSE_TRUSTED_TOOLS_B64"}
            or key.endswith("_GITHUB_TOKEN")
        ):
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


def _download_verified(
    url: str,
    expected_sha256: str,
    destination: Path,
    *,
    expected_size: int | None = None,
    prefix: str = "download-",
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise RuntimeError(f"Refusing symlinked download cache path: {destination}")
    if destination.exists():
        size_ok = expected_size is None or destination.stat().st_size == expected_size
        if size_ok and _sha256(destination) == expected_sha256:
            return
        destination.unlink()
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=".download", dir=destination.parent)
    os.close(tmp_fd)
    tmp = Path(tmp_name)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        digest = hashlib.sha256()
        total = 0
        with urllib.request.urlopen(request, timeout=180) as response, tmp.open("wb") as output:
            final_url = response.geturl()
            if urllib.parse.urlparse(final_url).scheme != "https":
                raise RuntimeError("Verified download redirected away from HTTPS")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if expected_size is not None and total > expected_size:
                    raise RuntimeError("Verified download exceeded its pinned archive size")
                digest.update(chunk)
                output.write(chunk)
        if expected_size is not None and total != expected_size:
            raise RuntimeError(f"Archive size mismatch: expected {expected_size}, got {total}")
        got = digest.hexdigest()
        if got != expected_sha256:
            raise RuntimeError(f"Archive SHA-256 mismatch: expected {expected_sha256}, got {got}")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def _safe_extract_tar(archive: Path, destination: Path, *, max_unpacked_bytes: int | None = None) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    total = 0
    mode = "r:*"
    with tarfile.open(archive, mode) as bundle:
        members = bundle.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"Unsafe path in archive: {member.name}")
            if member.ischr() or member.isblk() or member.isfifo():
                raise RuntimeError(f"Special file rejected from archive: {member.name}")
            if member.issym():
                link_target = (target.parent / member.linkname).resolve()
                if link_target != root and root not in link_target.parents:
                    raise RuntimeError(f"Unsafe symlink in archive: {member.name}")
            elif member.islnk():
                link_target = (destination / member.linkname).resolve()
                if link_target != root and root not in link_target.parents:
                    raise RuntimeError(f"Unsafe hardlink in archive: {member.name}")
            if member.isfile():
                total += member.size
                if max_unpacked_bytes is not None and total > max_unpacked_bytes:
                    raise RuntimeError("Archive exceeds configured unpacked-size ceiling")
        bundle.extractall(destination, members=members, filter="data")


def _safe_extract_zip(archive: Path, destination: Path, *, max_unpacked_bytes: int) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    total = 0
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        for info in infos:
            name = info.filename
            if "\\" in name:
                raise RuntimeError(f"Backslash path rejected from ZIP archive: {name}")
            pure = PurePosixPath(name)
            if pure.is_absolute() or any(part == ".." for part in pure.parts):
                raise RuntimeError(f"Unsafe path in ZIP archive: {name}")
            target = (destination / pure.as_posix()).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"Unsafe path in ZIP archive: {name}")
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(unix_mode)
            if file_type == stat.S_IFLNK:
                raise RuntimeError(f"Symlink rejected from ZIP archive: {name}")
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise RuntimeError(f"Special file rejected from ZIP archive: {name}")
            if not info.is_dir():
                total += info.file_size
                if total > max_unpacked_bytes:
                    raise RuntimeError("ZIP archive exceeds configured unpacked-size ceiling")
        for info in infos:
            name = PurePosixPath(info.filename).as_posix()
            if info.is_dir():
                (destination / name).mkdir(parents=True, exist_ok=True)
                continue
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def _tree_manifest(root: Path) -> tuple[list[dict[str, Any]], str]:
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if path.is_symlink():
            raise RuntimeError(f"Trusted tool payload contains a symlink: {path.relative_to(root)}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise RuntimeError(f"Trusted tool payload contains a non-regular file: {path.relative_to(root)}")
        rel = path.relative_to(root).as_posix()
        files.append({
            "path": rel,
            "size": path.stat().st_size,
            "mode": stat.S_IMODE(path.stat().st_mode),
            "sha256": _sha256(path),
        })
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return files, hashlib.sha256(encoded).hexdigest()



def _make_tree_owner_writable(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        try:
            os.chmod(path, 0o755 if path.is_dir() else 0o644)
        except OSError:
            pass
    try:
        os.chmod(root, 0o755)
    except OSError:
        pass

def _make_payload_read_only(payload: Path, executables: list[str]) -> None:
    executable_set = set(executables)
    for path in sorted(payload.rglob("*"), reverse=True):
        if path.is_symlink():
            raise RuntimeError(f"Trusted tool payload unexpectedly contains symlink {path}")
        if path.is_dir():
            os.chmod(path, 0o555)
        elif path.is_file():
            rel = path.relative_to(payload).as_posix()
            os.chmod(path, 0o555 if rel in executable_set else 0o444)
    os.chmod(payload, 0o555)


def _tool_object_dir(digest: str) -> Path:
    return TRUSTED_OBJECTS_DIR / digest


def _tool_manifest_path(digest: str) -> Path:
    return _tool_object_dir(digest) / "manifest.json"


def _expected_tool_manifest(spec: dict[str, Any], files: list[dict[str, Any]], tree_sha256: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "name": spec["name"],
        "version": spec["version"],
        "platform": spec["platform"],
        "url": spec["url"],
        "archive_sha256": spec["sha256"],
        "archive_size": spec["size"],
        "archive": spec["archive"],
        "max_unpacked_bytes": spec["max_unpacked_bytes"],
        "executables": spec["executables"],
        "tree_sha256": tree_sha256,
        "files": files,
    }


def _verify_tool_object(spec: dict[str, Any]) -> dict[str, Any]:
    object_dir = _tool_object_dir(spec["sha256"])
    manifest_path = object_dir / "manifest.json"
    payload = object_dir / "payload"
    if object_dir.is_symlink() or manifest_path.is_symlink() or payload.is_symlink():
        raise RuntimeError(f"Trusted tool object {spec['sha256']} contains a symlinked control path")
    if not manifest_path.is_file() or not payload.is_dir():
        raise RuntimeError(f"Trusted tool object {spec['sha256']} is incomplete")
    expected_modes = ((object_dir, 0o555), (payload, 0o555), (manifest_path, 0o444))
    for path, expected_mode in expected_modes:
        actual_mode = stat.S_IMODE(path.stat().st_mode)
        if actual_mode != expected_mode:
            raise RuntimeError(
                f"Trusted tool object {spec['sha256']} control permission mismatch: "
                f"{path.name} mode={oct(actual_mode)} expected={oct(expected_mode)}"
            )
    for directory in payload.rglob("*"):
        if directory.is_dir() and not directory.is_symlink():
            actual_mode = stat.S_IMODE(directory.stat().st_mode)
            if actual_mode != 0o555:
                raise RuntimeError(
                    f"Trusted tool object {spec['sha256']} directory permission mismatch: "
                    f"{directory.relative_to(payload)} mode={oct(actual_mode)}"
                )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Trusted tool object {spec['sha256']} manifest is invalid") from exc
    files, tree_sha = _tree_manifest(payload)
    expected = _expected_tool_manifest(spec, files, tree_sha)
    if manifest != expected:
        raise RuntimeError(f"Trusted tool object {spec['sha256']} failed manifest/content verification")
    for executable in spec["executables"]:
        target = payload / executable
        if not target.is_file() or target.is_symlink():
            raise RuntimeError(f"Trusted tool executable missing: {spec['name']}:{executable}")
        if not os.access(target, os.X_OK):
            raise RuntimeError(f"Trusted tool executable is not executable: {spec['name']}:{executable}")
    return manifest


def _quarantine_tool_object(digest: str, reason: str) -> None:
    object_dir = _tool_object_dir(digest)
    if not object_dir.exists() and not object_dir.is_symlink():
        return
    TRUSTED_QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    dest = TRUSTED_QUARANTINE_DIR / f"{digest}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    _make_tree_owner_writable(object_dir)
    os.replace(object_dir, dest)
    log(f"Quarantined trusted tool digest={digest} reason={reason}")


def _install_tool_object(spec: dict[str, Any]) -> dict[str, Any]:
    digest = spec["sha256"]
    object_dir = _tool_object_dir(digest)
    if object_dir.exists() or object_dir.is_symlink():
        try:
            return _verify_tool_object(spec)
        except RuntimeError as exc:
            _quarantine_tool_object(digest, str(exc))

    TRUSTED_OBJECTS_DIR.mkdir(parents=True, exist_ok=True)
    TRUSTED_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    TRUSTED_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    archive_suffix = {"zip": ".zip", "tar.gz": ".tar.gz", "tar.xz": ".tar.xz"}[spec["archive"]]
    archive = TRUSTED_DOWNLOADS_DIR / f"{digest}{archive_suffix}"
    _download_verified(
        spec["url"],
        digest,
        archive,
        expected_size=spec["size"],
        prefix=f"tool-{spec['name']}-",
    )

    staging = Path(tempfile.mkdtemp(prefix=f"{digest}.", dir=TRUSTED_STAGING_DIR))
    try:
        payload = staging / "payload"
        if spec["archive"] == "zip":
            _safe_extract_zip(archive, payload, max_unpacked_bytes=spec["max_unpacked_bytes"])
        else:
            _safe_extract_tar(archive, payload, max_unpacked_bytes=spec["max_unpacked_bytes"])
        for executable in spec["executables"]:
            target = payload / executable
            if not target.is_file() or target.is_symlink():
                raise RuntimeError(f"Configured trusted executable missing from archive: {executable}")
            os.chmod(target, 0o755)
        _make_payload_read_only(payload, spec["executables"])
        files, tree_sha = _tree_manifest(payload)
        manifest = _expected_tool_manifest(spec, files, tree_sha)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(staging / "manifest.json", 0o444)
        try:
            os.replace(staging, object_dir)
            os.chmod(object_dir, 0o555)
        except OSError:
            if object_dir.exists():
                shutil.rmtree(staging, ignore_errors=True)
                return _verify_tool_object(spec)
            raise
    finally:
        if staging.exists():
            _make_tree_owner_writable(staging)
            shutil.rmtree(staging, ignore_errors=True)

    log(
        f"Installed trusted tool name={spec['name']} version={spec['version']} "
        f"platform={spec['platform']} digest={digest}"
    )
    return _verify_tool_object(spec)


def _ref_path(spec: dict[str, Any]) -> Path:
    return TRUSTED_REFS_DIR / spec["name"] / spec["platform"] / f"{spec['version']}.json"


def _write_tool_ref(spec: dict[str, Any]) -> None:
    path = _ref_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "schema": 1,
        "name": spec["name"],
        "version": spec["version"],
        "platform": spec["platform"],
        "sha256": spec["sha256"],
        "installed_at": int(time.time()),
    }
    if path.is_file() and not path.is_symlink():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if current.get("sha256") == spec["sha256"]:
                value["installed_at"] = int(current.get("installed_at", value["installed_at"]))
        except Exception:
            pass
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _prune_trusted_tools(specs: list[dict[str, Any]], retention: int) -> None:
    configured = {(s["name"], s["platform"], s["version"], s["sha256"]) for s in specs}
    configured_by_group: dict[tuple[str, str], set[tuple[str, str, str, str]]] = {}
    for identity in configured:
        configured_by_group.setdefault((identity[0], identity[1]), set()).add(identity)
    keep_digests = {s["sha256"] for s in specs}
    if TRUSTED_REFS_DIR.exists():
        for platform_dir in [p for p in TRUSTED_REFS_DIR.glob("*/*") if p.is_dir() and not p.is_symlink()]:
            group = (platform_dir.parent.name, platform_dir.name)
            current_identities = configured_by_group.get(group, set())
            entries: list[tuple[int, Path, tuple[str, str, str, str]]] = []
            for path in platform_dir.glob("*.json"):
                if path.is_symlink():
                    path.unlink()
                    continue
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    identity = (
                        str(value.get("name", "")),
                        str(value.get("platform", "")),
                        str(value.get("version", "")),
                        str(value.get("sha256", "")),
                    )
                    valid = (
                        identity[0] == group[0]
                        and identity[1] == group[1]
                        and identity[2] == path.stem
                        and bool(TOOL_VERSION_RE.fullmatch(identity[2]))
                        and bool(SHA256_RE.fullmatch(identity[3]))
                    )
                    if not valid:
                        raise ValueError("invalid trusted-tool ref identity")
                    entries.append((int(value.get("installed_at", 0)), path, identity))
                except Exception:
                    path.unlink(missing_ok=True)
            entries.sort(key=lambda item: item[0], reverse=True)
            historical_slots = max(0, retention - len(current_identities))
            historical_kept = 0
            for _installed, path, identity in entries:
                if identity in current_identities:
                    keep_digests.add(identity[3])
                elif historical_kept < historical_slots:
                    keep_digests.add(identity[3])
                    historical_kept += 1
                else:
                    path.unlink(missing_ok=True)

    if TRUSTED_OBJECTS_DIR.exists():
        for child in TRUSTED_OBJECTS_DIR.iterdir():
            if child.name not in keep_digests and SHA256_RE.fullmatch(child.name):
                if child.is_dir() and not child.is_symlink():
                    _make_tree_owner_writable(child)
                    shutil.rmtree(child)
                elif child.is_symlink():
                    child.unlink()
    if TRUSTED_DOWNLOADS_DIR.exists():
        for child in TRUSTED_DOWNLOADS_DIR.iterdir():
            digest = child.name.split(".", 1)[0]
            if SHA256_RE.fullmatch(digest) and digest not in keep_digests:
                child.unlink(missing_ok=True)


def _prepare_trusted_tools(specs: list[dict[str, Any]], retention: int) -> dict[str, dict[str, Any]]:
    if not specs:
        return {}
    TRUSTED_DIR.mkdir(parents=True, exist_ok=True)
    prepared: dict[str, dict[str, Any]] = {}
    for spec in specs:
        manifest = _install_tool_object(spec)
        _write_tool_ref(spec)
        prepared[spec["name"]] = {"spec": spec, "manifest": manifest}
    _prune_trusted_tools(specs, retention)
    return prepared


def _assert_trusted_tools_unchanged(prepared: dict[str, dict[str, Any]]) -> None:
    for name, item in prepared.items():
        current = _verify_tool_object(item["spec"])
        if current != item["manifest"]:
            raise RuntimeError(f"Trusted tool changed during job: {name}")


def _trusted_tool_environment(prepared: dict[str, dict[str, Any]]) -> dict[str, str]:
    env: dict[str, str] = {}
    index: dict[str, Any] = {}
    for name, item in prepared.items():
        spec = item["spec"]
        payload = _tool_object_dir(spec["sha256"]) / "payload"
        env_name = "CARTHORSE_TRUSTED_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper() + "_ROOT"
        env[env_name] = str(payload)
        index[name] = {
            "version": spec["version"],
            "platform": spec["platform"],
            "sha256": spec["sha256"],
            "root": str(payload),
        }
    if index:
        env["CARTHORSE_TRUSTED_TOOLS_JSON"] = json.dumps(index, sort_keys=True, separators=(",", ":"))
    return env


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


def _processes_with_job_marker(marker: str) -> list[int]:
    if not sys.platform.startswith("linux"):
        return []
    needle = f"CARTHORSE_JOB_MARKER={marker}".encode("utf-8")
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            data = (entry / "environ").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        if needle in data.split(b"\0"):
            pids.append(pid)
    return pids


def _terminate_marked_processes(marker: str) -> None:
    pids = _processes_with_job_marker(marker)
    if not pids:
        return
    log(f"Cleaning {len(pids)} surviving job process(es)")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        remaining = _processes_with_job_marker(marker)
        if not remaining:
            return
        time.sleep(0.05)
    for pid in _processes_with_job_marker(marker):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.05)
    remaining = _processes_with_job_marker(marker)
    if remaining:
        raise RuntimeError(f"Unable to terminate surviving job processes: {remaining}")


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
    process = subprocess.Popen(args, cwd=cwd, env=env, text=True, start_new_session=True)
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
    _download_verified(asset_url, asset_sha, archive, prefix="runner-")
    _safe_extract_tar(archive, RUNNER_DIR)

    child_env = _child_environment()
    if os.geteuid() == 0:
        child_env["RUNNER_ALLOW_RUNASROOT"] = "1"
    rc = _run_command(
        [
            "./config.sh", "--unattended", "--replace",
            "--url", f"https://github.com/{config['repository']}",
            "--token", token,
            "--name", runner_name,
            "--labels", ",".join(config["labels"]),
            "--work", "_work",
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


def _fresh_job_environment(
    sequence: int,
    prepared_tools: dict[str, dict[str, Any]],
) -> tuple[Path, dict[str, str], str]:
    cycle = JOBS_DIR / f"job-{sequence:06d}"
    if cycle.exists() or cycle.is_symlink():
        raise RuntimeError(f"Job directory already exists: {cycle}")
    home = cycle / "home"
    tmp = cycle / "tmp"
    tool_cache = cycle / "toolcache"
    for path in (home, tmp, tool_cache):
        path.mkdir(parents=True, exist_ok=True)

    marker = uuid.uuid4().hex
    env = _child_environment()
    env.update(
        {
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "RUNNER_TEMP": str(tmp),
            "RUNNER_TOOL_CACHE": str(tool_cache),
            "AGENT_TOOLSDIRECTORY": str(tool_cache),
            "CARTHORSE_JOB_MARKER": marker,
        }
    )
    env.update(_trusted_tool_environment(prepared_tools))
    if os.geteuid() == 0:
        env["RUNNER_ALLOW_RUNASROOT"] = "1"
    return cycle, env, marker


def main() -> int:
    global STOP_REQUESTED
    config = _require_config()
    _harden_supervisor_process()
    _require_base_tools()

    # Setup-only configuration is removed before any runner/job process starts.
    os.environ.pop("CARTHORSE_REGISTRATION_TOKEN", None)
    os.environ.pop("CARTHORSE_TRUSTED_TOOLS_B64", None)

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

    registration_fingerprint = _registration_fingerprint()
    prepared_tools = _prepare_trusted_tools(config["trusted_tools"], config["trusted_retention"])
    if prepared_tools:
        log(
            "Trusted tools ready "
            + ",".join(
                f"{name}@{item['spec']['version']}:{item['spec']['sha256'][:12]}"
                for name, item in sorted(prepared_tools.items())
            )
        )
    _scrub_work()

    sequence = 0
    while not STOP_REQUESTED:
        cycle: Path | None = None
        marker = ""
        fatal_code = 0
        stop_after_cleanup = False
        try:
            sequence += 1
            _assert_trusted_tools_unchanged(prepared_tools)
            cycle, child_env, marker = _fresh_job_environment(sequence, prepared_tools)
            log(
                f"Ready repository={config['repository']} runner={runner_name} "
                f"labels={','.join(config['labels'])}"
            )
            rc = _run_command(["./run.sh", "--once"], cwd=RUNNER_DIR, env=child_env)
            _terminate_marked_processes(marker)
            _assert_registration_unchanged(registration_fingerprint)
            _assert_trusted_tools_unchanged(prepared_tools)
            _scrub_work()
            if STOP_REQUESTED:
                stop_after_cleanup = True
            else:
                log(f"Single-job runner exited with code {rc}; workspace scrubbed and trusted tools verified")
        except (ApiError, RuntimeError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            if STOP_REQUESTED:
                stop_after_cleanup = True
            else:
                log(f"ERROR: {exc}")
                # Registration or trusted-tool integrity failures are fail-closed.
                lowered = str(exc).lower()
                if "registration" in lowered or "trusted tool" in lowered:
                    fatal_code = 3
        finally:
            if marker:
                try:
                    _terminate_marked_processes(marker)
                except Exception as cleanup_exc:
                    log(f"ERROR: surviving process cleanup failed: {cleanup_exc}")
                    fatal_code = 4
            if cycle is not None:
                try:
                    _remove_tree(cycle, allowed_parent=JOBS_DIR)
                except Exception as cleanup_exc:
                    log(f"ERROR: job environment cleanup failed: {cleanup_exc}")
                    fatal_code = 4

        if fatal_code:
            return fatal_code
        if stop_after_cleanup:
            break
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
