#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import io
import os
import tarfile
import tempfile
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


def test_config_validation() -> None:
    original = os.environ.copy()
    try:
        os.environ.update(
            {
                "CARTHORSE_GITHUB_REPOSITORY": "carthorsestudios/poobiverse-classic",
                "CARTHORSE_GITHUB_TOKEN": "dummy-secret",
                "CARTHORSE_RUNNER_LABELS": "carthorse-ci,oldgrid",
                "CARTHORSE_RUNNER_NAME_PREFIX": "carthorse-oldgrid",
                "CARTHORSE_RUNNER_VERSION": "latest",
                "CARTHORSE_RESTART_DELAY_SECONDS": "3",
            }
        )
        cfg = sup._require_config()
        check(cfg["repository"] == "carthorsestudios/poobiverse-classic", "valid repository config")
        os.environ["CARTHORSE_GITHUB_REPOSITORY"] = "bad repo"
        try:
            sup._require_config()
        except sup.ConfigError:
            print("OK: invalid repository rejected")
        else:
            raise AssertionError("invalid repository was accepted")
    finally:
        os.environ.clear()
        os.environ.update(original)


def test_child_environment_secret_scrub() -> None:
    original = os.environ.copy()
    try:
        os.environ["CARTHORSE_GITHUB_TOKEN"] = "top-secret"
        os.environ["GITHUB_TOKEN"] = "job-secret"
        os.environ["GH_TOKEN"] = "gh-secret"
        os.environ["NORMAL_VALUE"] = "keep-me"
        child = sup._child_environment()
        check("CARTHORSE_GITHUB_TOKEN" not in child, "AMP PAT removed from child environment")
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
            print("OK: traversal archive rejected")
        else:
            raise AssertionError("traversal archive was accepted")


def main() -> int:
    test_config_validation()
    test_child_environment_secret_scrub()
    test_release_asset_selection()
    test_tar_path_guard()
    print("PASS: supervisor unit checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
