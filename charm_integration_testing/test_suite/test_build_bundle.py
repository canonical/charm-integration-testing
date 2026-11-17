# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path

import pytest

from bundle_builder_x import Solution

from .scheduler.states import State


@pytest.mark.state(requires=State.NO_BUNDLE, provides=State.NO_CONTROLLER)
def test_build_bundle(
    request: pytest.FixtureRequest,
    bundle_mermaid_output: Path,
    logger: logging.Logger,
    target_bundle: Path,
    target_controller: str,
    model: str,
    neighbor_bundle: Path | None,
    neighbor_controller: str | None,
    neighbor_model: str | None,
) -> None:
    solution: Solution = request.getfixturevalue("bundle_solution")

    separator = "-" * 80
    bundles_by_model = {b.model: b for b in solution.bundles}

    target_model_key = f"{target_controller}/{model}"
    target_contents = bundles_by_model[target_model_key].export()
    target_bundle.write_text(target_contents, encoding="utf-8")
    logger.info(f"Bundle written to {target_bundle}.")
    logger.info(f"Bundle content:\n{separator}\n{target_contents.strip()}\n{separator}")

    if neighbor_bundle is not None:
        assert neighbor_model is not None
        assert neighbor_controller is not None
        neighbor_model_key = f"{neighbor_controller}/{neighbor_model}"
        neighbor_contents = bundles_by_model[neighbor_model_key].export()
        neighbor_bundle.write_text(neighbor_contents, encoding="utf-8")
        logger.info(f"Bundle written to {neighbor_bundle}.")
        logger.info(f"Bundle content:\n{separator}\n{neighbor_contents.strip()}\n{separator}")

    bundle_mermaid_output.write_text(solution.export_mermaid(), encoding="utf-8")
    logger.info(f"Bundle Mermaid diagram written to {bundle_mermaid_output}")
