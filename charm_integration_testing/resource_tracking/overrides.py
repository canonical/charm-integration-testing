# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Per-charm resource-tracking overrides read from the charm-overrides files.

Some charms legitimately leave resources behind that the tracker would otherwise
flag as drift -- for example ``postgresql-k8s`` retains its ``pgdata`` PVCs across
removals and scale events, so PVC drift is expected rather than a defect.  A
charm can opt out of tracking a resource *kind* by listing it under a top-level
``resource_tracking.skip`` key in its ``static/charm-overrides/<charm>.yaml``
file::

    resource_tracking:
      skip:
        - pvc

The key lives in the same file as the bundle-builder solver overrides but in a
separate top-level section; the solver ignores it and this loader ignores the
solver's ``overrides`` list, so the two concerns stay decoupled.  Skips are
keyed by *charm* here; callers resolve the deployed *application* to its charm
before applying them, because resources are attributed to an application on the
cluster.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

LOGGER = logging.getLogger(__name__)


def load_resource_tracking_skips(overrides_dir: Path) -> dict[str, frozenset[str]]:
    """Return a mapping of charm name to the resource types it opts out of.

    Reads every ``<charm>.yaml`` in ``overrides_dir`` and extracts the
    ``resource_tracking.skip`` list.  Charms without the section are omitted.
    Parsing is best-effort: a malformed or unreadable file is logged and skipped
    rather than failing the suite.
    """
    skips: dict[str, frozenset[str]] = {}
    if not overrides_dir.is_dir():
        return skips

    for charm_file in sorted(overrides_dir.glob("*.yaml")):
        try:
            document = yaml.safe_load(charm_file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            LOGGER.debug("Skipping resource-tracking overrides for '%s'.", charm_file.name, exc_info=True)
            continue

        section = document.get("resource_tracking") if isinstance(document, dict) else None
        if not isinstance(section, dict):
            continue

        skip = section.get("skip")
        if not isinstance(skip, list):
            continue

        resource_types = frozenset(str(item) for item in skip)
        if resource_types:
            skips[charm_file.stem] = resource_types

    return skips
