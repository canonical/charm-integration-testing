# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SETUP = ROOT / "development-sandbox/bin/setup-s3.sh"


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


def test_setup_cleans_outside_bucket_when_creation_response_is_lost(tmp_path: Path) -> None:
    # GIVEN an overly permissive identity whose outside-bucket creation succeeds before raising
    (tmp_path / "boto3.py").write_text(
        "from io import BytesIO\n"
        "from pathlib import Path\n"
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
        "    def close(self): pass\n"
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
    assert "creation response lost" in result.stderr
    deleted = (tmp_path / "deleted").read_text().splitlines()
    assert len(deleted) == 2
    assert (tmp_path / "created").read_text() in deleted
