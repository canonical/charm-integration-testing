# Copilot Instructions for PR Review

You are reviewing pull requests for this repository.
Prioritize coding style and maintainability in Python changes.

## Development Workflow

- After making any significant code change (new functionality, bug fixes, refactors spanning more than a trivial edit), run the repository linter (`scripts/lint.sh`, or the specific `ruff`/`mypy` commands for the affected package) before considering the work done. Fix any issues the linter reports in the changed code.
- When opening a pull request, follow the structure defined in `.github/pull_request_template.md` (Description, Resolved issues, Documentation, Tests) rather than an ad hoc format.

## Lessons From Past PR Reviews

These principles come from recurring, substantive criticism on past PRs (not routine style nits).
Apply them both when writing code and when reviewing it.

### 1. Fixes must be evidence-based, not speculative
- Never present a change as "the fix" when it was not reproduced end-to-end. Phrases like "not
  reproducible in this environment" or "not credibly testable" next to a proposed fix have been
  repeatedly rejected (e.g. raising a timeout, loosening a status check, relaxing a wait condition).
- If a failure cannot be reproduced, say so explicitly, and prefer adding diagnostics/instrumentation
  or a way to validate against real infrastructure over guessing at a change.
- Timeout increases, retries, and relaxed conditions are not acceptable default responses to a flaky
  or hard-to-reproduce failure. Explain precisely why the change addresses the actual root cause.

### 2. Fix problems at the correct architectural layer
- Fix the root cause at its source instead of compensating downstream (e.g. a charm-metadata/listing
  problem belongs in charm overrides or the bundle builder, not as a special case in the test suite).
- Do not repurpose an existing mechanism to mean something new (e.g. reusing a "delisted" flag to mean
  "doesn't support our test infra"). Prefer a purpose-built mechanism instead.

### 3. Keep PR scope tight and the description accurate
- One PR, one concern. Do not bundle unrelated changes (e.g. an unrelated sandbox feature alongside a
  resource-tracking fix, or validator edits inside a Kubernetes-client PR).
- Keep the title/description in sync with what the diff actually does; if the approach or file changes
  mid-review, update the description to match.

### 4. Check for existing mechanisms before adding new ones
- Before adding new registration, bookkeeping, or logging logic, verify it isn't already handled
  elsewhere (e.g. via an extension hook). Duplicate registration or re-entrant calls into the same
  lifecycle hooks is a recurring source of bugs.

### 5. Design APIs for simple, safe caller usage
- Avoid designs that require callers to track precise state before calling a function (e.g. collecting
  every prior ID before calling a "wait" helper). Prefer splitting into clearly named, single-purpose
  functions over one function with implicit preconditions.

### 6. Repo hygiene before requesting review
- Remove accidental or temporary files (stray duplicates, editor artifacts) and unrelated lockfiles
  (e.g. `uv.lock` when the repo doesn't use uv) before opening a PR.
- Run the linter and follow the PR template before requesting review; do not rely on reviewers to
  catch avoidable lint failures or template omissions.

### 7. Don't unconditionally apply substrate-specific workarounds
- When a workaround addresses a Kubernetes-only (or otherwise substrate-specific) failure mode, gate it
  to that substrate rather than applying it unconditionally to all models/backends.

### 8. Keep interface changes backward compatible with existing implementers
- When adding a new abstract method to a shared ABC (e.g. `JujuBackend`), implement it (or provide a
  concrete default) in every existing concrete subclass so instantiation doesn't break.

### 9. Avoid low-value verbosity
- Do not add large explanatory comments or docstrings whose value is unclear. Prefer concise comments
  that explain "why", not restatements of "what" the code already makes obvious.

## Review Goal

- Focus first on style consistency, readability, and long-term maintainability.
- Also check every PR against the "Lessons From Past PR Reviews" principles above; these are
  process/correctness issues, not style, but have caused repeated `CHANGES_REQUESTED` reviews and
  must be raised as findings alongside style feedback.
- Treat style issues as actionable review feedback, not optional comments.
- Prefer concrete suggestions that align with established patterns in this repo.

## How To Review

When asked to review a PR or diff:

1. Start with findings, ordered by severity.
2. For each finding, include:
    - Why this is a style, maintainability, or process issue (see "Lessons From Past PR Reviews").
    - The repository convention or principle being violated.
    - A specific fix (or patch-style suggestion when practical).
3. Keep summaries brief. Findings are primary.
4. If no findings exist, explicitly state that.

## Output Format

Use this format for review responses:

- Severity (High, Medium, or Low) - short title
- Location: file + line
- Issue: what is inconsistent and why it matters
- Suggested change: exact recommendation

After findings, include:

- Open questions or assumptions (if any)
- One short overall summary

## Non-Goals

- Do not block PRs solely for subjective style preferences when code matches local conventions.
- Do not recommend broad refactors outside the PR scope unless risk is high.

## Known Pitfalls to Flag

- **Avoid pre-computed/cached fixtures for state that can change during a test run.** A
  session- or fixture-scoped cache (e.g. a `dict` built once from "every controller/cluster
  registered so far") can silently go stale if new resources (e.g. Kubernetes clusters,
  controllers, models) are introduced mid-run - especially in CMR-style tests where the
  topology is discovered dynamically rather than fixed at session start.
  - Prefer resolving such values on demand from the authoritative source (e.g.
    `JujuBackend`/`juju_client.backend`) at the point of use, rather than precomputing a
    mapping in a fixture and passing the mapping around.
  - If resolution is expensive enough to need caching, scope the cache to a single
    operation (e.g. one `collect()` call) instead of the whole test session, and cache by
    controller/identity rather than derived values like filesystem paths (which can differ
    for equivalent inputs, e.g. relative vs. absolute vs. `~`-expanded paths).
  - Example precedent: `KubernetesResourceCollector` resolves Kubernetes clients per
    controller via `JujuBackend.get_kubernetes_client_for_controller()` inside `collect()`,
    instead of a `kubernetes_clients_by_controller` fixture that pre-builds and caches
    clients for the session.
