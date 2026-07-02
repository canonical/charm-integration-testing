---
description: "Use when reviewing or implementing validator packages under validators/."
applyTo: "validators/**/*.py"
---

## Validator Framework Rules

Each interface validator lives in `validators/<interface_name>/` as an independent Python package.

### Package structure

- Subclass `BaseValidator` from `validators.base`.
- Register via `pyproject.toml` under the `endpoint_validators` entry-point group:
  ```toml
  [project.entry-points."endpoint_validators"]
  postgresql_client = "validators.postgresql_client:PostgreSQLClientValidator"
  ```
- The entry-point **name must exactly match the Juju interface name** (e.g. `postgresql_client`).
- Validator sub-packages use `setuptools`, not Poetry.

### Validation levels

`ValidationLevel = Literal["simple", "deep", "uat"]`

- `simple` — connectivity and schema checks; no side effects.
- `deep` — read/write canary operations; must clean up after itself.
- `uat` — optional; falls back to `deep` automatically if not implemented.
- Use `_skipped_result_due_to_level(level)` and `_skipped_result_due_to_role(level, role)` helpers — never return a custom `SKIPPED` result.
- Guard role at the top of `validate()`: skip validators that only apply to `"requires"` when `self.role` is `"provides"` or `"peer"`.

### Credential resolution

Always resolve credentials via `self.resolve_secret(uri_key, *fields)` — this handles both plain databag fields and Juju secret URIs.
Flag any direct read from `self.databag` that bypasses `resolve_secret` for credential fields.

### Result construction

Always build `ValidationResult` via `self._make_result(level=..., checks=[...])`.
Let it compute `status` from checks unless you need `ERROR` — use `self._error_result(level, "<error message>")` for that case.
Flag hand-constructed `ValidationResult(...)` calls that bypass these helpers.

### Canary tables (deep validation)

Canary table names must be UUID-generated (not user-controlled). Mark with `# nosec B608`.
Use parameterised queries (`%s`) for all user-supplied values — never string interpolation.
Canary tables must always be dropped in a `finally` block.

### Data modeling

Validator sub-packages use stdlib `dataclasses` for structured data.
`validators/base` uses Pydantic `BaseModel` (`ValidationResult`, `ValidationCheck`).
Match the existing style in the package you are working in.
Pydantic v2 API only: `.model_dump()` not `.dict()`; `.model_validate()` not `.parse_obj()`.
