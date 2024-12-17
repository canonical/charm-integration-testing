# Contributing to Charm Integration Testing

## Introduction

This document provides the information needed to contribute to Charm Integration Testing.

## General recommendations

Following the [README Getting Started](README.md#getting-started) guide to clone and download
dependencies.

## Linting and Formatting

Linting and formatting is enforced by [ruff](https://docs.astral.sh/ruff) and
[mdformat](https://mdformat.readthedocs.io) as a GitHub Action that runs automatically on Pull
Requests to this repository. To format, run:

```bash
./scripts/format.sh
```

## Tooling and Continuous Integration

...

## Project Structure

...

### Workflows ([test_scheduler/workflows](test_scheduler/workflows))

....

## Testing

### Unit Tests ([tests/unit](tests/unit))

```bash
./scripts/unit-tests.sh
```

Unit tests should be added to document and ensure the accuracy of individual pieces of code,
especially those that are heavy in logic with many possible outcomes. Preference is for
[Stubs](https://spring.io/blog/2007/01/15/unit-testing-with-stubs-and-mocks) with Dependency
Injection as opposed to Mocks.

### Integration Tests ([tests/integration](tests/integration))

## Pull requests

### Code review criteria & workflow

As a general rule, there should be one reviewer per PR. A review that requests changes should
provide sufficient depth so that the proposer can bring the code to an acceptable standard without
input from other reviewers. In certain infrequent situations, it may be necessary for a reviewer to
request that another reviewer provide additional guidance. This must be explicitly communicated in
the PR comments. Examples of when this might be done include working around cases of low test
coverage or when the changes affect something known to be of low quality (e.g., something
significantly complex and hard to reason about, brittle, dated, known to have broken in the past,
etc.).

On approving a change without any change requests the reviewer will merge the pull request. If they
choose not to perform the merge, they must leave a comment explaining the rationale (mostly this
exception is to cover situations when significant changes need to be staged across multiple
releases).

The reviewer is encouraged to use suggestions to communicate exact intended solutions, and to make
it easy to apply them. The reviewer _must_ do this when making trivial style related suggestions.
The reviewer might also post code into the PR, or to a branch branched off from the feature branch,
to communicate more complex suggestions.

Whenever a maintainer provides a review for a PR, they accept responsibility to follow the PR
through to its conclusion.

The following sections describe the criteria upon which maintainers will base their determination
among "Approve", "Request Changes", and "Comment".

#### Reviewer will approve the pull request when...

1. They have read and understood the pull request description and changes being proposed.
1. The requirements laid out [in the PR template](.github/pull_request_template.md) are met. In
   particular:

- the reviewer is convinced the changed code works as advertised.
- tests introduced cover the new functionality, as well as untouched code it may affect.
- testing reported by the author covers the new functionality, as well as untouched code it may
  affect.
- if needed, reviewer has tested the changed solution locally.

If the PR has no problems to address that requires actions from its contributor, reviewer merges it
upon approval (and deletes the feature branch).

When non-blocking issues are encountered by the reviewer, they mark the PR "approved" and leave the
changes and resulting merge to the contributor. Approval with comments is done by the reviewer when:

- Reviewer's change requests on the PR are only at most subjective (taste is a factor, e.g. arguable
  readability benefits).
- Reviewer is proposing cleaner / simpler alternatives to some parts of the code being proposed that
  if un-addressed is not risky to address in follow-up.
- Reviewer is proposing changes that are best (safer, faster, more productive, etc) to address in
  follow-up PRs. The reviewer will in these cases file follow-up issues, and link to them _in the PR
  description_.
- The code was determined to be difficult to read, i.e. it _could_ be improved, but in the end the
  reviewer understood it (reviewer leaves a comment in these cases, but leaves it to contributor
  discretion to address).
- Reviewer believes they have not caught a problem, but asks question(s) out of curiosity or to
  confirm their understanding (on contributor then to consider the question and either make further
  changes or simply answer it).
- Reviewer points out issues unrelated to what the PR is trying to accomplish (i.e. the problem
  existed before).

#### Reviewer will "request changes" to your PR when...

1. the pull request description is unclear, or it is clear that the changes do not meet the
   requirements [in the PR template](.github/pull_request_template.md).
1. doesn't do what is claimed.
1. introduces a new bug.
1. introduces a maintenance problem.
1. the solution is unnecessarily, significantly, too complex for the problem being solved.
1. the introduced code / patch is very difficult to understand (the reviewer has doubts of
   understanding it right, or doubts that others would).
1. the PR should be split into multiple parts (is too big to safely review, i.e. may hide critical
   issues). This call is not to be done for sake of readability, it is done for safety:

- if reviewer believes it were _more elegant_ to split the PR, they should approve and comment
- if the reviewer believes to not be doing a good job reviewing it without it being split, they
  should "request changes" and in their comment request for it to be split.

#### Reviewer will "comment" if...

They are not confident in making a call, delegating explicitly in a comment to a reviewer who they
believe _can_ make a call, as quickly and as early as possible in the process.
