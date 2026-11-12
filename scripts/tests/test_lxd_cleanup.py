# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/cleanup-environment.yaml"

FAKE_LXC = """#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

if sys.argv[1:] == ["list", "charmqa:", "--format", "json"]:
    print(os.environ["TEST_INSTANCES"])
    sys.exit(int(os.environ["TEST_LIST_EXIT"]))

assert sys.argv[1:3] == ["delete", "--force"], sys.argv
assert len(sys.argv) == 4, sys.argv
name = sys.argv[3].removeprefix("charmqa:")

def log(event):
    with open(os.environ["TEST_LOG"], "a") as stream:
        stream.write(json.dumps([event, name]) + "\\n")

log("start")
if os.environ["TEST_BARRIER"] == "true":
    barrier = Path(os.environ["TEST_LOG"]).parent / "barrier"
    (barrier / name).touch()
    deadline = time.monotonic() + 10
    while len(list(barrier.iterdir())) < 4:
        if time.monotonic() > deadline:
            log("timeout")
            sys.exit(1)
        time.sleep(0.01)

log("end")
sys.exit(255 if name == os.environ["TEST_FAIL_NAME"] else 0)
"""


@dataclass
class CleanupRun:
    result: subprocess.CompletedProcess[str]
    events: list[list[str]]


@dataclass
class FakeLXD:
    binary: Path
    log: Path
    script: str

    def run(
        self,
        instances: list[dict[str, str]],
        *,
        dry_run: bool = False,
        fail_name: str = "",
        barrier: bool = False,
        list_exit: int = 0,
        list_output: str | None = None,
    ) -> CleanupRun:
        env = {
            **os.environ,
            "LXC_BINARY": str(self.binary),
            "AGE_THRESHOLD": "240",
            "DRY_RUN": str(dry_run).lower(),
            "TEST_INSTANCES": json.dumps(instances) if list_output is None else list_output,
            "TEST_LOG": str(self.log),
            "TEST_LIST_EXIT": str(list_exit),
            "TEST_FAIL_NAME": fail_name,
            "TEST_BARRIER": str(barrier).lower(),
        }
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", self.script],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        events = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return CleanupRun(result, events)


@pytest.fixture
def lxd(tmp_path: Path) -> FakeLXD:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["cleanup-environment"]["steps"]
        if step["name"] == "Find and delete instances older than age threshold"
    )
    binary = tmp_path / "lxc"
    binary.write_text(FAKE_LXC)
    binary.chmod(0o755)
    (tmp_path / "barrier").mkdir()
    return FakeLXD(binary, tmp_path / "events.jsonl", script)


def instance(name: str, *, age_minutes: int = 300) -> dict[str, str]:
    created = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return {"name": name, "created_at": created.isoformat().replace("+00:00", "Z")}


def test_cleanup_continues_after_failed_deletion(lxd: FakeLXD) -> None:
    # GIVEN an eligible instance whose deletion fails, followed by other instances.
    instances = [instance(f"juju-old-{index}") for index in range(9)]

    # WHEN cleanup runs, including a CLI exit code that xargs normally treats as fatal.
    run = lxd.run(instances, fail_name="juju-old-0")

    # THEN every candidate is attempted and the step still reports failure.
    assert run.result.returncode != 0
    assert sorted(name for event, name in run.events if event == "start") == sorted(i["name"] for i in instances)
    assert len([event for event, _ in run.events if event == "end"]) == len(instances)
    assert "::error::Failed deleting instance: juju-old-0" in run.result.stderr
    assert "::error::One or more LXD instances could not be deleted" in run.result.stderr


def test_cleanup_uses_bounded_parallelism_and_preserves_filters(lxd: FakeLXD) -> None:
    # GIVEN nine old Juju instances, a recent one and an old unrelated instance.
    instances = [instance(f"juju-old-{index}") for index in range(9)]
    instances += [instance("juju-recent", age_minutes=10), instance("unrelated")]

    # WHEN the first four deletions wait until they are all running.
    run = lxd.run(instances, barrier=True)

    # THEN cleanup completes with four workers and deletes only eligible instances.
    assert run.result.returncode == 0, run.result.stderr
    active = 0
    peak = 0
    for event, _ in run.events:
        assert event in {"start", "end"}
        active += 1 if event == "start" else -1
        peak = max(peak, active)
    assert active == 0
    assert peak == 4
    assert sorted(name for event, name in run.events if event == "start") == [f"juju-old-{index}" for index in range(9)]


def test_cleanup_dry_run_does_not_delete(lxd: FakeLXD) -> None:
    # GIVEN an old instance and a dry run.
    instances = [instance("juju-old"), instance("juju-recent", age_minutes=10)]

    # WHEN cleanup runs.
    run = lxd.run(instances, dry_run=True)

    # THEN it previews only eligible instances without invoking deletion.
    assert run.result.returncode == 0, run.result.stderr
    assert run.events == []
    assert "Dry run: would delete instance: juju-old" in run.result.stdout
    assert "juju-recent" not in run.result.stdout


def test_cleanup_without_candidates_does_not_delete(lxd: FakeLXD) -> None:
    # GIVEN no instances.
    # WHEN cleanup runs.
    run = lxd.run([])

    # THEN no deletion is attempted.
    assert run.result.returncode == 0, run.result.stderr
    assert run.events == []


@dataclass(frozen=True)
class ListFailure:
    exit_code: int = 0
    output: str | None = None


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(ListFailure(exit_code=1), id="list-fails-with-partial-output"),
        pytest.param(ListFailure(output="not json"), id="malformed-json"),
        pytest.param(
            ListFailure(
                output=json.dumps([instance("juju-old"), {"name": "juju-invalid", "created_at": "not a date"}])
            ),
            id="invalid-date-after-valid-candidate",
        ),
    ],
)
def test_cleanup_rejects_failed_or_invalid_listing(lxd: FakeLXD, failure: ListFailure) -> None:
    # GIVEN a listing that fails or cannot be fully parsed.
    # WHEN cleanup runs.
    run = lxd.run([instance("juju-old")], list_exit=failure.exit_code, list_output=failure.output)

    # THEN no partially collected candidates are deleted.
    assert run.result.returncode != 0
    assert run.events == []
    assert "::error::Failed to list eligible LXD instances" in run.result.stderr
