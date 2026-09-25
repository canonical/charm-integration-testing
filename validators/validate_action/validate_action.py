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

import json
from typing import get_args

from ops import Object
from ops.charm import ActionEvent, CharmBase
from pydantic import BaseModel

from validators.base import ValidationLevel, ValidationResult
from validators.engine import run_for_charm


class ValidateActionResults(BaseModel):
    results: list[ValidationResult]


class _ValidateActionObserver(Object):
    """Binds the `validate` action to `run_validate_action`.

    `ops.Framework.observe` requires a bound method (not e.g. a lambda or
    `functools.partial`) as its observer, so `observe_validate_action` needs
    a real `Object` to own the callback.
    """

    def __init__(self, charm: CharmBase) -> None:
        super().__init__(charm, "validators-validate-action")
        self._charm = charm

    def _on_validate_action(self, event: ActionEvent) -> None:
        run_validate_action(self._charm, event)


def observe_validate_action(charm: CharmBase) -> None:
    """Wire the `validate` action to `run_validate_action`.

    This charm must declare a `validate` action in its charmcraft.yaml, e.g.::

        actions:
          validate:
            description: Runs integration validators against this charm's relations.
            params:
              level:
                description: "The depth of validation to run"
                type: string
                enum: [simple, deep, uat]
                default: simple

    Juju generates `charm.on.validate_action` from that declaration; if it is
    missing, this raises immediately with a clear message rather than letting
    every hook fail later with an opaque `AttributeError`/`NoSuchEventError` the
    first time something touches `charm.on.validate_action`.
    """
    try:
        event_source = charm.on.validate_action
    except AttributeError as exc:
        raise RuntimeError(
            "No `validate` action is declared for this charm. Add a `validate` action "
            "to charmcraft.yaml (see observe_validate_action's docstring for the "
            "expected schema) before calling observe_validate_action()."
        ) from exc
    observer = _ValidateActionObserver(charm)
    # ops.Framework only holds weak references to registered Objects, so the
    # observer must be kept alive elsewhere (i.e. an attribute on the charm)
    # or it is garbage-collected before the action fires.
    charm._validators_validate_action_observer = observer  # type: ignore[attr-defined]
    charm.framework.observe(event_source, observer._on_validate_action)


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

    action_results = ValidateActionResults(results=run_for_charm(charm, level=level, skip_missing_unvalidated=True))

    statuses = [result.status for result in action_results.results]
    event.set_results(
        {
            # Juju action results are ultimately flat str:str pairs (ops only recurses into
            # dict values; anything else, e.g. a list, is stringified with str(), which would
            # produce invalid JSON - Python reprs, not `true`/`false`/`null`). So this has to
            # stay a JSON string. It's dumped as a plain list (rather than
            # `action_results.model_dump_json()`, which would nest it under another "results"
            # key) so callers get `[...]` directly instead of `{"results": [...]}`.
            "results": json.dumps([r.model_dump(mode="json") for r in action_results.results]),
            "summary": (
                f"pass={statuses.count('PASS')} "
                f"fail={statuses.count('FAIL')} "
                f"error={statuses.count('ERROR')} "
                f"skipped={statuses.count('SKIPPED')}"
            ),
        }
    )

    errors = [r for r in action_results.results if r.status == "ERROR"]
    if errors:
        summary = "; ".join(f"{r.endpoint} ({r.interface}): {r.error}" for r in errors)
        event.fail(f"{len(errors)} validator(s) raised an error: {summary}")
