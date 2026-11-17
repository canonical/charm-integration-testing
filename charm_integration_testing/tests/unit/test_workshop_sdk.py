# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SDK_SOURCE = Path(__file__).resolve().parents[3] / "development-sandbox" / "workshop"
PACK_TASK = SDK_SOURCE.parents[1] / "mise-tasks" / "workshop-pack"
REVISION = "a" * 40


def write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{content}\n")
    path.chmod(0o755)


@pytest.fixture
def environment(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    return {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "TEST_BIN": str(binaries),
    }


@pytest.fixture
def sdk(tmp_path: Path) -> Path:
    installed = tmp_path / "sdk"
    installed.mkdir()
    for directory in ("bin", "hooks"):
        shutil.copytree(SDK_SOURCE / directory, installed / directory)
    shutil.copy2(SDK_SOURCE / "environment.sh", installed / "environment.sh")
    (installed / "revision").write_text(f"{REVISION}\n")
    (installed / "toolkit" / ".agents" / "skills").mkdir(parents=True)
    for name in ("run-charm-tests", "setup-charm-tests"):
        skill = installed / "toolkit" / ".agents" / "skills" / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Test skill.\n---\n")
    (installed / "toolkit" / ".agents" / "skills" / "system.md").write_text("Sandbox-only context\n")
    (installed / "toolkit" / "mise.toml").write_text("[tools]\n")
    return installed


def test_workshop_setup_group_installs_only_host_packages(tmp_path: Path, environment: dict[str, str]) -> None:
    # GIVEN a package installer that rejects tool installation and provisioning
    calls = tmp_path / "package-arguments"
    environment["PACKAGE_ARGUMENTS"] = str(calls)
    write_executable(
        Path(environment["TEST_BIN"]) / "mise",
        """
case "$1" in
    config)
        [[ "$2" == get && "$3" == --file && -f "$4" ]]
        ;;
    bootstrap)
        [[ "$2" == packages && "$3" == apply ]]
        printf '%s\\n' "$@" > "$PACKAGE_ARGUMENTS"
        ;;
    *) echo "Unexpected Mise operation: $*" >&2; exit 1 ;;
esac
""",
    )
    setup = SDK_SOURCE.parents[1] / "mise-tasks" / "setup"

    # WHEN preparing Workshop on a host
    subprocess.run(["bash", str(setup), "--group", "workshop"], env=environment, check=True)

    # THEN only the required host snaps are requested
    assert calls.read_text().splitlines() == [
        "bootstrap",
        "packages",
        "apply",
        "--yes",
        "snap:lxd",
        "snap:workshop",
        "snap:sdkcraft",
    ]


@pytest.mark.parametrize("exit_code", [0, 23], ids=["success", "builder-failure"])
@pytest.mark.parametrize("operation", ["pack", "try"])
def test_pack_exports_only_the_selected_commit(
    tmp_path: Path, environment: dict[str, str], exit_code: int, operation: str
) -> None:
    # GIVEN a committed SDK alongside untracked and modified local files
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    shutil.copy2(SDK_SOURCE.parents[1] / ".gitignore", repository / ".gitignore")
    shutil.copytree(
        SDK_SOURCE, repository / "development-sandbox" / "workshop", ignore=shutil.ignore_patterns("__pycache__")
    )
    task = repository / "mise-tasks" / "workshop-pack"
    task.parent.mkdir()
    shutil.copy2(PACK_TASK, task)
    (repository / "mise.toml").write_text("committed catalogue\n")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=SDK test",
            "-c",
            "user.email=sdk-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "Fixture",
        ],
        check=True,
    )
    (repository / ".env").write_text("untracked-secret\n")
    (repository / "mise.toml").write_text("uncommitted catalogue\n")
    context_file = tmp_path / "context-path"
    environment["CONTEXT_FILE"] = str(context_file)
    write_executable(
        Path(environment["TEST_BIN"]) / "sdkcraft",
        """
[[ "$#" == 1 && "$1" == "$BUILD_OPERATION" ]]
[[ ! -e toolkit/.env && ! -e toolkit/.git ]]
[[ "$(cat toolkit/mise.toml)" == 'committed catalogue' ]]
[[ "$(cat revision)" == "$(git -C "$TEST_REPOSITORY" rev-parse HEAD)" ]]
[[ -x hooks/setup-project && -x bin/charm-qa && -f environment.sh ]]
printf '%s' "$PWD" > "$CONTEXT_FILE"
if [[ "$BUILD_EXIT_CODE" == 0 ]]; then
    printf 'artifact' > charm-qa_amd64_ubuntu@24.04.sdk
fi
exit "$BUILD_EXIT_CODE"
""",
    )
    environment["TEST_REPOSITORY"] = str(repository)
    environment["BUILD_EXIT_CODE"] = str(exit_code)
    environment["BUILD_OPERATION"] = operation

    # WHEN building through SDKcraft
    arguments = ["--try"] if operation == "try" else []
    result = subprocess.run(["bash", str(task), *arguments, "HEAD"], env=environment, capture_output=True, text=True)

    # THEN only committed source reached the builder and its temporary context was removed
    assert result.returncode == exit_code, result.stderr
    assert not Path(context_file.read_text()).exists()
    assert (repository / ".env").read_text() == "untracked-secret\n"
    artifact = repository / "dist" / "workshop" / "charm-qa_amd64_ubuntu@24.04.sdk"
    if exit_code == 0:
        assert artifact.read_text() == "artifact"
    else:
        assert not artifact.exists()


