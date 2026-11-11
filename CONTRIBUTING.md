# Contributing to Charm Integration Testing

## Introduction

This document provides the information needed to contribute to Charm Integration
Testing.

## General recommendations

For workstation setup, follow the [README](README.md#getting-started).

### What goes where

| File | Owns |
|---|---|
| `mise.toml`, `mise.lock`, `.mise/locks/` | Tool catalogue, locks and snap channels |
| `mise-tasks/setup` | Setup groups and explicit tool, package and project installation |
| `pyproject.toml`, `uv.lock` | Workspace and locked dependencies; package dependencies remain local |

Version-only tools are listed under `[tools]`; per-tool tables hold provider
metadata such as npm/PyPI dependencies. `[bootstrap.packages]` declares
machine-wide packages. Mise installs standalone tools and packages; uv manages
the workspace. The `juju-k8s-crashdump` tool is pinned to the commit containing
[canonical/juju-k8s-crashdump#21](https://github.com/canonical/juju-k8s-crashdump/pull/21).

### Setup groups

Run `mise run setup -- --help` for supported groups and options.
Groups compose, for example `mise run setup -- --group development integration`.
The `runtime` group installs runtime project dependencies; `development` also
installs development extras and shared tooling, taking precedence when combined.

Workspace packages are installed editably in both runtime and development setup,
so Python source edits are available without reinstalling. Rerun setup after
changing dependencies, entry points or other package metadata.

Groups include installation prerequisites. With explicit `--tools` requests,
name those prerequisites too, for example `--tools node npm:markdownlint-cli2`.
Tool and package names must match the catalogue; version overrides are rejected,
and strict locking preserves managed versions and artifact checksums.

Tasks do not install missing dependencies automatically. Run setup before
executing them rather than relying on host tools. Groups control installation,
not PATH isolation; installed catalogue tools are available together.
Use setup rather than bare `mise install`, which installs the entire catalogue.

The [CI setup action](.github/actions/setup-mise/action.yaml) wraps the same task
and activates installed tools for later steps. Its [workflow callers](.github/workflows/)
select the groups and clients they need; cloud access and Swift reporting install
their clients independently. Mise-action caching remains disabled.

Installing system packages does not initialize substrates or register clouds.
Snaps require snapd and sudo, use `/snap/bin`, and are not revision-locked.
Remove a conflicting legacy `openstack` snap manually before installing
`openstackclients`; the shared setup does not remove it automatically.

### Selecting the Juju client

To select a persistent Juju client channel on this machine:

```bash
mise run juju:channel -- 4/stable
mise run juju:channel                  # Show the saved channel or repository default
juju version                          # Check the actual installed client
mise run juju:channel -- --reset       # Remove the selection and apply the default
```

The selection is saved outside the checkout at
`${XDG_CONFIG_HOME:-$HOME/.config}/charm-integration-testing/juju.toml`.
Later setup runs on that machine, including jobs on persistent CI runners, use
the saved selection. Without one, setup uses the channel in `mise.toml`. A failed
install does not replace the previous selection.

The `setup-mise` action applies a nonempty `juju-channel` input before
installing groups. The charm-testing workflow forwards `juju_channel`;
omitted or empty inputs preserve the saved selection or catalogue default.
The action does not use a `JUJU_CHANNEL` environment variable.

Channels are not revision pins and do not change controllers or agents; use fresh
test controllers when trying another major version.

## Linting and Formatting

Run `mise run format` to apply formatting and `mise run lint` to check it.
CI runs the same lint checks.

## Tooling and Continuous Integration

### Mise tasks

File tasks own their `#MISE` metadata. `mise-tasks/` contains
shared setup and package operations.

List available tasks, descriptions and aliases directly from that metadata:

```bash
mise tasks --extended
```

Use `mise tasks info <task>` to inspect an individual task.
File-name task names and the existing longer aliases remain supported.

### CI checks

The [PR workflows](.github/workflows/) define the lint, unit-test and live
integration checks. Check their results before requesting review.

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

Unit tests mirror the source structure under `tests/unit/`. Use
[task discovery](#mise-tasks) to find their entrypoints and run all unit test
suites before submitting a PR.

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
