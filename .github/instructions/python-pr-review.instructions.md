---
description: "Use when reviewing Python changes for style, maintainability, and repository consistency."
applyTo: "**/*.py"
---

Follow these Python-specific review expectations.

## Architecture and Layering

This is a multi-package monorepo. Dependency direction flows strictly bottom-to-top:

1. **Data / Transport** — frozen dataclasses and Pydantic models. No imports from higher layers.
2. **Backend** — ABCs and their concrete implementations (`JujuBackend`, `KubernetesBackend`, `BaseValidator`). Depend only on data models.
3. **Client / Facade** — orchestration classes that accept backends and extensions via constructor injection (`JujuClient`, `KubernetesClient`, `ValidatorRunner`). Depend on backend ABCs and data models.
4. **Extension** — lifecycle hooks injected into clients (`JujuExtension` subclasses). Depend on backend ABCs.
5. **Bundle Building** — standalone sub-system (`bundle_builder` package). Depends on `CharmhubClient` and Pydantic models; no dependency on Juju layers.
6. **Test Suite** — top-level orchestration. The only layer allowed to depend on all others.

Flag cross-layer violations: e.g. a data model importing a client, or a backend constructing an extension internally.

### Juju layer conventions

**`JujuBackend` (ABC)** expresses what an ideal Juju would natively provide. It should contain no workarounds, compensating logic, or implementation detail. If Juju were a perfect API, every method here would map cleanly onto it. Flag any PR that adds workaround behaviour directly to the ABC.

**`JujuClient`** is the single place for high-level orchestration and all logging. It delegates raw operations to the backend and calls extensions in sequence. Logging in a backend or extension is a style violation — it belongs in `JujuClient`.

**Extensions (`JujuExtension` subclasses)** are wired exclusively through `JujuClient`. They must never be invoked directly from a backend or from test code. Flag any direct extension call that bypasses `JujuClient`.

**`JubilantBackend`** is the active `JujuBackend` implementation. It inherits `JujuCmdBackend` but `JujuCmdBackend` is deprecated. When a new or changed behaviour is needed:
- Add or override it in `JubilantBackend`.
- Delete the corresponding method from `JujuCmdBackend` if it is no longer needed there.
- Do not modify `JujuCmdBackend` to add new functionality.

`JubilantBackend` is also permitted to depend on other infrastructure (e.g. `KubernetesClient`) when Juju itself cannot fulfil the operation. This is an intentional escape hatch for gaps in the Juju API, but it belongs in `JubilantBackend`, not in the ABC.

**`NullJujuBackend`** (in `charm_integration_testing/tests/unit/extensions/shared.py`) is the required base for all `JujuBackend` test stubs. It implements every abstract method as `NotImplementedError`, so individual test stubs only need to override the methods they exercise. Flag backend stubs that implement `JujuBackend` directly instead of extending `NullJujuBackend`.

## Typing and Signatures

- Require explicit type hints for public functions and methods.
- Prefer modern Python typing syntax:
  - `str | None` over `Optional[str]` in new code.
  - Built-in generics (`list[str]`, `dict[str, Any]`) over typing module aliases where possible.
- Flag untyped or weakly typed interfaces unless there is a clear boundary reason.

## Imports and Formatting

- Keep imports sorted and grouped in the Ruff style configured by this repo.
- Preserve line length at 120 characters.
- Avoid unnecessary aliases and unused imports.

## Data Modeling

- Prefer dataclasses or Pydantic models for structured data instead of ad-hoc dictionaries.
- Keep model responsibilities focused:
  - Transport/validation structures → Pydantic models.
  - Immutable domain-like structures → frozen dataclasses.
- In the `bundle_builder` package, prefer `@immutable_dataclass` over `@dataclass(frozen=True)` for domain models — it additionally supports `@cached_method` and `@computed_property`.
- `@serializeable_dataclass` is deprecated alongside `JujuCmdBackend`. Do not use it in new code. Existing uses are expected to disappear as `JujuCmdBackend` is phased out.
- Flag mutable defaults and recommend default factories.

## Test Style

- Use clear Arrange/Act/Assert flow with GIVEN/WHEN/THEN comments.
- Use descriptive parameterized cases with named `Params` dataclasses.
- Keep assertions deterministic.
- Prefer extending nearby test patterns over introducing a new style.

## Test Doubles

- Prefer explicit typed stubs/fakes for core behavior tests.
- Treat `MagicMock`, broad `patch`, and broad `monkeypatch` as last-resort tools for third-party boundaries that are hard to stub safely.
- Keep tests deterministic and easy to reason about.

## Dependency Injection

- Prefer dependency injection for clients, backends, loggers, paths, timeouts, and retriers.
- Flag hidden coupling when business logic constructs concrete dependencies internally instead of accepting abstractions via constructor or function parameters.
- Prefer explicit parameters over implicit globals.

## Runtime Inputs

