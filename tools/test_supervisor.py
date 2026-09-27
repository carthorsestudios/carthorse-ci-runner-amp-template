#!/usr/bin/env python3
from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "control" / "carthorse_ci_supervisor.py"
spec = importlib.util.spec_from_file_location("carthorse_ci_supervisor", MODULE_PATH)
assert spec and spec.loader
sup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sup)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"OK: {message}")


def tool_spec(sha: str = "a" * 64) -> dict:
    return {
        "name": "example-tool",
        "version": "1.2.3",
        "platform": "linux-x64",
        "url": "https://github.com/example/tool/releases/download/v1.2.3/tool.zip",
        "sha256": sha,
        "size": 1234,
        "archive": "zip",
        "max_unpacked_bytes": 4096,
        "executables": ["bin/tool"],
    }


def encode_tools(items: list[dict]) -> str:
    return base64.b64encode(json.dumps(items).encode("utf-8")).decode("ascii")


def test_config_validation() -> None:
    original = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(
            {
                "CARTHORSE_GITHUB_REPOSITORY": "carthorsestudios/example",
                "CARTHORSE_REGISTRATION_TOKEN": "one-hour-setup-token",
                "CARTHORSE_RUNNER_LABELS": "carthorse-ci,example",
                "CARTHORSE_RUNNER_NAME_PREFIX": "carthorse-example",
                "CARTHORSE_RUNNER_VERSION": "latest",
                "CARTHORSE_RESTART_DELAY_SECONDS": "3",
                "CARTHORSE_TRUSTED_TOOLS_B64": encode_tools([tool_spec()]),
                "CARTHORSE_TRUSTED_TOOL_RETENTION": "2",
            }
        )
        cfg = sup._require_config()
        check(cfg["repository"] == "carthorsestudios/example", "valid repository config")
        check(cfg["registration_token"] == "one-hour-setup-token", "registration token accepted for setup")
        check(cfg["trusted_tools"][0]["name"] == "example-tool", "trusted tool config decoded")
        check(cfg["trusted_retention"] == 2, "trusted tool retention accepted")

        os.environ.pop("CARTHORSE_REGISTRATION_TOKEN")
        cfg = sup._require_config()
        check(cfg["registration_token"] == "", "registration token optional after setup")

        os.environ["CARTHORSE_GITHUB_REPOSITORY"] = "bad repo"
        try:
            sup._require_config()
        except sup.ConfigError:
            print("OK: invalid repository rejected")
        else:
            raise AssertionError("invalid repository was accepted")

        os.environ["CARTHORSE_GITHUB_REPOSITORY"] = "carthorsestudios/example"
        bad = tool_spec()
        bad["url"] = "http://example.invalid/tool.zip"
        os.environ["CARTHORSE_TRUSTED_TOOLS_B64"] = encode_tools([bad])
        try:
            sup._require_config()
        except sup.ConfigError:
            print("OK: non-HTTPS trusted tool rejected")
        else:
            raise AssertionError("non-HTTPS trusted tool was accepted")

        duplicate = tool_spec("1" * 64)
        duplicate["platform"] = "linux-arm64"
        os.environ["CARTHORSE_TRUSTED_TOOLS_B64"] = encode_tools([tool_spec(), duplicate])
        try:
            sup._require_config()
        except sup.ConfigError:
            print("OK: duplicate trusted tool name rejected")
        else:
            raise AssertionError("duplicate trusted tool name was accepted")
    finally:
        os.environ.clear()
        os.environ.update(original)


def test_child_environment_secret_scrub() -> None:
    original = os.environ.copy()
    try:
        os.environ["CARTHORSE_REGISTRATION_TOKEN"] = "temporary-secret"
        os.environ["CARTHORSE_TRUSTED_TOOLS_B64"] = "operator-controlled-config"
        os.environ["GITHUB_TOKEN"] = "job-secret"
        os.environ["GH_TOKEN"] = "gh-secret"
        os.environ["NORMAL_VALUE"] = "keep-me"
        child = sup._child_environment()
        check("CARTHORSE_REGISTRATION_TOKEN" not in child, "registration token removed from child environment")
        check("CARTHORSE_TRUSTED_TOOLS_B64" not in child, "trusted tool source config removed from child environment")
        check("GITHUB_TOKEN" not in child and "GH_TOKEN" not in child, "ambient GitHub tokens removed from child environment")
        check(child.get("NORMAL_VALUE") == "keep-me", "non-secret environment retained")
    finally:
        os.environ.clear()
        os.environ.update(original)


