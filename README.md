# Cart Horse CI Runner — AMP Template

A generic, repository-scoped GitHub Actions self-hosted runner for Cart Horse Studios, supervised as a dedicated CubeCoders AMP instance.

This template is **CI infrastructure**, not an OldGrid-specific test harness. GitHub workflows remain the source of truth for what a project builds and tests. If OldGrid grows from 60 checks to 100 checks, the runner normally needs no change; GitHub simply sends the revised job to the same worker class.

## Intended deployment

Start with one dedicated instance:

- AMP instance: `Cart Horse CI - OldGrid`
- Repository: `carthorsestudios/poobiverse-classic`
- Custom labels: `carthorse-ci,oldgrid`
- Concurrency: one job at a time
- Container: required
- Suggested initial allocation: 4 CPU cores / 8 GB RAM

A later Scratch MMO instance can use this same template with its own repository-scoped token and labels such as `carthorse-ci,scratch-mmo`.

## Security model

The runner is deliberately repository-scoped and container-required.

Each worker cycle:

1. Resolves the official `actions/runner` release (`latest` by default).
2. Selects the official Linux asset for the current CPU architecture.
3. Requires and verifies the SHA-256 digest published in GitHub's release metadata.
4. Requests a short-lived repository runner registration token using the AMP-held fine-grained PAT.
5. Extracts the runner into a brand-new cycle directory.
6. Gives the job a fresh HOME, temp directory, tool cache, and work directory.
7. Registers with `--ephemeral --disableupdate`, so it accepts one job only.
8. Runs the job.
9. Deletes the entire cycle directory, including runner credentials and job files.
10. Creates a fresh worker for the next job.

The long-lived GitHub token is removed from child environments. The supervisor also marks itself non-dumpable on Linux before launching any job process, preventing same-UID job processes from reading the supervisor's process memory/environment through normal `/proc`/ptrace access.

The supervisor removes only **offline** GitHub runner registrations carrying this AMP instance's persistent unique id. It does not delete unrelated self-hosted runners.

## GitHub token

Create a **fine-grained personal access token** for the Cart Horse GitHub account only after the AMP template is installed.

For the first OldGrid instance:

- Resource owner: `carthorsestudios`
- Repository access: **Only select repositories** → `poobiverse-classic`
- Repository permission: **Administration: Read and write**
- Give it an expiration appropriate for your maintenance policy.

Enter the token directly into AMP's **GitHub Runner Administration Token** password field. Never commit it, store it in this public repository, or paste it into chat.

The token is used only to call GitHub's repository self-hosted-runner administration endpoints and mint short-lived registration tokens. Workflow jobs do not inherit it.

## Add this template repository to AMP

In ADS:

1. Open **Configuration → Instance Deployment → Configuration Repositories**.
2. Add `carthorsestudios/carthorse-ci-runner-amp-template:main`.
3. Fetch/update the repository.
4. Refresh AMP if needed.
5. Create a **Cart Horse CI Runner** Generic Module instance.
6. Keep the instance containerized; this template marks Docker/container isolation as required.

No inbound game/server port is exposed by this template. The runner only needs outbound HTTPS access to GitHub and the package registries/download hosts used by project workflows.

## OldGrid migration sequence

Do **not** change OldGrid's `runs-on` until the AMP runner is installed and shows `Ready` in its console and as an idle self-hosted runner in GitHub.

The first migration should be deliberately conservative:

- Move only OldGrid's `Build and verify (ubuntu)` job from `ubuntu-24.04` to `[self-hosted, linux, x64, carthorse-ci, oldgrid]`.
- Leave `Publish verified release` on GitHub-hosted Ubuntu initially.
- Keep all existing tests and gates.
- Stop uploading the ~53 MB private package candidate on pull requests; only main needs that candidate for publication.

After the self-hosted verifier is proven stable, other workloads can be migrated separately.

## Toolchain flexibility

The base container supplies Linux, Python, Git, curl, archive utilities, and the native libraries required by the GitHub runner. Project workflows remain free to use standard setup actions such as `actions/setup-node`, `actions/setup-python`, and project-specific installers.

A normal addition of tests, TypeScript modules, Python scripts, packaging checks, or validators does **not** require changing this AMP template. A template change is only likely when a project begins requiring a materially new host capability such as Docker service containers, GPU tooling, Windows-specific compilation, or another privileged system facility.

## Runner updates

`Runner Version` defaults to `latest`. The supervisor refreshes GitHub's official latest-release metadata at most once per hour and caches verified runner archives by version. Each ephemeral runner is configured with `--disableupdate`; version changes happen between jobs under supervisor control rather than mutating a live worker during a job.

For troubleshooting, `Runner Version` can be pinned to an exact `N.N.N` release.

## Local/static validation

Run:

```bash
python3 tools/validate_template.py
```

The validator checks the AMP template identity, container requirement, secret field configuration, supervisor compilation, pinned bootstrap SHA-256, and the key ephemeral/credential-isolation invariants.

## Important limitations

- One AMP instance serves one GitHub repository.
- One job runs at a time per instance.
- This is a trusted-private-repository runner. Do not point it at a public repository that accepts untrusted fork pull requests.
- The AMP container is the host isolation boundary. Do not mount production game data into the CI instance.
- Docker-based Actions/service containers are not part of the initial OldGrid configuration. If a future workflow genuinely needs Docker inside CI, treat that as an explicit infrastructure change rather than mounting the host Docker socket into this runner.
