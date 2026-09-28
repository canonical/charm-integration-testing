Chaos Resource Constraints
===========================

This document explains the per-charm override mechanism for chaos test
parameters, distinct from the bundle-construction overrides described in
:doc:`/how-to/use-bundle-builder-x`.

Motivation
----------

The chaos test matrix targets eight scenarios: CPU total exhaustion, CPU
moderate pressure, memory total exhaustion, memory moderate pressure, disk
fill, disk I/O latency, disk I/O saturation, and network isolation. Each
needs charm-specific parameters -- a charm with a small workload may need
only a modest amount of memory pressure to trigger interesting behaviour,
while another may need much more to have any effect at all, and the
"moderate pressure" and "total exhaustion" variants of the same experiment
typically need different values for the same charm. These values are a
test-execution concern, not something that belongs in the bundle-building
override files under ``static/charm-overrides/``, which is already scoped to
how bundles are assembled (endpoints, configs, resources, constraints).

Components
----------

**Schema** (``chaos_client.resource_constraints.CharmResourceConstraints``)
  A ``CharmResourceConstraints`` block declares optional chaos parameters,
  grouped by scenario rather than by ``ChaosClient`` method (e.g.
  ``memory_exhaustion_size_mb`` and ``memory_moderate_pressure_size_mb`` are
  independent fields, both backed by ``ChaosClient.stress_memory``), plus the
  ``criteria`` under which the block applies, reusing the same
  ``track``/``risk``/``ubuntu_version`` matching rules as
  ``bundle_builder_x.overrides.CharmOverridesCriteria``. Only scenarios
  backed by an implemented ``ChaosClient`` operation have fields; disk I/O
  saturation has none yet (unimplemented), and network isolation has none
  because ``isolate_network`` takes no tunable parameters.

**Client** (``chaos_client.resource_constraints.ResourceConstraintsClient``)
  Reads ``static/charm-resource-constraints/<charm-name>.yaml`` files, picks
  the first matching block for a charm's ``(channel, ubuntu_version)``, and
  returns all-defaults (every field ``None``) when no file, no block, or no
  directory is configured -- so it is always safe to call, even for charms
  with no override on file.

File format
-----------

.. code-block:: yaml

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

See ``static/charm-resource-constraints/README.md`` for the full list of
supported scenarios and fields.

Configuring the directory
--------------------------

The directory is passed via ``--charm-resource-constraints`` (default
``./static/charm-resource-constraints/``). Unlike ``--charm-overrides``, a
missing or unset directory is not an error -- it simply means no charm has
customized its chaos parameters yet.
