# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Run the "simple"-level integration check used on every update-status hook.

This is a small, self-contained glue module: it runs all installed endpoint
validators (discovered via `validators-engine`, the same mechanism
`validators-runner` uses) at the "simple" level for every non-peer relation
defined on the charm. It does not depend on `validators-runner`, so charms
using this module only need to install the specific `validators-*` interface
packages they actually use (plus `validators-base` and `validators-engine`),
instead of pulling in every interface validator in this monorepo.
"""

import logging

from ops import BlockedStatus, Object, StoredState
from ops.charm import CharmBase
from pydantic import BaseModel

from validators.base import ValidationResult
from validators.engine import run_for_charm

logger = logging.getLogger(__name__)


class UpdateStatusCheckResults(BaseModel):
    results: list[ValidationResult]


class ValidationStatusStore(Object):
    """Persist the outcome of the periodic integration check across hooks.

    Charms would otherwise each need their own `StoredState` fields plus the
    logic to turn FAIL/ERROR results into a status message and clear it once
    the check passes again. Instantiate this once (e.g. in `__init__`), then
    either pass this store to `run_simple_check(charm, store)` to have it
    recorded automatically, or call `record()` yourself with the `.results`
    of `run_simple_check(charm)`, and read `status()` from a `collect-status`
    handler.
    """

    _stored = StoredState()  # type: ignore[no-untyped-call]

    def __init__(self, charm: CharmBase, key: str = "validators-update-status-check") -> None:
        super().__init__(charm, key)
        self._stored.set_default(kind=None, message="")

    def record(self, results: list[ValidationResult]) -> None:
        """Record the outcome of *results*.

        Sets a Blocked status if any result is FAIL/ERROR. Otherwise, clears
        any prior failure only once every result is a genuine PASS: a
        SKIPPED result (e.g. a relation that hasn't negotiated data yet)
        leaves a previously recorded failure in place, since it means the
        check didn't actually re-run to confirm the problem is resolved.
        """
        failing = [r for r in results if r.status in ("FAIL", "ERROR")]
        if failing:
            summary = "; ".join(f"{r.endpoint} ({r.interface}): {r.status}" for r in failing)
            self._stored.kind = "blocked"
            self._stored.message = f"Integration check failed: {summary}"
        elif all(r.status == "PASS" for r in results):
            self.clear()

    def clear(self) -> None:
        """Clear any previously recorded failure."""
        self._stored.kind = None
        self._stored.message = ""

    def status(self) -> BlockedStatus | None:
        """Return the stored status, or None if the last recorded check passed."""
        if self._stored.kind == "blocked":  # type: ignore[comparison-overlap]
            return BlockedStatus(str(self._stored.message))
        return None


def run_simple_check(charm: CharmBase, store: ValidationStatusStore | None = None) -> UpdateStatusCheckResults:
    """Run all installed validators for *charm* at the "simple" level.

    Logs an error for every FAIL/ERROR result. Returns the full results so
    the caller can build a unit status (e.g. from a collect-status handler)
    without needing to re-run the validators. If *store* is given, also
    records the outcome in it (see `ValidationStatusStore`).
    """
    results = run_for_charm(charm, level="simple", skip_missing_unvalidated=True)

    for result in results:
        if result.status in ("FAIL", "ERROR"):
            failed_checks = "; ".join(c.message for c in result.checks if not c.passed)
            logger.error(
                "Integration check %s for endpoint '%s' (interface '%s', level '%s'): %s",
                result.status,
                result.endpoint,
                result.interface,
                result.level,
                result.error or failed_checks or "no details",
            )
    if store is not None:
        store.record(results)
    return UpdateStatusCheckResults(results=results)
