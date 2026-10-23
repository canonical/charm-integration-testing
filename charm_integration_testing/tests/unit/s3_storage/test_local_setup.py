# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[4]
SETUP = ROOT / "development-sandbox/bin/setup-s3.sh"


@pytest.mark.parametrize(
    ("statuses", "expected_attempts", "success"),
    [
        pytest.param((503, 503, 200), 3, True, id="wait-for-success"),
        pytest.param((503, 403), 2, True, id="wait-for-auth-required"),
        pytest.param((503,), 30, False, id="persistent-server-error"),
        pytest.param((404,), 30, False, id="wrong-service"),
    ],
)
def test_setup_waits_for_a_healthy_s3_response(
    tmp_path: Path, statuses: tuple[int, ...], expected_attempts: int, success: bool
) -> None:
    # GIVEN an HTTP endpoint that is reachable before it is ready
    responses: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status = statuses[min(len(responses), len(statuses) - 1)]
            responses.append(status)
            self.send_response(status)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    sleep = tmp_path / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o700)
    readiness = "ready=0\n" + SETUP.read_text().split("ready=0\n", 1)[1].split('cd "${project_root}"', 1)[0]
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            # WHEN the actual shell readiness loop uses real curl responses
            result = subprocess.run(
                ["bash", "-eu", "-c", readiness],
                capture_output=True,
                check=False,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{tmp_path}:{os.environ['PATH']}",
                    "S3_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
                },
            )
        finally:
            server.shutdown()
            thread.join()

    # THEN server errors are retried, and only success/auth-required responses proceed
    assert (result.returncode == 0) is success
    assert len(responses) == expected_attempts
    if not success:
        assert "credentials were not written" in result.stderr


def test_setup_restricts_existing_credentials_before_reading_them(tmp_path: Path) -> None:
    # GIVEN pre-existing overly permissive credential files and an invalid binary
    state = tmp_path / ".local/share/charmqa-s3"
    (state / "bin").mkdir(parents=True)
    (state / "bin/weed").touch()
    credentials = [state / name for name in ("s3.json", "s3.conf", "s3.env")]
    for path in credentials:
        path.write_text("unchanged")
        path.chmod(0o644)
    state.chmod(0o755)

    # WHEN setup inspects the existing installation
    result = subprocess.run(
        ["bash", str(SETUP)], capture_output=True, text=True, check=False, env={**os.environ, "HOME": str(tmp_path)}
    )

    # THEN permissions are restricted before even the binary-validation failure
    assert result.returncode != 0
    assert "binary checksum mismatch" in result.stderr
    assert state.stat().st_mode & 0o777 == 0o700
    for path in credentials:
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.read_text() == "unchanged"


def test_setup_restricts_state_before_creating_children(tmp_path: Path) -> None:
    # GIVEN a permissive state directory with a path that prevents child creation
    state = tmp_path / ".local/share/charmqa-s3"
    state.mkdir(parents=True)
    state.chmod(0o755)
    (state / "data").write_text("unchanged")

    # WHEN mkdir fails, THEN the containing directory has already been restricted
    result = subprocess.run(
        ["bash", str(SETUP)], capture_output=True, text=True, check=False, env={**os.environ, "HOME": str(tmp_path)}
    )
    assert result.returncode != 0
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "data").read_text() == "unchanged"


