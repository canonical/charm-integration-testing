# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import sys
from pathlib import Path
from subprocess import CalledProcessError

import pytest
from test_suite import test_charmlint as module
from utils.git import GitClient

pytest_plugins = ["pytester"]


@pytest.fixture
def git_client(logger: logging.Logger) -> GitClient:
    return GitClient(logger)


@pytest.fixture
def target_charm_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    return source


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
@pytest.mark.parametrize("exit_code", [0, 1, 2], ids=["passed", "error", "warnings-or-runtime-error"])
def test_charmlint_logs_output(
    strict: bool,
    exit_code: int,
    tmp_path: Path,
    target_charm_source: Path,
    git_client: GitClient,
    request: pytest.FixtureRequest,
    logger: logging.Logger,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GIVEN a charmlint executable that writes to both output streams
    charmlint = tmp_path / "charmlint"
    expected_args = [str(target_charm_source), *(["--strict"] if strict else [])]
    charmlint.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"assert sys.argv[1:] == {expected_args!r}\n"
        "print('Lint results', flush=True)\n"
        "print('Lint diagnostic', file=sys.stderr, flush=True)\n"
        f"sys.exit({exit_code})\n"
    )
    charmlint.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    # WHEN charmlint runs, retaining its failure status
    test = module.test_charmlint_strict if strict else module.test_charmlint
    with caplog.at_level(logging.INFO):
        if exit_code:
            with pytest.raises(CalledProcessError) as exc_info:
                test(request, logger)
            assert exc_info.value.returncode == exit_code
        else:
            test(request, logger)

    # THEN both stdout and stderr are logged even when charmlint fails
    assert "Charmlint output:\nLint results\nLint diagnostic\n" in caplog.messages


def test_missing_git_skips_lint_without_skipping_other_tests(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    charmlint = pytester.path / "charmlint"
    charmlint.write_text(f"#!{sys.executable}\nraise AssertionError('must not run')\n")
    charmlint.chmod(0o755)
    monkeypatch.setenv("PATH", str(pytester.path))
    pytester.makeconftest(
        """
        import logging
        import pytest
        from test_suite.conftest import git_client, record_pipeline_version_execution_metadata

        @pytest.fixture(scope="session")
        def logger():
            return logging.getLogger("test")

        @pytest.fixture
        def execution_metadata():
            return lambda *args: None

        @pytest.fixture(autouse=True)
        def record_metadata(record_pipeline_version_execution_metadata):
            pass

        @pytest.fixture
        def target_charm_source():
            pytest.fail("Source must not be acquired without Git")
        """
    )
    pytester.makepyfile(
        """
        from test_suite.test_charmlint import test_charmlint

        def test_unrelated():
            pass
        """
    )

    result = pytester.runpytest("-rs")

    result.assert_outcomes(passed=1, skipped=1)
    result.stdout.fnmatch_lines(["*Git is not installed*"])
