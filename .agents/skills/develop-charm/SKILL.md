---
name: develop-charm
description: Make a requested change to a Juju charm and verify it with the charm's checks and available integration tests. Use for charm bug fixes, compatibility work, and other code changes.
---

# Develop and verify a charm change

## Goal

Make the requested change in the maintainer's charm repository, run the most
relevant checks available, and produce an evidence-based report. The report
must distinguish verified results from checks that were skipped, blocked, or
not run. Do not assign a broad charm quality score.

## Start with the request and repository

1. Clarify the requested behavior and expected outcome if they are not specific.
   Ask for the charm repository URL or checkout path if it is not clear from the
   conversation or available workspace.
2. Use an existing checkout at
   `$PROJECT_ROOT/development-sandbox/workspaces/` when available. For a remote
   repository without a checkout, clone it into that directory using a
   descriptive repository name. Do not replace an existing directory or discard
   uncommitted work.
3. Confirm the repository, branch, and working-tree state before editing. Keep
   all charm changes inside the charm repository; do not modify the parent
   `charm-integration-testing` checkout as part of the charm change.
4. If the checkout has uncommitted changes, inspect them and preserve them.
   Do not overwrite conflicting edits. Ask before switching branches, discarding
   changes, or performing any operation that could lose work.

## Inspect and make the change

1. Identify the charm framework, build process, metadata, supported bases,
   dependencies, and existing unit, static, and integration tests. Follow the
   charm repository's own development documentation and conventions.
2. Inspect relevant code and tests, then make the smallest complete change that
   addresses the request. Add or update tests for the changed behavior.
3. Explain the intended change and validation plan before any consequential
   action that was not clearly requested. Do not publish, release, or push
   changes unless explicitly authorized.

## Verify

1. Run the charm's documented formatting, lint, unit, and build checks that are
   relevant and available. Record the exact command, exit status, and concise
   result for each. Do not describe a build or unit test as integration
   evidence.
2. When the integration suite can exercise the requested behavior, build the
   changed charm to a `.charm` artifact and pass it to
   `scripts/run-tests.sh` with `--target-charm-file <path>` and its metadata
   name via `--target-charm <name>`. This deploys the local artifact instead
   of resolving that target from Charmhub. Do not specify target channel or
   revision options with a local artifact.
3. Capture the artifact SHA-256, exact charm revision or source commit, Juju
   version, charm base, substrate, and scenario. Upgrade/downgrade checks run
   between the artifact and the default Charmhub release (or
   `--target-downgrade-revision` / `--target-downgrade-charm-file`); report any
   that skip, such as on a base mismatch, as skipped. If the available test path cannot deploy the exact built artifact, state
   that local-source integration coverage is not run; do not substitute a
   published revision and claim it validates the change.
4. Separate charm failures from environment failures. If provisioning,
   bootstrap, deployment, or test infrastructure prevents a check from
   completing, report it as blocked with the relevant diagnostics, not as a
   charm failure or a pass.
5. Preserve useful command output, test logs, and build artifacts in the report
   directory. Do not include credentials, secret values, or unrelated user data.

## Evidence report

Create a report directory at:

```text
$PROJECT_ROOT/development-sandbox/reports/<charm>-<YYYYMMDD-HHMMSS>/
```

Write `report.md` there and keep supporting logs or test artifacts beside it.
Include:

- Charm repository, branch, starting revision, and resulting working-tree
  revision or dirty state.
- Requested change and a concise list of files changed.
- A results table with check, exact command or scenario, result
  (`PASS`, `FAIL`, `BLOCKED`, `SKIPPED`, or `NOT RUN`), and evidence path.
- For deployment or integration coverage: exact charm revision, Juju version,
  charm base, substrate, and tested scenarios. Mark unknown values as unknown.
- Failure and limitation details, including checks that could not run and why.
- A conclusion scoped to the evidence, for example which checks passed for the
  tested Juju/base/substrate combination. Do not infer overall charm quality or
  compatibility outside the tested scenarios.

The report and artifacts persist on the host under
`development-sandbox/reports/`. Summarize the report path and key results when
handing work back to the maintainer.
