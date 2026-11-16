# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import sys
from pathlib import Path
from subprocess import CalledProcessError

import pytest
from test_suite import test_charmlint as module


@pytest.fixture
def target_charm_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    return source


@pytest.mark.parametrize("exit_code", [0, 1], ids=["passed", "failed"])
def test_charmlint_logs_output(
    exit_code: int,
    tmp_path: Path,
    target_charm_source: Path,
    request: pytest.FixtureRequest,
    logger: logging.Logger,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GIVEN a charmlint executable that writes to both output streams
    charmlint = tmp_path / "charmlint"
    charmlint.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"assert sys.argv[1:] == [{str(target_charm_source)!r}]\n"
        "print('Lint results', flush=True)\n"
        "print('Lint diagnostic', file=sys.stderr, flush=True)\n"
        f"sys.exit({exit_code})\n"
    )
    charmlint.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    # WHEN charmlint runs, retaining its failure status
    with caplog.at_level(logging.INFO):
        if exit_code:
            with pytest.raises(CalledProcessError) as exc_info:
                module.test_charmlint(request, logger)
            assert exc_info.value.returncode == exit_code
        else:
            module.test_charmlint(request, logger)

    # THEN both stdout and stderr are logged even when charmlint fails
    assert "Charmlint output:\nLint results\nLint diagnostic\n" in caplog.messages
