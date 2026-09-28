# Charm resource constraints

Per-charm chaos test parameters (e.g. how much memory or CPU a stress
experiment should apply, and for how long), read by
`ResourceConstraintsClient`
(`charm_integration_testing/chaos_client/resource_constraints.py`).

This is a separate mechanism from `static/charm-overrides/`, which drives
bundle construction (endpoints, configs, resources, constraints). Resource
constraints only affect chaos test execution.

## File format

One file per charm, named `<charm-name>.yaml`, containing a list of
`constraints` blocks. Each block applies only when its `criteria` match the
deployed charm's channel and Ubuntu base (same `criteria` schema as
`charm-overrides`: `track`, `risk`, `ubuntu_version`, and `all_of`/`any_of`/
`none_of` combinators). A block with no `criteria` always matches. When
several blocks match, the first one listed is used. Fields left unset fall
back to the chaos test's built-in defaults.

```yaml
constraints:
  - criteria:
      - track: "14"
        ubuntu_version: "22.04"
    memory_exhaustion_workers: 2
    memory_exhaustion_size_mb: 2048
    memory_exhaustion_duration_seconds: 60
    memory_moderate_pressure_size_mb: 256
  - criteria:
      - track: "16"
    memory_exhaustion_size_mb: 4096
```