def test_release_asset_selection() -> None:
    version = "2.337.0"
    asset_name = f"actions-runner-linux-{sup._runner_arch()}-{version}.tar.gz"
    metadata = {
        "assets": [
            {
                "name": asset_name,
                "browser_download_url": f"https://github.com/actions/runner/releases/download/v{version}/{asset_name}",
                "digest": "sha256:" + "a" * 64,
            }
        ]
    }
    url, digest = sup._select_runner_asset(version, metadata)
    check(url.endswith(asset_name), "official runner asset selected")
    check(digest == "a" * 64, "runner release digest required")


def _write_tar(path: Path, member_name: str) -> None:
    data = b"test"
    with tarfile.open(path, "w:gz") as bundle:
        info = tarfile.TarInfo(member_name)
        info.size = len(data)
        bundle.addfile(info, io.BytesIO(data))


def test_tar_path_guard() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        good = root / "good.tar.gz"
        _write_tar(good, "bin/runner")
        sup._safe_extract_tar(good, root / "good")
        check((root / "good" / "bin" / "runner").read_bytes() == b"test", "safe runner archive extracts")

        bad = root / "bad.tar.gz"
        _write_tar(bad, "../escape")
        try:
            sup._safe_extract_tar(bad, root / "bad")
        except RuntimeError:
            print("OK: tar traversal rejected")
        else:
            raise AssertionError("tar traversal was accepted")


