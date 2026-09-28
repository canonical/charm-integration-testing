Chaos Resource Constraints
===========================

This document explains the per-charm override mechanism for chaos test
parameters, distinct from the bundle-construction overrides described in
:doc:`/how-to/use-bundle-builder-x`.

Motivation
----------

Chaos experiments (CPU/memory stress, disk fill, I/O latency, network
isolation) need charm-specific parameters -- a charm with a small workload
may need only a modest amount of memory pressure to trigger interesting
behaviour, while another may need much more to have any effect at all. These
values are a test-execution concern, not something that belongs in the
bundle-building override files under ``static/charm-overrides/``, which is
already scoped to how bundles are assembled (endpoints, configs, resources,
constraints).

Components
----------

**Schema** (``chaos_client.resource_constraints.CharmResourceConstraints``)
  A ``CharmResourceConstraints`` block declares optional chaos parameters
  (e.g. ``stress_memory_size_mb``, ``stress_cpu_workers``,
  ``duration_seconds``) and the ``criteria`` under which it applies, reusing
  the same ``track``/``risk``/``ubuntu_version`` matching rules as
  ``bundle_builder_x.overrides.CharmOverridesCriteria``.

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
       stress_memory_workers: 2
       stress_memory_size_mb: 2048
       duration_seconds: 60
     - criteria:
         - track: "16"
       stress_memory_size_mb: 4096

See ``static/charm-resource-constraints/README.md`` for the full list of
supported fields.

Configuring the directory
--------------------------

The directory is passed via ``--charm-resource-constraints`` (default
``./static/charm-resource-constraints/``). Unlike ``--charm-overrides``, a
missing or unset directory is not an error -- it simply means no charm has
customized its chaos parameters yet.
