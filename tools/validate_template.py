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


def kvp_value(kvp: str, key: str) -> str:
    prefix = key + "="
    line = next((x for x in kvp.splitlines() if x.startswith(prefix)), "")
    if not line:
        fail(f"missing KVP value {key}")
    return line[len(prefix):]


def bootstrap_from_kvp(kvp: str) -> str:
    line = kvp_value(kvp, "App.CommandLineArgs")
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
        ROOT / "tools" / "test_supervisor.py",
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

    repository_field = by_name.get("TargetRepository", {})
    labels_field = by_name.get("RunnerLabels", {})
    prefix_field = by_name.get("RunnerNamePrefix", {})
    if repository_field.get("DefaultValue") != "":
        fail("generic template must not default to a project repository")
    if labels_field.get("DefaultValue") != "carthorse-ci":
        fail("generic template must use only the generic default label")
    if prefix_field.get("DefaultValue") != "carthorse-ci":
        fail("generic template must use a neutral runner prefix")

    trusted_field = by_name.get("TrustedToolsBase64", {})
    if trusted_field.get("InputType") != "text" or trusted_field.get("DefaultValue") != "":
        fail("trusted tools field must be optional text with an empty default")
    if not trusted_field.get("SkipIfEmpty"):
        fail("trusted tools field must be skippable when empty")
    retention = by_name.get("TrustedToolRetention", {})
    if retention.get("DefaultValue") != "2" or retention.get("MinValue") != "1" or retention.get("MaxValue") != "5":
        fail("trusted tool retention contract")
    ok("neutral defaults, temporary registration token, and optional trusted tools")

    kvp = KVP.read_text(encoding="utf-8")
    required_lines = [
        "Meta.DockerRequired=True",
        "Meta.ContainerPolicy=Required",
        "Meta.SpecificDockerImage=cubecoders/ampbase:ubuntu",
        "Meta.ConfigVersion=8",
        "CARTHORSE_REGISTRATION_TOKEN",
        "CARTHORSE_TRUSTED_TOOLS_B64",
        "CARTHORSE_TRUSTED_TOOL_RETENTION",
        "Console.AppReadyRegex=",
    ]
    for required_line in required_lines:
        if required_line not in kvp:
            fail(f"missing KVP contract: {required_line}")
    if "CARTHORSE_GITHUB_TOKEN" in kvp:
        fail("KVP must not expose a long-lived GitHub administration token")

    for package in (
        "python3", "golang-go", "xz-utils", "zip", "unzip", "zstd", "procps",
        "coreutils", "findutils",
    ):
        if f'"{package}"' not in kvp:
            fail(f"missing required container package: {package}")

    try:
        app_settings = json.loads(kvp_value(kvp, "App.AppSettings"))
    except json.JSONDecodeError as exc:
        fail(f"invalid App.AppSettings JSON: {exc}")
    if app_settings.get("TargetRepository") != "":
        fail("KVP repository default must be neutral")
    if app_settings.get("RunnerLabels") != "carthorse-ci":
        fail("KVP runner labels default must be neutral")
    if app_settings.get("RunnerNamePrefix") != "carthorse-ci":
        fail("KVP runner prefix default must be neutral")
    if app_settings.get("TrustedToolsBase64") != "" or app_settings.get("TrustedToolRetention") != "2":
        fail("KVP trusted tool defaults")
    app_blob = json.dumps(app_settings, sort_keys=True).lower()
    if "oldgrid" in app_blob or "scratch" in app_blob or "poobiverse" in app_blob:
        fail("project-specific defaults remain in generic AppSettings")
    ok("Ubuntu container, baseline packages, and generic pool defaults")

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
    if (
        "raw.githubusercontent.com/carthorsestudios/"
        "carthorse-ci-runner-amp-template/main/control/carthorse_ci_supervisor.py"
        not in bootstrap
    ):
        fail("bootstrap source URL contract")
    ok("bootstrap pins supervisor SHA-256 and source")

    for name in ("carthorseciports.json", "carthorseciupdates.json"):
        if json.loads((ROOT / name).read_text(encoding="utf-8")) != []:
            fail(f"{name} must remain an empty list")
    ok("no network port or AMP updater surface exposed")

    supervisor_text = SUPERVISOR.read_text(encoding="utf-8")
    checks = {
        "single-job listener": '["./run.sh", "--once"]',
        "persistent registration": '".credentials_rsaparams"',
        "registration fingerprint": "_assert_registration_unchanged",
        "fresh HOME": '"HOME": str(home)',
        "fresh temp": '"TMPDIR": str(tmp)',
        "fresh Actions tool cache": '"RUNNER_TOOL_CACHE": str(tool_cache)',
        "temporary token removed from child environment": 'CARTHORSE_REGISTRATION_TOKEN',
        "trusted source config scrubbed from child environment": 'CARTHORSE_TRUSTED_TOOLS_B64',
        "supervisor process hardening": "PR_SET_DUMPABLE",
        "release digest verification": 'digest.startswith("sha256:")',
        "workspace scrub": "_scrub_work()",
        "base-tool preflight": "_require_base_tools()",
        "supervisor revision": 'SUPERVISOR_REVISION = "9"',
        "trusted content-addressed objects": 'TRUSTED_OBJECTS_DIR = TRUSTED_DIR / "objects" / "sha256"',
        "trusted ZIP extraction": "_safe_extract_zip",
        "trusted content tree": "_tree_manifest",
        "trusted post-job verification": "_assert_trusted_tools_unchanged",
        "trusted quarantine": "_quarantine_tool_object",
        "trusted retention": "_prune_trusted_tools",
        "job process marker": "CARTHORSE_JOB_MARKER",
        "surviving process cleanup": "_terminate_marked_processes",
        "Go 1.22 preflight": "Go 1.22 or newer is required",
        "Python 3.12 preflight": "Python 3.12 or newer is required",
        "zstd base tool": '"zstd"',
        "go base tool": '"go"',
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
    ok("one-time registration, clean-job, and trusted-tool invariants")

    searchable = []
    for path in ROOT.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
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
