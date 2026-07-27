# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared idle-timeout selection for tests that wait on bundles to settle.

Extracted so any test that waits for a model to reach idle (not just ``test_deploy``) can extend
its timeout when a bundle involves a charm known to need materially longer to settle its
relations. See https://github.com/canonical/charm-integration-testing/issues/794.
"""

from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path

from juju.bundle_utils import parse_charm_names_from_bundle

# Default wait window for a bundle to reach idle.
DEFAULT_IDLE_TIMEOUT = timedelta(minutes=15)

# Extended wait window applied when a bundle contains any charm known to need materially
# longer to settle its relations (see https://github.com/canonical/charm-integration-testing/issues/794):
# postgresql-k8s/postgresql can take longer than DEFAULT_IDLE_TIMEOUT to publish the
# relation data that charms like pgbouncer-k8s block on, especially under CI resource
# contention, without either side being stuck.
EXTENDED_IDLE_TIMEOUT = timedelta(minutes=25)
SLOW_SETTLING_CHARMS = {"postgresql-k8s", "postgresql"}


def idle_timeout_for_bundle(bundle: Path) -> timedelta:
    charm_names = parse_charm_names_from_bundle(bundle.read_text(encoding="utf-8"))
    if charm_names & SLOW_SETTLING_CHARMS:
        return EXTENDED_IDLE_TIMEOUT
    return DEFAULT_IDLE_TIMEOUT


def idle_timeout_for_bundles(bundles: Iterable[Path | None]) -> timedelta:
    """Extended timeout if any of the given bundles (ignoring ``None``s) needs it."""
    for bundle in bundles:
        if bundle is not None and idle_timeout_for_bundle(bundle) == EXTENDED_IDLE_TIMEOUT:
            return EXTENDED_IDLE_TIMEOUT
    return DEFAULT_IDLE_TIMEOUT