def test_setup_preserves_the_active_toolkit_on_failure(sdk: Path, environment: dict[str, str]) -> None:
    # GIVEN an active revision and a new SDK whose dependency installation fails
    state = Path(environment["HOME"]) / ".local" / "share" / "charm-qa"
    previous = state / "toolkits" / "previous"
    previous.mkdir(parents=True)
    (state / "current").symlink_to(previous)
    write_executable(Path(environment["TEST_BIN"]) / "mise", "exit 17")

    # WHEN preparing the new revision
    result = subprocess.run(["bash", str(sdk / "hooks" / "setup-project")], env={**environment, "SDK": str(sdk)})

    # THEN the failure propagates without replacing the active toolkit
    assert result.returncode == 17
    assert (state / "current").resolve() == previous


def test_setup_reuses_writable_source(sdk: Path, environment: dict[str, str]) -> None:
    # GIVEN an SDK and a successful, isolated dependency installer
    write_executable(
        Path(environment["TEST_BIN"]) / "mise",
        """
[[ "$*" == 'run setup -- --group development integration cloud-k8s' ]]
[[ "$PROJECT_ROOT" == "$PWD" && "$UV_PROJECT_ENVIRONMENT" == "$PWD/.venv" ]]
[[ "$MISE_DATA_DIR" == "$HOME/.local/share/charm-qa/mise" ]]
""",
    )
    hook = ["bash", str(sdk / "hooks" / "setup-project")]
    hook_environment = {**environment, "SDK": str(sdk)}
    subprocess.run(hook, env=hook_environment, check=True)
    toolkit = Path(environment["HOME"]) / ".local" / "share" / "charm-qa" / "current"
    (toolkit / "mise.toml").write_text("local edits\n")

    # WHEN setup runs again for the same source revision
    subprocess.run(hook, env=hook_environment, check=True)

    # THEN editable source is retained without modifying the packaged SDK
    assert (toolkit / "mise.toml").read_text() == "local edits\n"
    assert (sdk / "toolkit" / "mise.toml").read_text() == "[tools]\n"
    assert toolkit.resolve().name == REVISION


def test_setup_refreshes_only_owned_skill_links(sdk: Path, environment: dict[str, str]) -> None:
    # GIVEN registered skills alongside unrelated user content
    write_executable(Path(environment["TEST_BIN"]) / "mise", "exit 0")
    home = Path(environment["HOME"])
    directories = [home / ".agents" / "skills", home / ".claude" / "skills"]
    hook = ["bash", str(sdk / "hooks" / "setup-project")]
    hook_environment = {**environment, "SDK": str(sdk)}
    subprocess.run(hook, env=hook_environment, check=True)
    for directory in directories:
        (directory / "user-skill").mkdir()
        (directory / "user-skill" / "SKILL.md").write_text("User content\n")
        (directory / "user-link").symlink_to(home / "missing-user-skill")
        assert not (directory / "system.md").exists()
        assert (directory / "setup-charm-tests" / "SKILL.md").is_file()
    shutil.rmtree(sdk / "toolkit" / ".agents" / "skills" / "setup-charm-tests")
    updated_skill = sdk / "toolkit" / ".agents" / "skills" / "run-charm-tests" / "SKILL.md"
    updated_skill.write_text("Updated skill\n")
    (sdk / "revision").write_text(f"{'b' * 40}\n")

    # WHEN a new SDK revision removes a skill and updates another
    subprocess.run(hook, env=hook_environment, check=True)
    subprocess.run(hook, env=hook_environment, check=True)

    # THEN owned links follow the new toolkit, while only removed owned links disappear
    for directory in directories:
        assert (directory / "run-charm-tests" / "SKILL.md").read_text() == "Updated skill\n"
        assert not (directory / "setup-charm-tests").is_symlink()
        assert (directory / "user-skill" / "SKILL.md").read_text() == "User content\n"
        assert (directory / "user-link").is_symlink()