def test_zip_guards() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        good = root / "good.zip"
        with zipfile.ZipFile(good, "w") as bundle:
            bundle.writestr("bin/tool", b"tool")
        sup._safe_extract_zip(good, root / "good", max_unpacked_bytes=100)
        check((root / "good" / "bin" / "tool").read_bytes() == b"tool", "safe ZIP extracts")

        traversal = root / "traversal.zip"
        with zipfile.ZipFile(traversal, "w") as bundle:
            bundle.writestr("../escape", b"bad")
        try:
            sup._safe_extract_zip(traversal, root / "bad-traversal", max_unpacked_bytes=100)
        except RuntimeError:
            print("OK: ZIP traversal rejected")
        else:
            raise AssertionError("ZIP traversal was accepted")

        symlink = root / "symlink.zip"
        with zipfile.ZipFile(symlink, "w") as bundle:
            info = zipfile.ZipInfo("bin/link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(info, "../outside")
        try:
            sup._safe_extract_zip(symlink, root / "bad-symlink", max_unpacked_bytes=100)
        except RuntimeError:
            print("OK: ZIP symlink rejected")
        else:
            raise AssertionError("ZIP symlink was accepted")

        large = root / "large.zip"
        with zipfile.ZipFile(large, "w") as bundle:
            bundle.writestr("large", b"x" * 101)
        try:
            sup._safe_extract_zip(large, root / "bad-size", max_unpacked_bytes=100)
        except RuntimeError:
            print("OK: ZIP unpacked-size ceiling enforced")
        else:
            raise AssertionError("ZIP size ceiling was ignored")


def test_registration_fingerprint() -> None:
    original_runner = sup.RUNNER_DIR
    try:
        with tempfile.TemporaryDirectory() as temp:
            runner = Path(temp)
            sup.RUNNER_DIR = runner
            for name in ("run.sh", "config.sh", ".runner", ".credentials", ".credentials_rsaparams"):
                (runner / name).write_text(name, encoding="utf-8")
            baseline = sup._registration_fingerprint()
            sup._assert_registration_unchanged(baseline)
            (runner / ".credentials").write_text("tampered", encoding="utf-8")
            try:
                sup._assert_registration_unchanged(baseline)
            except RuntimeError:
                print("OK: registration tampering rejected")
            else:
                raise AssertionError("registration tampering was accepted")
    finally:
        sup.RUNNER_DIR = original_runner



def _set_trusted_root(temp_root: Path) -> tuple:
    original = (
        sup.TRUSTED_DIR,
        sup.TRUSTED_OBJECTS_DIR,
        sup.TRUSTED_REFS_DIR,
        sup.TRUSTED_DOWNLOADS_DIR,
        sup.TRUSTED_STAGING_DIR,
        sup.TRUSTED_QUARANTINE_DIR,
    )
    trusted = temp_root / "trusted"
    sup.TRUSTED_DIR = trusted
    sup.TRUSTED_OBJECTS_DIR = trusted / "objects" / "sha256"
    sup.TRUSTED_REFS_DIR = trusted / "refs"
    sup.TRUSTED_DOWNLOADS_DIR = trusted / "downloads"
    sup.TRUSTED_STAGING_DIR = trusted / "staging"
    sup.TRUSTED_QUARANTINE_DIR = trusted / "quarantine"
    return original


def _restore_trusted_root(original: tuple) -> None:
    (
        sup.TRUSTED_DIR,
        sup.TRUSTED_OBJECTS_DIR,
        sup.TRUSTED_REFS_DIR,
        sup.TRUSTED_DOWNLOADS_DIR,
        sup.TRUSTED_STAGING_DIR,
        sup.TRUSTED_QUARANTINE_DIR,
    ) = original


def test_trusted_tool_full_install_and_recovery() -> None:
    with tempfile.TemporaryDirectory() as temp:
        original = _set_trusted_root(Path(temp))
        try:
            archive_source = Path(temp) / "tool.zip"
            with zipfile.ZipFile(archive_source, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("bin/tool", b"trusted-binary")
                bundle.writestr("share/data.txt", b"trusted-data")
            digest = sup._sha256(archive_source)
            spec = tool_spec(digest)
            spec["size"] = archive_source.stat().st_size
            spec["max_unpacked_bytes"] = 4096
            sup.TRUSTED_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
            cached = sup.TRUSTED_DOWNLOADS_DIR / f"{digest}.zip"
            cached.write_bytes(archive_source.read_bytes())

            manifest = sup._install_tool_object(spec)
            target = sup._tool_object_dir(digest) / "payload" / "bin" / "tool"
            check(target.read_bytes() == b"trusted-binary", "trusted tool installs from verified cached archive")
            check((target.stat().st_mode & 0o777) == 0o555, "trusted executable is read/execute only")
            check(manifest["archive_sha256"] == digest, "trusted object manifest pins archive digest")

            os.chmod(target, 0o755)
            target.write_bytes(b"corrupt")
            os.chmod(target, 0o555)
            repaired = sup._install_tool_object(spec)
            check(target.read_bytes() == b"trusted-binary", "corrupt trusted object is quarantined and rebuilt")
            check(repaired["tree_sha256"] == sup._verify_tool_object(spec)["tree_sha256"], "rebuilt trusted object verifies")
            quarantined = list(sup.TRUSTED_QUARANTINE_DIR.glob(f"{digest}-*"))
            check(len(quarantined) == 1, "corrupt trusted object retained in quarantine")
        finally:
            _restore_trusted_root(original)


def test_trusted_tool_retention_prunes_read_only_object() -> None:
    with tempfile.TemporaryDirectory() as temp:
        original = _set_trusted_root(Path(temp))
        try:
            keep = tool_spec("d" * 64)
            keep["version"] = "2.0.0"
            old1 = tool_spec("e" * 64)
            old1["version"] = "1.0.0"
            old2 = tool_spec("f" * 64)
            old2["version"] = "0.9.0"
            now = int(time.time())
            installed_at = {
                keep["version"]: now - 300,
                old1["version"]: now - 100,
                old2["version"]: now - 200,
            }
            for spec in (keep, old1, old2):
                object_dir = sup._tool_object_dir(spec["sha256"])
                payload = object_dir / "payload"
                payload.mkdir(parents=True)
                (payload / "data").write_text(spec["version"], encoding="utf-8")
                sup._make_payload_read_only(payload, [])
                os.chmod(object_dir, 0o555)
                ref = sup._ref_path(spec)
                ref.parent.mkdir(parents=True, exist_ok=True)
                ref.write_text(json.dumps({
                    "schema":1,"name":spec["name"],"version":spec["version"],
                    "platform":spec["platform"],"sha256":spec["sha256"],
                    "installed_at":installed_at[spec["version"]]
                })+"\n",encoding="utf-8")
            sup._prune_trusted_tools([keep], retention=2)
            check(sup._tool_object_dir(keep["sha256"]).exists(), "current trusted tool retained even when older")
            check(sup._tool_object_dir(old1["sha256"]).exists(), "newest previous trusted tool retained")
            check(not sup._tool_object_dir(old2["sha256"]).exists(), "older read-only trusted tool pruned")
        finally:
            _restore_trusted_root(original)

def test_trusted_tool_manifest_and_tamper() -> None:
    original = (
        sup.TRUSTED_DIR,
        sup.TRUSTED_OBJECTS_DIR,
        sup.TRUSTED_REFS_DIR,
        sup.TRUSTED_DOWNLOADS_DIR,
        sup.TRUSTED_STAGING_DIR,
        sup.TRUSTED_QUARANTINE_DIR,
    )
    try:
        with tempfile.TemporaryDirectory() as temp:
            trusted = Path(temp) / "trusted"
            sup.TRUSTED_DIR = trusted
            sup.TRUSTED_OBJECTS_DIR = trusted / "objects" / "sha256"
            sup.TRUSTED_REFS_DIR = trusted / "refs"
            sup.TRUSTED_DOWNLOADS_DIR = trusted / "downloads"
            sup.TRUSTED_STAGING_DIR = trusted / "staging"
            sup.TRUSTED_QUARANTINE_DIR = trusted / "quarantine"

            spec = tool_spec("b" * 64)
            object_dir = sup._tool_object_dir(spec["sha256"])
            payload = object_dir / "payload"
            (payload / "bin").mkdir(parents=True)
            target = payload / "bin" / "tool"
            target.write_bytes(b"known-good")
            sup._make_payload_read_only(payload, spec["executables"])
            files, tree_sha = sup._tree_manifest(payload)
            manifest = sup._expected_tool_manifest(spec, files, tree_sha)
            (object_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            os.chmod(object_dir / "manifest.json", 0o444)
            os.chmod(object_dir, 0o555)
            check(sup._verify_tool_object(spec)["tree_sha256"] == tree_sha, "trusted tool object verifies")

            os.chmod(target, 0o755)
            target.write_bytes(b"tampered")
            os.chmod(target, 0o555)
            try:
                sup._verify_tool_object(spec)
            except RuntimeError:
                print("OK: trusted tool content tampering rejected")
            else:
                raise AssertionError("trusted tool content tampering was accepted")

            target.write_bytes(b"known-good") if os.access(target, os.W_OK) else None
            if not os.access(target, os.W_OK):
                os.chmod(target, 0o755)
                target.write_bytes(b"known-good")
                os.chmod(target, 0o555)
            os.chmod(target, 0o755)
            try:
                sup._verify_tool_object(spec)
            except RuntimeError:
                print("OK: trusted tool permission tampering rejected")
            else:
                raise AssertionError("trusted tool permission tampering was accepted")


            os.chmod(target, 0o555)
            os.chmod(payload, 0o755)
            try:
                sup._verify_tool_object(spec)
            except RuntimeError:
                print("OK: trusted tool directory permission tampering rejected")
            else:
                raise AssertionError("trusted tool directory permission tampering was accepted")

            os.chmod(payload, 0o555)
            manifest_path = object_dir / "manifest.json"
            os.chmod(manifest_path, 0o644)
            try:
                sup._verify_tool_object(spec)
            except RuntimeError:
                print("OK: trusted tool manifest permission tampering rejected")
            else:
                raise AssertionError("trusted tool manifest permission tampering was accepted")
    finally:
        (
            sup.TRUSTED_DIR,
            sup.TRUSTED_OBJECTS_DIR,
            sup.TRUSTED_REFS_DIR,
            sup.TRUSTED_DOWNLOADS_DIR,
            sup.TRUSTED_STAGING_DIR,
            sup.TRUSTED_QUARANTINE_DIR,
        ) = original


def test_trusted_tool_environment() -> None:
    spec = tool_spec("c" * 64)
    prepared = {"example-tool": {"spec": spec, "manifest": {"schema": 1}}}
    original = sup.TRUSTED_OBJECTS_DIR
    try:
        with tempfile.TemporaryDirectory() as temp:
            sup.TRUSTED_OBJECTS_DIR = Path(temp)
            env = sup._trusted_tool_environment(prepared)
            check("CARTHORSE_TRUSTED_EXAMPLE_TOOL_ROOT" in env, "trusted tool root exposed with deterministic env name")
            index = json.loads(env["CARTHORSE_TRUSTED_TOOLS_JSON"])
            check(index["example-tool"]["sha256"] == spec["sha256"], "trusted tool identity exposed to job")
    finally:
        sup.TRUSTED_OBJECTS_DIR = original


def test_read_only_job_tree_cleanup() -> None:
    with tempfile.TemporaryDirectory() as temp:
        jobs = Path(temp) / "jobs"
        job = jobs / "job-000004"
        module = job / "home" / "go" / "pkg" / "mod" / "github.com" / "gorilla" / "websocket@v1.5.3"
        module.mkdir(parents=True)
        source = module / "conn.go"
        source.write_text("package websocket\n", encoding="utf-8")

        # Match the Go module cache behavior that caused Scratch workers to
        # fail startup after their first real validation run.
        os.chmod(source, 0o444)
        for path in reversed(list(job.rglob("*"))):
            if path.is_dir() and not path.is_symlink():
                os.chmod(path, 0o555)
        os.chmod(job, 0o555)

        sup._remove_tree(job, allowed_parent=jobs)
        check(not job.exists(), "read-only Go module cache job tree scrubbed")


def test_surviving_process_cleanup() -> None:
    marker = "test-" + str(os.getpid())
    env = os.environ.copy()
    env["CARTHORSE_JOB_MARKER"] = marker
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=env,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while process.pid not in sup._processes_with_job_marker(marker) and time.monotonic() < deadline:
            time.sleep(0.05)
        check(process.pid in sup._processes_with_job_marker(marker), "surviving job process detected")
        sup._terminate_marked_processes(marker)
        process.wait(timeout=3)
        check(process.pid not in sup._processes_with_job_marker(marker), "surviving job process terminated")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_base_tool_preflight() -> None:
    original_which = sup.shutil.which
    original_run = sup.subprocess.run
    try:
        sup.shutil.which = lambda name: f"/usr/bin/{name}"
        def good_run(args, **kwargs):
            out = "Python 3.12.3\n" if args[0] == "python3" else "go version go1.22.12 linux/amd64\n"
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
        sup.subprocess.run = good_run
        sup._require_base_tools()
        print("OK: complete base toolchain accepted")

        sup.shutil.which = lambda name: None if name == "zstd" else f"/usr/bin/{name}"
        try:
            sup._require_base_tools()
        except sup.ConfigError as exc:
            check("zstd" in str(exc), "missing base tool rejected")
        else:
            raise AssertionError("missing base tool was accepted")

        sup.shutil.which = lambda name: f"/usr/bin/{name}"
        sup.subprocess.run = lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="Python 3.11.9\n", stderr="")
        try:
            sup._require_base_tools()
        except sup.ConfigError as exc:
            check("3.12" in str(exc), "old Python rejected")
        else:
            raise AssertionError("old Python was accepted")

        def old_go_run(args, **kwargs):
            out = "Python 3.12.3\n" if args[0] == "python3" else "go version go1.21.9 linux/amd64\n"
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
        sup.subprocess.run = old_go_run
        try:
            sup._require_base_tools()
        except sup.ConfigError as exc:
            check("1.22" in str(exc), "old Go rejected")
        else:
            raise AssertionError("old Go was accepted")
    finally:
        sup.shutil.which = original_which
        sup.subprocess.run = original_run


def main() -> int:
    test_config_validation()
    test_child_environment_secret_scrub()
    test_release_asset_selection()
    test_tar_path_guard()
    test_zip_guards()
    test_registration_fingerprint()
    test_trusted_tool_full_install_and_recovery()
    test_trusted_tool_retention_prunes_read_only_object()
    test_trusted_tool_manifest_and_tamper()
    test_trusted_tool_environment()
    test_read_only_job_tree_cleanup()
    test_surviving_process_cleanup()
    test_base_tool_preflight()
    print("PASS: supervisor unit checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
