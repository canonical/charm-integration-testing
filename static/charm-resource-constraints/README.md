# Charm resource constraints

Per-charm chaos test parameters (e.g. how much memory or CPU a stress
experiment should apply, and for how long), read by
`ResourceConstraintsClient`
(`charm_integration_testing/chaos_client/resource_constraints.py`) and
applied automatically by `MetaChaosClient` before it dispatches a chaos
experiment to the underlying implementation.

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

## Supported fields

Every field is optional; unset fields fall back to the chaos test's built-in
defaults. Fields are grouped by scenario, not by `ChaosClient` method, since a
charm may need different values for the "moderate pressure" and "total
exhaustion" variant of the same underlying stress operation.

| Field                                       | Type | Backed by                  |
| -------------------------------------------- | ---- | ---------------------------- |
| `cpu_exhaustion_workers`                     | int  | `ChaosClient.stress_cpu`     |
| `cpu_exhaustion_duration_seconds`             | int  | `ChaosClient.stress_cpu`     |
| `cpu_moderate_pressure_workers`               | int  | `ChaosClient.stress_cpu`     |
| `cpu_moderate_pressure_duration_seconds`      | int  | `ChaosClient.stress_cpu`     |
| `memory_exhaustion_workers`                   | int  | `ChaosClient.stress_memory`  |
| `memory_exhaustion_size_mb`                   | int  | `ChaosClient.stress_memory`  |
| `memory_exhaustion_duration_seconds`          | int  | `ChaosClient.stress_memory`  |
| `memory_moderate_pressure_workers`            | int  | `ChaosClient.stress_memory`  |
| `memory_moderate_pressure_size_mb`            | int  | `ChaosClient.stress_memory`  |
| `memory_moderate_pressure_duration_seconds`   | int  | `ChaosClient.stress_memory`  |
| `disk_fill_size_mb`                           | int  | `ChaosClient.fill_disk`      |
| `disk_io_latency_delay_ms`                    | int  | `ChaosClient.io_latency`     |
| `disk_io_latency_percent`                     | int  | `ChaosClient.io_latency`     |
| `disk_io_latency_duration_seconds`            | int  | `ChaosClient.io_latency`     |

Disk I/O saturation and network isolation have no fields: no `ChaosClient`
implementation exists yet for I/O saturation, and `isolate_network` takes no
configurable parameters to override.

## How experiments consume these fields

`MetaChaosClient` resolves the deployed unit's charm, channel and Ubuntu base
from Juju, loads the first matching block from
`static/charm-resource-constraints/<charm>.yaml`, and merges any configured
fields over the values the test passed explicitly.

For `stress_cpu()` and `stress_memory()`, the keyword-only `scenario`
parameter selects which field set is checked:

- `scenario="exhaustion"` uses the `*_exhaustion_*` fields
- `scenario="moderate_pressure"` uses the `*_moderate_pressure_*` fields

`fill_disk()` and `io_latency()` have only one constraint block each, so they
do not take a `scenario` parameter.

```python
from datetime import timedelta

require_chaos_tool.stress_memory(
    model,
    unit,
    workers=1,
    size_mb=512,
    duration=timedelta(minutes=2),
    scenario="moderate_pressure",
)
```

If the file is absent, no block matches, or a field is left unset, the caller's
original argument is used unchanged.
