# Copilot Instructions for PR Review

You are reviewing pull requests for this repository.
Prioritize coding style and maintainability in Python changes.

## Development Workflow

- After making any significant code change (new functionality, bug fixes, refactors spanning more than a trivial edit), run the repository linter (`scripts/lint.sh`, or the specific `ruff`/`mypy` commands for the affected package) before considering the work done. Fix any issues the linter reports in the changed code.
- When opening a pull request, follow the structure defined in `.github/pull_request_template.md` (Description, Resolved issues, Documentation, Tests) rather than an ad hoc format.

## Review Goal

- Focus first on style consistency, readability, and long-term maintainability.
- Treat style issues as actionable review feedback, not optional comments.
- Prefer concrete suggestions that align with established patterns in this repo.

## How To Review

When asked to review a PR or diff:

1. Start with findings, ordered by severity.
2. For each finding, include:
	- Why this is a style or maintainability issue.
	- The repository convention being violated.
	- A specific fix (or patch-style suggestion when practical).
3. Keep summaries brief. Findings are primary.
4. If no style findings exist, explicitly state that.

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
