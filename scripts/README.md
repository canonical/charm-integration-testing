# Bundle-Builder-X Benchmark Suite

Compares bundle-builder-x result quality across two branches (typically `main` vs a feature branch).

## What it tests

- **200 single-charm specs** — every charm in `static/charm-overrides/` plus popular extras, on both kubernetes and machine platforms, with alternate-channel variants for key charms.
- **50 complex scenarios** — multi-app stacks (COS, identity platform, data platform, Temporal, Wazuh, CK), cross-model relations (CMR) across 12 different patterns, limit-capacity cases, cyclic dependencies, and at-least-one constraints.

## Quick start

```bash
cd bundle_builder_x

# Run on current branch (120s hard timeout per spec)
poetry run python ../scripts/benchmark_suite.py run \
    --output ../results/$(git rev-parse --abbrev-ref HEAD | tr '/' '-').json \
    --overrides ../static/charm-overrides \
    --workers 6

# Run on main (use a tighter timeout because main may hang for hours on some specs)
git worktree add /tmp/bb-main main
cd /tmp/bb-main/bundle_builder_x
poetry install -q
poetry run python ../scripts/benchmark_suite.py run \
    --output ../../results/main.json \
    --overrides ../static/charm-overrides \
    --workers 4 \
    --hard-timeout 90
cd -

# Compare
poetry run python ../scripts/benchmark_suite.py compare \
    ../results/main.json \
    ../results/$(git rev-parse --abbrev-ref HEAD | tr '/' '-').json
```

The compare command exits with code 1 if any regressions are found (SAT → UNSAT/TIMEOUT/ERROR).

## Options

### `run`

| Flag | Default | Description |
|------|---------|-------------|
| `--output` | required | Path for the JSON results file |
| `--overrides` | none | Path to `static/charm-overrides/` directory |
| `--charmhub-url` | env/default | Override Charmhub API base URL (e.g. for nginx cache) |
| `--workers` | 4 | Parallel subprocess workers |
| `--hard-timeout` | 120 | Per-spec wall-clock timeout in seconds. **Use 60–90 for `main`** to avoid multi-hour hangs. |
| `--category` | all | Only run specs in a specific category (`single-charm`, `cmr`, `multi-app`, `cyclic`, etc.) |
| `--limit` | all | Only run the first N specs (useful for quick smoke-tests) |

### `compare`

```
poetry run python scripts/benchmark_suite.py compare <baseline.json> <branch.json> [--verbose]
```

`--verbose` shows all rows; by default only notable rows (regressions, improvements, large speedups/slowdowns) are printed.

### `list`

```
poetry run python scripts/benchmark_suite.py list
```

Prints all 250 spec IDs with their category and platform.

## Output format

Results are saved as JSON:

```json
{
  "metadata": {
    "timestamp": "2026-06-25T...",
    "git_branch": "main",
    "git_commit": "abc1234",
    "hard_timeout": 90,
    "n_specs": 250
  },
  "results": [
    {
      "id": "single-alertmanager-k8s-k8s",
      "category": "single-charm",
      "platform": "kubernetes",
      "status": "SAT",
      "elapsed_s": 1.23,
      "n_apps": 5,
      "n_integrations": 8,
      "error": null
    }
  ]
}
```

**Status values:**

| Status | Meaning |
|--------|---------|
| `SAT` | Bundle built successfully |
| `UNSAT` | Spec is unsatisfiable (no valid bundle exists) |
| `SOLVER_TIMEOUT` | Z3 solver's per-iteration timeout fired (not the hard wall-clock timeout) |
| `TIMEOUT` | Hard wall-clock timeout expired (subprocess killed) — only expected on `main` |
| `ERROR` | Unexpected exception (import error, spec validation failure, etc.) |

## Regression criteria

The comparison treats `B` as a regression vs `A` when `B`'s status rank is worse:

```
SAT < UNSAT < SOLVER_TIMEOUT < ERROR < TIMEOUT
```

For specs where both return SAT:
- ≥2× faster → "FASTER" (improvement)
- ≤0.5× speed → "SLOWER" (flagged but not a regression — performance regressions do not exit 1)

## Running time estimate

With 6 workers and the nginx proxy cache warm:

| Scope | Estimated time |
|-------|---------------|
| All 250 specs (feature branch) | ~15–25 minutes |
| All 250 specs (main, 90s timeout) | ~30–60 minutes |
| Single-charm only (200 specs) | ~10–15 minutes |
| Quick smoke (--limit 20) | ~3–5 minutes |
