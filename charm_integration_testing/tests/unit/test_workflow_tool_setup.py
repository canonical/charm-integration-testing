# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml


@dataclass(frozen=True)
class Params:
    channel: str
    expected_channel: str | None
    succeeds: bool = True
    install_fails: bool = False


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(Params("", None), id="shared-default"),
        pytest.param(Params("3/candidate", "3/candidate"), id="explicit-channel"),
        pytest.param(Params("4/edge/test-branch", "4/edge/test-branch"), id="branch-channel"),
        pytest.param(Params('3/stable"\nstate = "absent"', None, False), id="invalid-channel"),
        pytest.param(Params("3/stabel", None, False), id="invalid-risk"),
        pytest.param(Params("3/candidate", "3/candidate", False, True), id="install-failure"),
    ],
)
def test_workflow_juju_channel_override(params: Params, tmp_path: Path) -> None:
    # GIVEN an isolated command boundary, without access to the host's snap manager.
    repository = Path(__file__).resolve().parents[3]
    workflow = yaml.safe_load((repository / ".github/workflows/charm-testing.yaml").read_text())
    step = next(step for step in workflow["jobs"]["execute-charm-test"]["steps"] if step["name"] == "Install Juju")
    log = tmp_path / "commands"
    mise = tmp_path / "mise"
    mise.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$COMMAND_LOG"\n'
        'if [[ "$1" == trust ]]; then cat "$2" >> "$COMMAND_LOG"; fi\n'
        'if [[ "$*" == *"packages apply"* && "$INSTALL_FAILS" == true ]]; then exit 42; fi\n'
    )
    mise.chmod(0o755)
    snap = tmp_path / "snap"
    snap.write_text('#!/bin/bash\nprintf "snap %s\\n" "$*" >> "$COMMAND_LOG"\n')
    snap.chmod(0o755)

    # WHEN the workflow shell runs with the selected input.
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "TMPDIR": str(tmp_path),
            "COMMAND_LOG": str(log),
            "JUJU_CHANNEL": params.channel,
            "INSTALL_FAILS": str(params.install_fails).lower(),
        },
        capture_output=True,
        text=True,
    )

    # THEN only valid selections reach installation, and temporary overrides are removed.
    assert (result.returncode == 0) == params.succeeds, result.stderr
    commands = log.read_text() if log.exists() else ""
    if params.expected_channel is not None:
        assert f'"snap:juju" = {{ channel = "{params.expected_channel}" }}' in commands
        assert "bootstrap packages apply --yes snap:juju" in commands
    elif params.succeeds:
        assert commands == "bootstrap packages apply --yes snap:juju\nsnap list juju\n"
    else:
        assert commands == ""
        assert "Invalid Juju channel" in result.stdout
    assert not list(tmp_path.glob("tmp.*"))
    assert ("snap list juju" in commands) == params.succeeds
