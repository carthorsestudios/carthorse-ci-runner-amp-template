# Cart Horse CI Runner — AMP Template

A generic, repository-scoped GitHub Actions self-hosted runner for Cart Horse Studios, supervised as a dedicated CubeCoders AMP instance.

The template is CI infrastructure, not a game-specific test harness. Project repositories remain the source of truth for builds, validation, release packaging, and which labels select a worker.

## Deployment model

Use one AMP instance per worker. Multiple workers may register independently to the same repository and advertise the same custom labels; GitHub then routes matching jobs to whichever worker is online and idle.

Example:

- OldGrid worker: repository `carthorsestudios/poobiverse-classic`, labels `carthorse-ci,oldgrid`
- Scratch MMO worker 1: repository `carthorsestudios/scratch-mmo`, labels `carthorse-ci,scratch-mmo`
- Scratch MMO worker 2: repository `carthorsestudios/scratch-mmo`, labels `carthorse-ci,scratch-mmo`

Each instance still accepts only one job at a time. Adding another worker to a pool does not require a dispatcher or template fork.

The template requires a CubeCoders container and uses `cubecoders/ampbase:ubuntu`. The container packages include the GitHub runner's native dependencies plus Python 3.12+, Go 1.22+, compression/archive utilities, process tools, and the ordinary GNU utilities used by Cart Horse CI workflows.

## Security model

The runner is repository-scoped and container-required. No long-lived GitHub repository-administration PAT is stored on the server.

Initial setup uses the time-limited registration token GitHub displays under **Settings > Actions > Runners > New self-hosted runner**. After registration:

1. The setup token is removed from the supervisor environment before any job process starts and should be cleared from AMP.
2. `run.sh --once` accepts one job and exits.
3. Every job gets a fresh HOME, temp directory, and GitHub Actions tool cache.
4. `_work` and the job-specific HOME/temp/tool-cache tree are deleted after every job.
5. A per-job marker is used to find and terminate ordinary background processes that survived the runner job.
6. Persistent runner registration files are fingerprinted and checked after each job. Registration tampering is fail-closed.
7. Optional persistent trusted tools are independently verified before jobs and cryptographically reverified after each job. Persistent tool corruption or mutation is fail-closed.
8. AMP container isolation remains the host boundary. Do not mount production data or the host Docker socket into this runner.

This is intended only for trusted private repositories. It is not an isolation system for arbitrary public fork code.

## One-time registration

For each AMP worker:

1. Configure **Target GitHub Repository**, **Custom Runner Labels**, and **Runner Name Prefix**.
2. In the target repository, open **Settings > Actions > Runners > New self-hosted runner** and select Linux/x64.
3. Copy only the temporary registration token into AMP's **Initial Registration Token** field.
4. Start the AMP instance.
5. Wait for the console to report `Ready` and for GitHub to show the worker idle.
6. Clear **Initial Registration Token** in AMP.
7. Restart the instance once and prove that it reconnects without the token.

Never paste a registration token into chat or commit it.

## Trusted immutable tools

Persistent tools are optional. Leave **Trusted Tools Bundle (Base64 JSON)** empty for a worker that needs no persistent toolchain.

The field contains base64-encoded UTF-8 JSON. The decoded value is an array. Each entry has this schema:

```json
[
  {
    "name": "example-tool",
    "version": "1.2.3",
    "platform": "linux-x64",
    "url": "https://github.com/example/tool/releases/download/v1.2.3/tool.zip",
    "sha256": "64-lowercase-hex-characters",
    "size": 12345678,
    "archive": "zip",
    "max_unpacked_bytes": 50000000,
    "executables": ["bin/tool"]
  }
]
```

Supported archive types are `zip`, `tar.gz`, and `tar.xz`.

The supervisor:

- requires credential-free HTTPS URLs;
- pins the exact archive size and SHA-256;
- downloads to a temporary file and atomically publishes only a verified archive;
- rejects archive traversal, ZIP symlinks, and special-file surprises;
- enforces an unpacked-size ceiling;
- installs into a content-addressed path under `control/trusted-tools/objects/sha256/<digest>`;
- records a manifest containing source identity, file sizes, permissions, per-file SHA-256 values, and a tree digest;
- makes payload files read-only, except configured executable files which are read/execute;
- verifies the entire payload before it is exposed to a job and again after the job;
- quarantines a corrupt object discovered during startup and reacquires it from the pinned source;
- fails closed if a job leaves a persistent trusted-tool mutation behind.

A job receives:

- `CARTHORSE_TRUSTED_<NORMALIZED_NAME>_ROOT` for each configured tool; and
- `CARTHORSE_TRUSTED_TOOLS_JSON`, a compact identity/index document.

The persistent tool store is not used as `RUNNER_TOOL_CACHE`; the normal Actions tool cache remains ephemeral per job.

Because repository code and the supervisor currently run under the same container identity, filesystem mode bits are defense-in-depth rather than a hostile-code security boundary. The important persistence guarantee is that the supervisor is the only normal installer path and any persistent content or permission change is detected before another job is accepted. Do not use this design for untrusted workflow code.

## Trusted-tool retention

**Trusted Tool Versions Retained** defaults to 2. This retains at most two installed versions per tool/platform group, including the current version, while unreferenced content-addressed objects and archives are pruned.

Each AMP worker owns its own trusted-tool store. Workers in the same pool do not share a writable tool cache.

## Generic defaults

The template intentionally has no default target repository and no project-specific label. New instances default to:

- labels: `carthorse-ci`
- runner name prefix: `carthorse-ci`
- trusted tools: disabled
- trusted-tool retention: 2 versions

Existing AMP instances should retain their saved repository, labels, and prefix. Do not change an existing project's labels merely because the template defaults became neutral.

## Add this template repository to AMP

In ADS:

1. Open **Configuration > Instance Deployment > Configuration Repositories**.
2. Add `carthorsestudios/carthorse-ci-runner-amp-template:main`.
3. Fetch/update the repository.
4. Refresh AMP if needed.
5. Create a **Cart Horse CI Runner** Generic Module instance.
6. Keep the instance containerized.

No inbound game/server port is exposed. The worker needs outbound HTTPS access to GitHub and any package/download hosts explicitly used by trusted project workflows.

## Local/static validation

Run:

```bash
python3 tools/validate_template.py
```

The validator compiles the supervisor, runs the unit checks, validates AMP/container contracts, confirms neutral defaults and temporary-token behavior, verifies the pinned bootstrap hash, and checks the clean-job/trusted-tool invariants.

## Important limitations

- One AMP instance is one worker and runs one job at a time.
- Multiple workers may serve one repository by using the same labels.
- This is for trusted private repository workflows only.
- Do not mount production game data into CI.
- Do not mount the host Docker socket.
- Docker-based Actions/service containers require a separate explicit infrastructure review.
- A persistent self-hosted runner is not equivalent to a fresh GitHub-hosted VM; this design limits and verifies the persistent surfaces rather than claiming there are none.
