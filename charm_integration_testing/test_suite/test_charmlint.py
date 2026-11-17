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

    charm_source: Path = request.getfixturevalue("target_charm_source")
    result = run(  # nosec B603
        [charmlint, str(charm_source)],
        stdout=PIPE,
        stderr=STDOUT,
        text=True,
        check=False,
    )
    logger.info("Charmlint output:\n%s", result.stdout)
    result.check_returncode()
