# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import shutil
from pathlib import Path
from subprocess import run

import pytest


@pytest.mark.preflight
def test_charmlint(request: pytest.FixtureRequest) -> None:
    charmlint = shutil.which("charmlint")
    if charmlint is None:
        pytest.skip("charmlint is not installed.")

    charm_source: Path = request.getfixturevalue("target_charm_source")
    run([charmlint, str(charm_source)], check=True)