@pytest.mark.parametrize("client", [".agents", ".claude"])
@pytest.mark.parametrize("conflict", ["directory", "file", "symlink"])
def test_setup_rejects_skill_conflicts_before_switching_toolkit(
    sdk: Path, environment: dict[str, str], client: str, conflict: str
) -> None:
    # GIVEN an existing user skill that Charm QA does not own
    home = Path(environment["HOME"])
    previous = home / ".local" / "share" / "charm-qa" / "toolkits" / "previous"
    previous.mkdir(parents=True)
    current = previous.parent.parent / "current"
    current.symlink_to(previous)
    directory = home / client / "skills"
    directory.mkdir(parents=True)
    target = directory / "run-charm-tests"
    if conflict == "directory":
        target.mkdir()
        (target / "SKILL.md").write_text("User skill\n")
    elif conflict == "file":
        target.write_text("User file\n")
    else:
        target.symlink_to(home / "missing-user-skill")
    write_executable(Path(environment["TEST_BIN"]) / "mise", "exit 0")

    # WHEN setup encounters the name conflict
    result = subprocess.run(
        ["bash", str(sdk / "hooks" / "setup-project")],
        env={**environment, "SDK": str(sdk)},
        capture_output=True,
        text=True,
    )

    # THEN it reports the path without changing user content, the toolkit, or other registrations
    assert result.returncode == 1
    assert f"conflicts with {target}" in result.stderr
    assert current.resolve() == previous
    assert not (home / ".agents" / "skills" / "setup-charm-tests").exists()
    if conflict == "directory":
        assert (target / "SKILL.md").read_text() == "User skill\n"
    elif conflict == "file":
        assert target.read_text() == "User file\n"
    else:
        assert target.readlink() == home / "missing-user-skill"


def test_tasks_lists_toolkit_tasks(sdk: Path, environment: dict[str, str]) -> None:
    # GIVEN a prepared toolkit and a Mise task-listing command
    state = Path(environment["HOME"]) / ".local" / "share" / "charm-qa"
    state.mkdir(parents=True)
    (state / "current").symlink_to(sdk / "toolkit")
    write_executable(
        Path(environment["TEST_BIN"]) / "mise",
        """
[[ "$1" == -C && "$2" == "$PROJECT_ROOT" && "$3" == tasks && "$4" == ls && "$5" == --name-only ]]
""",
    )

    # WHEN listing tasks with a Mise option
    subprocess.run([str(sdk / "bin" / "charm-qa"), "tasks", "--name-only"], env=environment, check=True)


def test_exec_preserves_workspace_and_uses_toolkit_environment(
    sdk: Path, tmp_path: Path, environment: dict[str, str]
) -> None:
    # GIVEN a prepared toolkit separate from the consumer's checkout
    state = Path(environment["HOME"]) / ".local" / "share" / "charm-qa"
    state.mkdir(parents=True)
    (state / "current").symlink_to(sdk / "toolkit")
    workspace = tmp_path / "consumer project"
    workspace.mkdir()
    write_executable(
        Path(environment["TEST_BIN"]) / "mise",
        """
[[ "$1" == -C && "$2" == "$PROJECT_ROOT" && "$3" == exec && "$4" == -- ]]
cd "$2"
shift 4
exec "$@"
""",
    )
    write_executable(
        Path(environment["TEST_BIN"]) / "uv",
        """
[[ "$1" == run && "$2" == --project && "$3" == "$PROJECT_ROOT" ]]
[[ "$4" == --no-sync && "$5" == --all-packages && "$6" == -- ]]
[[ "$UV_PROJECT_ENVIRONMENT" == "$PROJECT_ROOT/.venv" ]]
shift 6
exec "$@"
""",
    )
    command = [
        str(sdk / "bin" / "charm-qa"),
        "exec",
        "--",
        "python3",
        "-c",
        "import json, os, sys; print(json.dumps([os.getcwd(), os.environ['PROJECT_ROOT'], "
        "os.environ['CHARM_QA_WORKSPACE'], sys.argv[1:]]))",
        "argument with spaces",
        "--flag",
    ]

    # WHEN executing a command from a consumer checkout
    result = subprocess.run(command, cwd=workspace, env=environment, capture_output=True, text=True, check=True)

    # THEN paths and argument boundaries survive without using the consumer's environment
    assert json.loads(result.stdout) == [
        str(workspace),
        str(sdk / "toolkit"),
        str(workspace),
        ["argument with spaces", "--flag"],
    ]