@pytest.mark.parametrize("name", ["bin", "data"])
def test_setup_rejects_symlinked_state_subdirectories(tmp_path: Path, name: str) -> None:
    # GIVEN a service-data or executable directory redirected outside owned state
    state = tmp_path / ".local/share/charmqa-s3"
    state.mkdir(parents=True)
    target = tmp_path / "unrelated"
    target.mkdir()
    target.chmod(0o755)
    (state / name).symlink_to(target, target_is_directory=True)

    # WHEN setup starts, THEN it rejects the path before downloading or starting a service
    result = subprocess.run(
        ["bash", str(SETUP)], capture_output=True, text=True, check=False, env={**os.environ, "HOME": str(tmp_path)}
    )
    assert result.returncode != 0
    assert "must be an owned directory, not a symlink" in result.stderr
    assert target.stat().st_mode & 0o777 == 0o755
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("name", ["s3.json", "s3.conf", "s3.env"])
def test_setup_rejects_symlinked_credentials(tmp_path: Path, name: str) -> None:
    # GIVEN a credential path that points outside the S3 state directory
    state = tmp_path / ".local/share/charmqa-s3"
    state.mkdir(parents=True)
    target = tmp_path / "unrelated"
    target.write_text("unchanged")
    target.chmod(0o644)
    (state / name).symlink_to(target)
    # WHEN setup starts, THEN it refuses rather than reading or chmod-ing the target
    result = subprocess.run(
        ["bash", str(SETUP)], capture_output=True, text=True, check=False, env={**os.environ, "HOME": str(tmp_path)}
    )
    assert result.returncode != 0
    assert "must be an owned regular file, not a symlink" in result.stderr
    assert target.stat().st_mode & 0o777 == 0o644
    assert target.read_text() == "unchanged"


@pytest.mark.parametrize("optimized", [False, True], ids=["normal", "optimized"])
def test_setup_rejects_mismatched_canary_with_optimization(tmp_path: Path, optimized: bool) -> None:
    # GIVEN an S3 transport that returns the wrong canary content
    (tmp_path / "boto3.py").write_text(
        "from io import BytesIO\n"
        "from botocore.exceptions import ClientError\n"
        "class Client:\n"
        "    def create_bucket(self, **kwargs): pass\n"
        "    def put_object(self, **kwargs): pass\n"
        "    def get_object(self, **kwargs): return {'Body': BytesIO(b'wrong-content')}\n"
        "    def delete_object(self, **kwargs): pass\n"
        "    def delete_bucket(self, **kwargs): pass\n"
        "    def put_bucket_tagging(self, **kwargs): pass\n"
        "    def get_bucket_tagging(self, **kwargs): return {}\n"
        "    def list_objects_v2(self, **kwargs): return {}\n"
        "    def close(self): pass\n"
        "class Restricted(Client):\n"
        "    def create_bucket(self, **kwargs):\n"
        "        raise ClientError({'Error': {'Code': 'AccessDenied'}}, 'CreateBucket')\n"
        "    def __getattribute__(self, name):\n"
        "        method = super().__getattribute__(name)\n"
        "        if name == 'close' or name.startswith('_'): return method\n"
        "        def call(**kwargs):\n"
        "            if kwargs.get('Bucket', '').startswith('charmqa-outside-'):\n"
        "                raise ClientError({'Error': {'Code': 'AccessDenied'}}, name)\n"
        "            return method(**kwargs)\n"
        "        return call\n"
        "def client(*args, **kwargs):\n"
        "    return Restricted() if kwargs['aws_access_key_id'] == 'test-access' else Client()\n"
    )
    probe = SETUP.read_text().split("poetry run python - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
    command = [sys.executable, *(["-O"] if optimized else []), "-c", probe]

    # WHEN the actual setup probe runs, including under python -O
    result = subprocess.run(
        command,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "S3_ENDPOINT": "http://s3.test",
            "S3_ACCESS_KEY": "test-access",
            "S3_SECRET_KEY": "test-secret",
            "S3_CLEANUP_ACCESS_KEY": "cleanup-access",
            "S3_CLEANUP_SECRET_KEY": "cleanup-secret",
        },
    )
    # THEN correctness checks cannot disappear with Python assertions
    assert result.returncode != 0
    assert "Local S3 canary content mismatch" in result.stderr
    assert "Local S3 read/write passed" not in result.stdout


