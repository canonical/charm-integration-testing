---
name: file-pr
description: Prepare or open a focused GitHub pull request with accurate evidence and repository-specific conventions. Use when asked to create a PR or prepare its title and description.
---

# Task: prepare or file a pull request

## Goal

Present a reviewable, maintainable change with an accurate repository-specific
PR description and validation record. A PR is not ready merely because the code
works locally: verify the change, target branch, template, and authorization
before publishing it.

## Before preparing the PR

1. Read `../file-as-agent/SKILL.md`. Confirm the target repository, base branch,
   source branch, and requested action. Do not assume the current checkout is the
   PR target. If the code originated in another repository or standalone charm,
   inspect the destination's current implementation and adapt to its APIs and
   architecture; do not copy files blindly.

2. Read the target repository's contribution instructions and pull request
   template, including `.github/pull_request_template.md` and
   `.github/PULL_REQUEST_TEMPLATE/`. If a template exists, preserve its headings,
   comments, checklist items, and order. Do not assume a template from another
   repository applies.

3. Inspect the complete diff, recent nearby PRs, related issues, and open PRs
   touching the same behavior or shared API. Compare diffs where work overlaps.
   Reuse existing mechanisms and agree on semantics rather than introducing a
   competing abstraction.

4. Confirm the change is at the correct architectural layer and the PR contains
   one concern. Check public API compatibility, concrete implementations,
   resource/lifecycle ownership, documentation, tests, and accidental files.
   Refactor when it clarifies responsibilities or makes domain rules testable;
   do not add a helper/module or copy a maintainer refactor without understanding
   why it helps this change.

5. For a bug-fix PR, establish what was reproduced and how the fix was verified.
   Prefer a baseline reproduction followed by the same live scenario with the
   patched artifact when feasible; use `reproduce-charm-bug` and
   `fix-charm-bug` for that work. If the live test was blocked or not run, say
   so plainly and report the phase reached. Do not attribute upstream or
   previous-branch test results to this patch.

## Drafting

- Use the target template verbatim and answer each applicable field directly.
- Keep the title short and specific. Describe the observable problem, the
  behavior changed, and why. Link the issue using the target repo's convention.
- In Tests, list exact commands with their actual result. Distinguish unit,
  lint/type/build, integration, and live deployment evidence. For live tests,
  state the tested revision/artifact, substrate, phase reached, and full success
  conditions checked. A bootstrap or deploy failure is not a pass for the
  operation under test.
- State whether docs were updated. Check how this repository actually handles
  changelogs; do not create a new changelog file just because a generic
  checklist mentions one.
- Keep paragraphs concise and avoid line-wrapping prose manually when GitHub
  renders it normally. Link detailed reports rather than copying them into the
  description.
- Include the verified AI disclosure according to `file-as-agent` and the target
  repository's rules.

## Create, push, and verify

- Re-run relevant checks after the final code changes. Review `git diff
  --check`, status, and the complete staged diff. Ensure temporary files,
  unrelated lockfiles, generated artifacts, and secrets are absent.
- Respect the repository's commit signing and message conventions. Never mask
  or replace global Git configuration just to make a commit succeed. Include
  the required Copilot co-author trailer when committing unless the user opts
  out.
- Do not push without explicit user authorization. If authorized, push only the
  specified branch/repository. A force push needs explicit force-push
  authorization; use a lease and stop if authorization fails.
- Create the PR only when requested and when the branch is available remotely.
  Use the target template, choose draft status if requested, and verify the
  resulting PR URL, base/head, title, and body.
- If the user asks only for a draft description or local preparation, do not
  publish anything.

## Learning from canonical/service-mesh#890

The maintainer's follow-up commits clarified the Istio CNI ownership boundary by
extracting independently testable ownership helpers, naming resource checks
explicitly, and separating charm-owned cleanup from release of a shared claim.
The cleanup behavior itself had already been corrected after a reproduced live
failure. Use the refactor as a maintainability lesson: make ownership and
cleanup boundaries explicit, and test malformed or foreign claims when relevant.
Do not treat the exact helper/module layout as a universal requirement.

## Output

- PR URL if opened; otherwise the local branch and ready-to-use title/body.
- Target repository, base/head, and draft status.
- Files/change summary and exact validation commands/results.
- Any blocked or unverified behavior, and whether changes were pushed.
