# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Run endpoint validators at a user-chosen level, for a charm's `validate` action.

This is a small, self-contained glue module: it runs all installed endpoint
validators (discovered via `validators-engine`, the same mechanism
`validators-runner` uses), for every non-peer relation defined on the charm,
at the level requested via the action's `level` param. It does not depend on
`validators-runner`, so charms using this module only need to install the
specific `validators-*` interface packages they actually use (plus
`validators-base` and `validators-engine`), instead of pulling in every
interface validator in this monorepo.
"""

import logging
from typing import get_args

from ops.charm import ActionEvent, CharmBase
from pydantic import BaseModel

from validators.base import ValidationLevel, ValidationResult
from validators.engine import run_for_charm

logger = logging.getLogger(__name__)


class ValidateActionResults(BaseModel):
    results: list[ValidationResult]


def run_validate_action(charm: CharmBase, event: ActionEvent) -> None:
    """Handle the `validate` action.

    Reads the `level` action param (defaults to "simple"), runs all
    installed validators for *charm* at that level, reports the full
    results as JSON via `event.set_results`, and fails the action if any
    result is `ERROR`.
    """
    level = event.params.get("level", "simple")
    if level not in get_args(ValidationLevel):
        event.fail(f"Invalid level '{level}'. Must be one of {get_args(ValidationLevel)}.")
        return

    results = ValidateActionResults(results=run_for_charm(charm, level=level))

    statuses = [result.status for result in results.results]
    event.set_results(
        {
            "results": results.model_dump_json(),
            "summary": (
                f"pass={statuses.count('PASS')} "
                f"fail={statuses.count('FAIL')} "
                f"error={statuses.count('ERROR')} "
                f"skipped={statuses.count('SKIPPED')}"
            ),
        }
    )

    errors = [r for r in results.results if r.status == "ERROR"]
    if errors:
        summary = "; ".join(f"{r.endpoint} ({r.interface}): {r.error}" for r in errors)
        event.fail(f"{len(errors)} validator(s) raised an error: {summary}")
