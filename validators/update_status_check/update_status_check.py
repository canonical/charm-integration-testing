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

from ops.charm import CharmBase
from pydantic import BaseModel

from validators.base import ValidationResult
from validators.engine import run_for_charm

logger = logging.getLogger(__name__)


class UpdateStatusCheckResults(BaseModel):
    results: list[ValidationResult]


def run_simple_check(charm: CharmBase) -> UpdateStatusCheckResults:
    """Run all installed validators for *charm* at the "simple" level.

    Logs an error for every FAIL/ERROR result. Returns the full results so
    the caller can build a unit status (e.g. from a collect-status handler)
    without needing to re-run the validators.
    """
    results = run_for_charm(charm, level="simple")

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
    return UpdateStatusCheckResults(results=results)
