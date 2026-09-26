#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "control" / "carthorse_ci_supervisor.py"
KVP = ROOT / "carthorseci.kvp"
CONFIG = ROOT / "carthorseciconfig.json"


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def ok(message: str) -> None:
    print(f"OK: {message}")


def bootstrap_from_kvp(kvp: str) -> str:
    line = next((x for x in kvp.splitlines() if x.startswith("App.CommandLineArgs=")), "")
    marker = "${IFS}%s${IFS}"
    if marker not in line or "|base64${IFS}-d)" not in line:
        fail("unable to locate KVP bootstrap")
    if "\\${IFS}" in line or "\\$(" in line:
        fail("KVP launch command must not escape shell expansion tokens")
    encoded = line.split(marker, 1)[1].split("|base64", 1)[0]
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8")
    except Exception as exc:
        fail(f"invalid KVP bootstrap encoding: {exc}")
    raise AssertionError("unreachable")


def main() -> int:
    required = [
        ROOT / "manifest.json",
        KVP,
        CONFIG,
        ROOT / "carthorseciports.json",
        ROOT / "carthorseciupdates.json",
        SUPERVISOR,
    ]
    for path in required:
        if not path.is_file():
            fail(f"missing {path.relative_to(ROOT)}")
    ok("required template files present")

    subprocess.run([sys.executable, "-m", "py_compile", str(SUPERVISOR)], check=True)
    ok("supervisor compiles")

    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("prefix") != "CARTHORSECI" or manifest.get("repotype") != "AppTemplates":
        fail("manifest identity")
    ok("manifest identity")

    fields = json.loads(CONFIG.read_text(encoding="utf-8"))
    by_name = {item.get("FieldName"): item for item in fields}
    token_field = by_name.get("GitHubToken", {})
    if token_field.get("InputType") != "password" or token_field.get("DefaultValue"):
        fail("GitHub token field must be password type with no default")
    ok("GitHub token is secret and has no committed default")

    kvp = KVP.read_text(encoding="utf-8")
    for required_line in [
        "Meta.DockerRequired=True",
        "Meta.ContainerPolicy=Required",
        "Meta.SpecificDockerImage=cubecoders/ampbase:debian",
        "CARTHORSE_GITHUB_TOKEN",
        "Console.AppReadyRegex=",
    ]:
        if required_line not in kvp:
            fail(f"missing KVP contract: {required_line}")
    ok("container and AMP runtime contracts")

    digest = hashlib.sha256(SUPERVISOR.read_bytes()).hexdigest()
    bootstrap = bootstrap_from_kvp(kvp)
    if f"EXPECTED={digest}" not in bootstrap:
        fail("KVP bootstrap does not pin the current supervisor SHA-256")
    if "raw.githubusercontent.com/carthorsestudios/carthorse-ci-runner-amp-template/main/control/carthorse_ci_supervisor.py" not in bootstrap:
        fail("bootstrap source URL contract")
    ok("bootstrap pins supervisor SHA-256 and source")

    for name in ("carthorseciports.json", "carthorseciupdates.json"):
        if json.loads((ROOT / name).read_text(encoding="utf-8")) != []:
            fail(f"{name} must remain an empty list")
    ok("no network port or AMP updater surface exposed")

    supervisor_text = SUPERVISOR.read_text(encoding="utf-8")
    checks = {
        "ephemeral runner flag": '"--ephemeral"',
        "runner update disabled per cycle": '"--disableupdate"',
        "fresh HOME": '"HOME": str(home)',
        "fresh temp": '"TMPDIR": str(tmp)',
        "fresh tool cache": '"RUNNER_TOOL_CACHE": str(tool_cache)',
        "PAT removed from child environment": 'env.pop("GITHUB_TOKEN", None)',
        "supervisor process hardening": "PR_SET_DUMPABLE",
        "release digest verification": 'digest.startswith("sha256:")',
        "cycle deletion": "shutil.rmtree(cycle)",
    }
    for label, needle in checks.items():
        if needle not in supervisor_text:
            fail(label)
    ok("ephemeral isolation and credential-boundary invariants")

    searchable = []
    for path in ROOT.rglob("*"):
        if path.is_file() and path.name != "bootstrap.sh":
            searchable.append(path.read_text(encoding="utf-8", errors="ignore"))
    if re.search(r"github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+", "\n".join(searchable)):
        fail("repository appears to contain a GitHub token literal")
    ok("no obvious GitHub token literal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