@pytest.mark.parametrize("cleanup_failure", ["none", "delete", "application-close", "cleanup-close"])
def test_setup_cleans_outside_bucket_when_creation_response_is_lost(tmp_path: Path, cleanup_failure: str) -> None:
    # GIVEN an overly permissive identity whose outside-bucket creation succeeds before raising
    (tmp_path / "boto3.py").write_text(
        "from io import BytesIO\n"
        "from pathlib import Path\n"
        f"cleanup_failure = {cleanup_failure!r}\n"
        "class Client:\n"
        "    def __init__(self, application): self.application = application\n"
        "    def create_bucket(self, Bucket):\n"
        "        if self.application and Bucket.startswith('charmqa-outside-'):\n"
        "            Path('created').write_text(Bucket)\n"
        "            raise RuntimeError('creation response lost')\n"
        "    def put_object(self, **kwargs): pass\n"
        "    def get_object(self, **kwargs): return {'Body': BytesIO(b'local-s3')}\n"
        "    def delete_object(self, **kwargs): pass\n"
        "    def delete_bucket(self, Bucket):\n"
        "        with Path('deleted').open('a') as output: output.write(Bucket + '\\n')\n"
        "        if cleanup_failure == 'delete' and not Bucket.startswith('charmqa-outside-'):\n"
        "            raise RuntimeError('probe bucket cleanup failed')\n"
        "    def close(self):\n"
        "        name = 'application-close' if self.application else 'cleanup-close'\n"
        "        with Path('closed').open('a') as output: output.write(name + '\\n')\n"
        "        if cleanup_failure == name: raise RuntimeError(name + ' failed')\n"
        "def client(*args, **kwargs):\n"
        "    return Client(kwargs['aws_access_key_id'] == 'test-access')\n"
    )
    probe = SETUP.read_text().split("poetry run python - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
    # WHEN the actual setup probe encounters the post-mutation failure
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "S3_ENDPOINT": "http://s3.test",
            "S3_ACCESS_KEY": "test-access",
            "S3_SECRET_KEY": "test-secret",
            "S3_CLEANUP_ACCESS_KEY": "cleanup-access",
            "S3_CLEANUP_SECRET_KEY": "cleanup-secret",
        },
    )
    # THEN both attempted buckets are cleaned and the original failure remains visible
    assert result.returncode != 0
    assert result.stderr.rstrip().endswith("RuntimeError: creation response lost")
    if cleanup_failure != "none":
        assert "Local S3 probe cleanup failed:" in result.stderr
    deleted = (tmp_path / "deleted").read_text().splitlines()
    assert len(deleted) == 2
    assert (tmp_path / "created").read_text() in deleted
    assert (tmp_path / "closed").read_text().splitlines() == ["application-close", "cleanup-close"]


def test_setup_closes_application_client_when_management_client_initialization_fails(tmp_path: Path) -> None:
    # GIVEN an application client followed by failing management-client initialization
    (tmp_path / "boto3.py").write_text(
        "from pathlib import Path\n"
        "class Client:\n"
        "    def close(self): Path('closed').touch()\n"
        "def client(*args, **kwargs):\n"
        "    if kwargs['aws_access_key_id'] == 'cleanup-access':\n"
        "        raise RuntimeError('management client initialization failed')\n"
        "    return Client()\n"
    )
    probe = SETUP.read_text().split("poetry run python - <<'PY'\n", 1)[1].split("\nPY", 1)[0]

    # WHEN the actual setup probe starts, THEN the created client is still closed
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "S3_ENDPOINT": "http://s3.test",
            "S3_ACCESS_KEY": "test-access",
            "S3_SECRET_KEY": "test-secret",
            "S3_CLEANUP_ACCESS_KEY": "cleanup-access",
            "S3_CLEANUP_SECRET_KEY": "cleanup-secret",
        },
    )
    assert result.returncode != 0
    assert "management client initialization failed" in result.stderr
    assert (tmp_path / "closed").exists()