- Treat environment variables as boundary concerns.
- Prefer reading env/config at fixtures, CLI entrypoints, or top-level wiring, then pass values into reusable modules.
- Flag import-time environment reads in library modules.

## Hardcoded Values

- Flag magic literals in production logic (timeouts, retry counts, paths, model names, API URLs, etc.) when they should be configurable or named constants.
- Prefer named constants, defaults in signatures, or injected values over scattered literals.
- Allow local, scenario-specific literals in tests when they improve readability.
- Do not flag explicit, overrideable API defaults when they are intentional convenience defaults and remain configurable by callers.

## Complexity and Readability

- Flag deeply nested conditionals when they can be simplified.
- Flag long functions that mix multiple responsibilities.
- Prefer explicit names over terse variable names.
- Ask for docstrings on non-obvious logic and public APIs.

## ResourceRegistry (SQ105)

The `resource_registry` package is the single system responsible for tracking live infrastructure resources (Juju controllers, models, etc.) and ensuring logs are collected and the resources are destroyed even when tests fail mid-run.

### Core design

- **`ResourceRegistry`** (in `charm_integration_testing/resource_registry/registry.py`) is the central store. Resources are registered on creation and torn down in reverse registration order (`teardown_all()`). Children are torn down depth-first before their parent.
- **`ResourceHandle` protocol** identifies a resource: `resource_id` (unique string key), `resource_type` (human label), `path_segment` (filesystem-safe name used in log file names). Implementations must be frozen dataclasses. `path_segment` must sanitize unsafe path characters.
- **`LogCollector` protocol** collects logs for a resource. `supports(handle)` guards whether it applies. `collect(handle)` does the work and **should raise on failure** — the registry will catch, save, and re-raise after cleanup. Collectors receive their destination at construction time, not at call time.
- **`ResourceTeardownWarning`** is emitted by `teardown_all()` when a `teardown()` call raises, so that all resources are still processed. Within a single `teardown()`, log and destroy exceptions propagate directly so callers can observe them. If both fail, the destroy exception is raised chained from the log exception (`raise destroy_exc from log_exc`).

### Juju integration

- **`JujuControllerHandle`** (in `charm_integration_testing/juju/resource_registry/handles.py`) is the concrete `ResourceHandle` for Juju controllers.
- **`JujuCrashdumpCollector`** (in `charm_integration_testing/juju/resource_registry/collectors.py`) wraps `juju-crashdump` (machine) and `juju-k8s-crashdump` (Kubernetes). `output_dir` is a constructor argument; if `None`, collection is silently skipped. Output is a single `.tar.gz` file written flat into `output_dir` — no subdirectories. File name is `{handle.path_segment}.tar.gz`.
- **`JujuResourceRegistryExtension`** (in `charm_integration_testing/juju/resource_registry/extension.py`) is a `JujuExtension` that wires the registry into the `JujuClient` lifecycle: registers on `post_bootstrap_controller`, collects logs on `pre_kill_controller`, deregisters on `post_kill_controller`.

### Review rules

- The `ResourceRegistry` must not be called directly from test code or backends — only from extensions or session-scoped fixtures.
- `LogCollector.collect()` must not swallow exceptions. Failures should propagate so the registry can surface them correctly.
- `LogCollector` implementations must accept `output_dir` (and any other destination config) at construction time, not as arguments to `collect()`.
- `ResourceHandle` implementations must be frozen dataclasses; `path_segment` must produce a filesystem-safe string (no slashes, colons, spaces, or other shell-special characters).
- Log output files must be written flat into `output_dir` — no subdirectories per resource.
- Teardown must always run even when log collection fails. Flag any pattern that would skip the destroyer when a collector raises.
- `register()` enforces that a handle is not already registered and that a parent exists before its child is registered. Flag code that tries to work around these invariants.
- `deregister()` re-parents orphaned children to root so they remain reachable by `teardown_all()`. Flag any refactor that removes this re-parenting.
- Log messages from `ResourceRegistry` and `LogCollector` subclasses must start with a capital letter.
- Loggers in these classes must be created via `logger.getChild(type(self).__name__)` — never store the parent logger directly.

## Copyright Headers

Use one canonical copyright format across the entire repository, even in areas that still contain legacy headers.

- Preferred format:
  - `# Copyright <year> Canonical Ltd.`
  - `# See LICENSE file for licensing details.`
- Flag any file that uses a different copyright style.
- Flag any file missing a copyright header.

## PR Template

Every PR must use the repository's pull request template. When reviewing, verify the description covers:

- What issues are resolved (with issue references).
- How the changes were tested and what automated tests exist.
- Whether documentation and README are up to date.

Flag PRs where the description is empty or clearly hasn't followed the template structure.

## Consistency

- Prioritize consistency with neighboring modules over introducing a new style.
- Respect package boundaries; avoid introducing cross-package coupling unless required.
- Keep findings actionable with precise fixes.
