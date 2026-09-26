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
    subprocess.run([sys.executable, str(ROOT / "tools" / "test_supervisor.py")], check=True)
    ok("supervisor compiles and unit checks pass")

    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("prefix") != "CARTHORSECI" or manifest.get("repotype") != "AppTemplates":
        fail("manifest identity")
    ok("manifest identity")

    fields = json.loads(CONFIG.read_text(encoding="utf-8"))
    by_name = {item.get("FieldName"): item for item in fields}
    if "GitHubToken" in by_name:
        fail("long-lived GitHub administration PAT field must not exist")
    token_field = by_name.get("RegistrationToken", {})
    if token_field.get("InputType") != "password" or token_field.get("DefaultValue"):
        fail("registration token field must be password type with no default")
    if not token_field.get("SkipIfEmpty"):
        fail("registration token must be optional after first setup")
    ok("only a temporary registration token is configured")

    kvp = KVP.read_text(encoding="utf-8")
    for required_line in [
        "Meta.DockerRequired=True",
        "Meta.ContainerPolicy=Required",
        "Meta.SpecificDockerImage=cubecoders/ampbase:debian",
        "CARTHORSE_REGISTRATION_TOKEN",
        "Console.AppReadyRegex=",
    ]:
        if required_line not in kvp:
            fail(f"missing KVP contract: {required_line}")
    if "CARTHORSE_GITHUB_TOKEN" in kvp:
        fail("KVP must not expose a long-lived GitHub administration token")
    for package in ("xz-utils", "zip", "unzip", "procps"):
        if f'"{package}"' not in kvp:
            fail(f"missing required container package: {package}")
    ok("container and temporary-token AMP contracts")

    digest = hashlib.sha256(SUPERVISOR.read_bytes()).hexdigest()
    bootstrap = bootstrap_from_kvp(kvp)
    if "\n" in bootstrap or "\r" in bootstrap:
        fail("AMP bootstrap must be single-line; command substitution collapses newlines")
    syntax = subprocess.run(
        ["/bin/bash", "-n", "-c", bootstrap],
        capture_output=True,
        text=True,
    )
    if syntax.returncode != 0:
        fail(f"AMP bootstrap shell syntax invalid: {syntax.stderr.strip()}")
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
    if 'tools_text = ",".join(BASE_TOOLS)\\n' in supervisor_text:
        fail("supervisor contains a literal backslash-n in executable Python source")
    checks = {
        "single-job listener": '["./run.sh", "--once"]',
        "persistent registration": '".credentials_rsaparams"',
        "registration fingerprint": "_assert_registration_unchanged",
        "fresh HOME": '"HOME": str(home)',
        "fresh temp": '"TMPDIR": str(tmp)',
        "fresh tool cache": '"RUNNER_TOOL_CACHE": str(tool_cache)',
        "temporary token removed from child environment": 'key == "CARTHORSE_REGISTRATION_TOKEN"',
        "supervisor process hardening": "PR_SET_DUMPABLE",
        "release digest verification": 'digest.startswith("sha256:")',
        "workspace scrub": "_scrub_work()",
        "base-tool preflight": "_require_base_tools()",
        "preflight revision log": "Base tools OK revision=",
        "supervisor revision": 'SUPERVISOR_REVISION = "7"',
        "zip base tool": '"zip"',
        "unzip base tool": '"unzip"',
        "ps base tool": '"ps"',
    }
    for label, needle in checks.items():
        if needle not in supervisor_text:
            fail(label)
    forbidden = [
        '"--ephemeral"',
        '"--disableupdate"',
        "registration-token\")",
        'Authorization": f"Bearer',
        "CARTHORSE_GITHUB_TOKEN",
    ]
    for needle in forbidden:
        if needle in supervisor_text:
            fail(f"forbidden long-lived/admin or ephemeral-registration behavior remains: {needle}")
    ok("one-time-registration and clean-job invariants")

    searchable = []
    for path in ROOT.rglob("*"):
        if path.is_file() and path.name != "bootstrap.sh":
            searchable.append(path.read_text(encoding="utf-8", errors="ignore"))
    text = "\n".join(searchable)
    if re.search(r"github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+", text):
        fail("repository appears to contain a GitHub token literal")
    forbidden_admin_phrase = "Administration:" + " Read and write"
    if forbidden_admin_phrase in text:
        fail("documentation must not instruct storing repository administration credentials")
    ok("no admin-PAT instructions or obvious GitHub token literal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
