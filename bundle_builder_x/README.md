# bundle\_builder\_x

Solver-based bundle builder for Juju charms. Given a YAML spec listing models,
applications, and integrations, it fetches charm metadata from Charmhub and uses
a Z3 SMT solver to produce minimal, valid bundles.

## Quick start

```bash
poetry run bundle-builder-x \
    --spec my-spec.yaml \
    --overrides static/charm-overrides \
    --output-bundles output/ \
    --log-level INFO
```

## What it does

- Reads a multi-model spec file describing which charms you want and how they
  connect.
- Fetches charm metadata from Charmhub, applying local per-charm overrides
  (endpoint constraints, config defaults, proxy declarations).
- Encodes everything as Z3 constraints and solves for a valid bundle.
- Iteratively expands the domain when the solver finds the problem unsatisfiable
  (e.g. a required endpoint has no matching charm yet).
- Outputs per-model Juju bundle YAML and optionally a Mermaid diagram.

## Spec file

A minimal spec:

```yaml
models:
  - name: my-app
    platform: kubernetes
    applications:
      db:
        charm: postgresql-k8s
      app:
        charm: kratos
```

See the [spec file reference](../docs/reference/spec-file.md) for the full format.

## Documentation

- [How to use bundle builder X](../docs/how-to/use-bundle-builder-x.rst) --
  CLI usage, Python API, and output format.
- [Algorithm explanation](../docs/explanation/bundle-builder-x-algorithm.rst) --
  how the solver loop works and how it differs from the original graph-search
  builder.
- [Constraint DSL reference](../docs/reference/constraint-dsl.md) --
  the expression language used in per-charm override files.
- [Spec file reference](../docs/reference/spec-file.md) --
  full field reference and validation rules.

## Module layout

```
bundle_builder_x/
  spec.py              # Pydantic models for the spec file
  bundle_builder.py    # Main solve loop (domain expansion + Z3)
  domain.py            # Z3 variable declarations (the "domain")
  domain_builder.py    # Builds the initial domain from a spec
  constraints.py       # Generates Z3 assertions from the domain
  constraints_dsl.py   # Parses and compiles the constraint DSL
  dsl_lowering.py      # Lowers DSL AST nodes to Z3 expressions
  extract.py           # Extracts a Solution from a Z3 model
  bundle.py            # Output data models (Bundle, Solution)
  charm.py             # Charm metadata types
  charmhub.py          # Charmhub client (with override merging)
  charmhub_http.py     # Raw HTTP calls to the Charmhub API
  overrides.py         # Per-charm override loading
  assertion_tags.py    # Encode/decode tags for unsat core analysis
  entrypoint.py        # CLI entry point
  snapstore.py         # Snap store client (Juju version resolution)
  timing.py            # Build timing / timeline support
```

## Tests

```bash
# Unit and logic tests (fast, no network)
poetry run python -m pytest bundle_builder_x/tests/unit/ bundle_builder_x/tests/logic/ -q

# Integration tests (hits Charmhub API)
poetry run python -m pytest bundle_builder_x/tests/integration/ -v
```
