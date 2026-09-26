# Cart Horse CI Runner — AMP Template

A generic, repository-scoped GitHub Actions self-hosted runner for Cart Horse Studios, supervised as a dedicated CubeCoders AMP instance.

This template is **CI infrastructure**, not an OldGrid-specific test harness. GitHub workflows remain the source of truth for what a project builds and tests. If OldGrid grows from 60 checks to 100 checks, the runner normally needs no change; GitHub simply sends the revised job to the same worker.

## Intended deployment

Start with one dedicated instance:

- AMP instance: `Cart Horse CI - OldGrid`
- Repository: `carthorsestudios/poobiverse-classic`
- Custom labels: `carthorse-ci,oldgrid`
- Concurrency: one job at a time
- Container: required
- Suggested initial allocation: 4 CPU cores / 8 GB RAM

A later Scratch MMO instance can use this same template with its own repository registration and labels such as `carthorse-ci,scratch-mmo`.

## Security model

The runner is repository-scoped and container-required. **No long-lived GitHub PAT is stored on the server.**

Initial setup uses the time-limited registration token GitHub displays on the repository's **Settings > Actions > Runners > New self-hosted runner** page. GitHub documents that this token expires after one hour and is used by `config.sh` to register the runner.

After registration:

1. The runner remains registered only to the selected repository.
2. The setup token is removed from child environments and is no longer needed.
3. The listener is launched with `run.sh --once`, so it accepts one job and exits.
4. Each job gets a fresh HOME, temp directory, and tool cache.
5. The GitHub Actions `_work` directory is deleted and recreated after every job.
6. The job-specific HOME/temp/tool-cache directory is deleted after every job.
7. The persistent runner registration files are fingerprinted before jobs begin. If a job changes them, the supervisor fails closed instead of starting another job.
8. AMP container isolation remains the boundary protecting the host and production game instances.

The runner software uses GitHub's normal automatic self-update mechanism after registration. The initial download is selected from the official `actions/runner` release and verified against GitHub's published SHA-256 digest.

The supervisor makes no authenticated repository-administration API calls after this redesign.

## One-time registration token

Do **not** create a PAT for this runner.

For OldGrid:

1. Open `carthorsestudios/poobiverse-classic` on GitHub.
2. Go to **Settings > Actions > Runners**.
3. Click **New self-hosted runner**.
4. Select Linux and x64.
5. GitHub will show a setup command containing `--token <temporary value>`.
6. Copy only that temporary token value into AMP's **Initial Registration Token** password field.
7. Start the AMP instance.
8. After the console reports `Ready`, clear the **Initial Registration Token** field in AMP. The token also expires automatically after one hour.

Never paste the temporary token into chat or commit it.

If the local runner registration is ever deliberately removed or the CI instance is recreated from scratch, generate a new one-hour registration token and repeat the registration step.

## Add this template repository to AMP

In ADS:

1. Open **Configuration > Instance Deployment > Configuration Repositories**.
2. Add `carthorsestudios/carthorse-ci-runner-amp-template:main`.
3. Fetch/update the repository.
4. Refresh AMP if needed.
5. Create a **Cart Horse CI Runner** Generic Module instance.
6. Keep the instance containerized; this template marks Docker/container isolation as required.

No inbound game/server port is exposed. The runner only needs outbound HTTPS access to GitHub and the package registries/download hosts used by project workflows.

## OldGrid migration sequence

Do **not** change OldGrid's `runs-on` until the AMP runner is installed and shows `Ready` in its console and as an idle self-hosted runner in GitHub.

The first migration is deliberately conservative:

- Move only OldGrid's `Build and verify (ubuntu)` job from `ubuntu-24.04` to `[self-hosted, linux, x64, carthorse-ci, oldgrid]`.
- Leave `Publish verified release` on GitHub-hosted Ubuntu initially.
- Keep all existing tests and gates.
- Stop uploading the ~53 MB private package candidate on pull requests; only main needs that candidate for publication.

After the self-hosted verifier is proven stable, other workloads can be migrated separately.

## Toolchain flexibility

The base container supplies Linux, Python, Git, curl, archive utilities, and the native libraries required by the GitHub runner. Project workflows remain free to use standard setup actions such as `actions/setup-node`, `actions/setup-python`, and project-specific installers.

Normal additions of tests, TypeScript modules, Python scripts, packaging checks, or validators do **not** require changing this AMP template. A template change is only likely when a project begins requiring a materially new host capability such as Docker service containers, GPU tooling, Windows-specific compilation, or another privileged system facility.

## Runner updates

`Initial GitHub Actions Runner Version` defaults to `latest` and is used only when the runner is first installed. After registration, GitHub's standard self-hosted-runner automatic update behavior remains enabled so the runner does not age out of supported versions.

## Local/static validation

Run:

```bash
python3 tools/validate_template.py
```

The validator checks template identity, container isolation, the absence of a long-lived administration PAT, supervisor compilation/unit tests, the pinned bootstrap SHA-256, one-job-at-a-time behavior, workspace scrubbing, and registration-file tamper detection.

## Important limitations

- One AMP instance serves one GitHub repository.
- One job runs at a time per instance.
- This is a trusted-private-repository runner. Do not point it at a public repository or use it for untrusted fork pull requests.
- The AMP container is the host isolation boundary. Do not mount production game data into the CI instance.
- A persistent self-hosted runner is not equivalent to a fresh GitHub-hosted VM. This design scrubs the normal job state and detects changes to runner registration files, but repository workflows must still be treated as trusted.
- Docker-based Actions/service containers are not part of the initial OldGrid configuration. If a future workflow genuinely needs Docker inside CI, treat that as an explicit infrastructure change rather than mounting the host Docker socket into this runner.
