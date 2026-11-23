# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import shutil
from pathlib import Path
from subprocess import PIPE, STDOUT, run  # nosec B404

import pytest


def test_charmlint(request: pytest.FixtureRequest, logger: logging.Logger) -> None:
    charmlint = shutil.which("charmlint")
    if charmlint is None:
        pytest.skip("charmlint is not installed.")

    charm_source: Path | None = request.getfixturevalue("target_charm_source")
    if charm_source is None:
        pytest.skip("Git or the target charm could not be resolved for linting.")
    result = run(  # nosec B603
        [charmlint, str(charm_source)],
        stdout=PIPE,
        stderr=STDOUT,
        text=True,
        check=False,
    )
    logger.info("Charmlint output:\n%s", result.stdout)
    result.check_returncode()
