# Contributing to Charm Integration Testing

## Introduction

This document provides the information needed to contribute to Charm Integration
Testing.

## General recommendations

Install mise as described in the [README](README.md#getting-started), then
prepare a workstation:

```bash
export MISE_ENV=development
mise run setup
```

### What goes where

| File | Owns |
|---|---|
| `mise.toml` | Shared locked tools and snap channels for Juju, kubectl, OpenStack clients, and LXD |
| `mise.development.toml` | Additional development tools, including linters and Node |
| `pyproject.toml` / `poetry.lock` | Project Python dependencies, pytest plugins, mypy, and type stubs |

Development adds tools to the shared runtime inventory.
`[tools]` declares isolated, locked executables;
`[bootstrap.packages]` declares machine-wide packages. Python tools such as Ruff
run in isolated environments. Mypy stays with Poetry because it uses the project's
Pydantic plugin and dependencies; linter settings remain in `pyproject.toml`.

### Explicit setup

| Command | Prepares |
|---|---|
| `mise run setup` | All selected tools and project dependencies; leaves system packages untouched |
| `mise -E development run setup` | Also installs the development tools |
| `mise run setup --bootstrap` | Also installs the declared system packages |
| `mise run setup --tools-only` | Prepares executable tools without project dependencies or system packages |

All tools in the selected inventory install during setup; there is no first-use
installer. Select development with `MISE_ENV=development` for the shell or
`mise -E development` for one command. Runtime CI uses only the base inventory.
Strict locking preserves committed versions and artifact checksums.

Project-dependent test tasks and lint share
`setup-project`, which runs Poetry installation before the task. Poetry owns
dependency reconciliation; there is no additional readiness cache.
This does not bootstrap system packages.

The CI setup action wraps the same task. Lint and workflow/security checks select
`environment: development`; runtime and unit-test jobs use the base inventory.
Jobs without project dependencies set
`install-project: "false"`. The action carries the environment into later job
steps. The test workflow supplies `JUJU_CHANNEL` at job scope for cloud setup.
Mise-action caching remains disabled.

System-package installation is explicit and operation-owned: OpenStack cloud
setup ensures Juju and `openstackclients`; Kubernetes cloud setup ensures Juju
and kubectl; Swift uploads independently ensure `openstackclients`. Cleanup
selects clients for its environment. A satisfied snap/channel is checked locally
and skipped; repeat calls do not force a refresh. Kubernetes-only cloud access
does not install OpenStack clients, although Swift reporting still requires them.

`mise run setup --bootstrap` is an opt-in full system-package preparation.
Installing system packages does not initialize substrates or register clouds.
Snaps require snapd and sudo, use `/snap/bin`, and are not revision-locked.
Snapd can auto-refresh them; applying a changed channel can also refresh an
existing daemon. Preview changes with
`mise run bootstrap-packages -- --dry-run` on an existing machine.

OpenStack operations and Swift uploads share `openstackclients`; remove any
conflicting `openstack` snap manually.

### Selecting the Juju client

For a persistent selection on this machine:

```bash
mise run juju:channel -- 4/stable
mise run juju:channel                  # Show the saved channel or repository default
juju version                          # Check the actual installed client
mise run juju:channel -- --reset       # Remove the selection and apply the default
```

The task saves only the channel in
`${XDG_CONFIG_HOME:-$HOME/.config}/charm-integration-testing/juju.toml`, outside
the checkout. In the sandbox this is VM-local, survives new shells and VM restarts,
and is shared by checkouts in the same VM. A failed channel install does not replace
the previous selection. Reset removes the selection before applying the default;
if installation fails, fix the reported error and retry.

`mise run bootstrap-packages` owns channel selection for setup and substrate tasks.
Precedence is `JUJU_CHANNEL`, then the saved selection, then `mise.toml`.
`JUJU_CHANNEL=4/edge mise run bootstrap-packages -- snap:juju` overrides one invocation without changing the
saved selection. The setup action's `juju-channel` input and test workflow's
`juju_channel` input use this temporary path; an empty input uses the saved selection
or repository default.

Selected channels refresh installed Juju before applying packages, including branch
changes hidden by snap's installed-state listing. Channels are not revision pins.
This changes only the client, not controllers or model agents; use fresh test
controllers when experimenting with another major version.

## Linting and Formatting

Run `mise run format` to apply formatting and `mise run lint` to check it.
CI runs the same lint checks.

## Tooling and Continuous Integration

### Mise tasks

File tasks own their `#MISE` metadata. `mise-tasks/` contains
shared setup and package operations.

| Task | Purpose |
|---|---|
| `mise run test:charm-integration-testing` | Run `charm_integration_testing` unit tests |
| `mise run test:validators` | Run validator unit tests |
| `mise run test:bundle-builder-x` | Run bundle builder tests |

### CI gates (all must pass on every PR)

1. **lint** — `mise run lint`
2. **charm-integration-testing-unit-tests**
3. **validators-unit-tests**
4. **bundle-builder-x-tests** (unit | integration | logic | overrides suites)
5. **charm-testing-integration-tests** — full Juju integration suite

### Juju / environment notes

- `juju status > file` exits 1 with an empty file — always pipe: `juju status | cat > file`
- `kubectl` uses the declared snap channel; `sudo k8s kubectl` also works in the sandbox
- After editing validator source code, pass `--reinstall` to `dev-validate.py` to force re-injection on units

## Documentation

The documentation is a sphinx project under the `docs` repository. It is based
on the
[documentation starter pack](https://canonical-starter-pack.readthedocs-hosted.com/latest/).
Refer to that for information on documentation settings.

For adding, editing and removing pages, use the `how-to`, `reference` and
`tutorial` directories and `rst` files inside them. For reference on RST
syntax, refer to
[the documentation starter pack cheatsheet](https://github.com/canonical/sphinx-docs-starter-pack/blob/main/docs/reference/doc-cheat-sheet.rst).

## Project Structure

### Test Suite ([charm_integration_testing/test_suite](charm_integration_testing/test_suite))

The `test_suite` module contains the list of tests to be run on a Charm
integration. The fixtures in `conftest.py` provide the client to interact with
Juju and the variables for which endpoints to test.

## Testing

Unit tests mirror the source structure under `tests/unit/`. Run all unit test suites via their respective mise tasks above before submitting a PR.

Integration tests live in `charm_integration_testing/test_suite/` and run against
live charms. Follow the [local testing guide](docs/how-to/run-and-debug-tests-locally.rst)
to run them in the sandbox.

## Pull requests

### Branch naming

| Situation | Pattern |
|---|---|
| Jira ticket | `SQT-<number>/short-description` |
| Bug fix | `fix/short-description` |
| New addition | `add/short-description` |
| Other | `<type>/short-description` |

### Code review criteria & workflow

As a general rule, there should be one reviewer per PR. A review that requests
changes should provide sufficient depth so that the proposer can bring the code
to an acceptable standard without input from other reviewers. In certain
infrequent situations, it may be necessary for a reviewer to request that
another reviewer provide additional guidance. This must be explicitly
communicated in the PR comments. Examples of when this might be done include
working around cases of low test coverage or when the changes affect something
known to be of low quality (e.g., something significantly complex and hard to
reason about, brittle, dated, known to have broken in the past, etc.).

On approving a change without any change requests the reviewer will merge the
pull request. If they choose not to perform the merge, they must leave a comment
explaining the rationale (mostly this exception is to cover situations when
significant changes need to be staged across multiple releases).

The reviewer is encouraged to use suggestions to communicate exact intended
solutions, and to make it easy to apply them. The reviewer _must_ do this when
making trivial style related suggestions. The reviewer might also post code into
the PR, or to a branch branched off from the feature branch, to communicate more
complex suggestions.

Whenever a maintainer provides a review for a PR, they accept responsibility to
follow the PR through to its conclusion.

The following sections describe the criteria upon which maintainers will base
their determination among "Approve", "Request Changes", and "Comment".

#### Reviewer will approve the pull request when

1. They have read and understood the pull request description and changes being
   proposed.
1. The requirements laid out
   [in the PR template](.github/pull_request_template.md) are met. In
   particular:

- the reviewer is convinced the changed code works as advertised.
- tests introduced cover the new functionality, as well as untouched code it
  may affect.
- testing reported by the author covers the new functionality, as well as
  untouched code it may affect.
- if needed, reviewer has tested the changed solution locally.

If the PR has no problems to address that requires actions from its contributor,
reviewer merges it upon approval (and deletes the feature branch).

When non-blocking issues are encountered by the reviewer, they mark the PR
"approved" and leave the changes and resulting merge to the contributor.
Approval with comments is done by the reviewer when:

- Reviewer's change requests on the PR are only at most subjective (taste is a
  factor, e.g. arguable readability benefits).
- Reviewer is proposing cleaner / simpler alternatives to some parts of the
  code being proposed that if un-addressed is not risky to address in follow-up.
- Reviewer is proposing changes that are best (safer, faster, more productive,
  etc) to address in follow-up PRs. The reviewer will in these cases file
  follow-up issues, and link to them _in the PR description_.
- The code was determined to be difficult to read, i.e. it _could_ be improved,
  but in the end the reviewer understood it (reviewer leaves a comment in these
  cases, but leaves it to contributor discretion to address).
- Reviewer believes they have not caught a problem, but asks question(s) out of
  curiosity or to confirm their understanding (on contributor then to consider
  the question and either make further changes or simply answer it).
- Reviewer points out issues unrelated to what the PR is trying to accomplish
  (i.e. the problem existed before).

#### Reviewer will "request changes" to your PR when

1. the pull request description is unclear, or it is clear that the changes do
   not meet the requirements
   [in the PR template](.github/pull_request_template.md).
1. doesn't do what is claimed.
1. introduces a new bug.
1. introduces a maintenance problem.
1. the solution is unnecessarily, significantly, too complex for the problem
   being solved.
1. the introduced code / patch is very difficult to understand (the reviewer
   has doubts of understanding it right, or doubts that others would).
1. the PR should be split into multiple parts (is too big to safely review, i.e.
   may hide critical issues). This call is not to be done for sake of
   readability, it is done for safety:

- if reviewer believes it were _more elegant_ to split the PR, they should
  approve and comment
- if the reviewer believes to not be doing a good job reviewing it without it
  being split, they should "request changes" and in their comment request for it
  to be split.

#### Reviewer will "comment" if

They are not confident in making a call, delegating explicitly in a comment to a
reviewer who they believe _can_ make a call, as quickly and as early as possible
in the process.
