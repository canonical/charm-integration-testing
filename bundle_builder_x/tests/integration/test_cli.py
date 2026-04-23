# Copyright (C) 2026 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Integration tests for the CLI entrypoint."""

import subprocess
from pathlib import Path

import yaml


def test_cli_writes_bundle_output(tmp_path: Path) -> None:
    # GIVEN a minimal spec file
    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(
        yaml.dump(
            {
                "models": [
                    {
                        "name": "test-model",
                        "platform": "kubernetes",
                        "applications": {"pg": {"charm": "postgresql-k8s", "channel": "14/stable"}},
                    }
                ]
            }
        )
    )
    overrides_path = Path(__file__).parent / "../../../static/charm-overrides"
    output_dir = tmp_path / "bundles"

    # WHEN the CLI is invoked
    result = subprocess.run(
        [
            "bundle-builder-x",
            "--spec",
            str(spec_file),
            "--overrides",
            str(overrides_path),
            "--output-bundles",
            str(output_dir),
        ],
        capture_output=True,
        timeout=300,
    )

    # THEN it succeeds
    assert result.returncode == 0, f"CLI failed: {result.stderr.decode()}"
    # AND the output file exists
    bundle_file = output_dir / "test-model.yaml"
    assert bundle_file.is_file()
    # AND the output is valid YAML with the expected charm
    parsed = yaml.safe_load(bundle_file.read_text())
    assert "pg" in parsed["applications"]


def test_cli_writes_mermaid_output(tmp_path: Path) -> None:
    # GIVEN a minimal spec file
    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(
        yaml.dump(
            {
                "models": [
                    {
                        "name": "test-model",
                        "platform": "kubernetes",
                        "applications": {"pg": {"charm": "postgresql-k8s", "channel": "14/stable"}},
                    }
                ]
            }
        )
    )
    overrides_path = Path(__file__).parent / "../../../static/charm-overrides"
    mermaid_file = tmp_path / "output.md"

    # WHEN the CLI is invoked with mermaid output
    result = subprocess.run(
        [
            "bundle-builder-x",
            "--spec",
            str(spec_file),
            "--overrides",
            str(overrides_path),
            "--output-mermaid",
            str(mermaid_file),
        ],
        capture_output=True,
        timeout=300,
    )

    # THEN it succeeds
    assert result.returncode == 0, f"CLI failed: {result.stderr.decode()}"
    # AND the mermaid file exists with expected content
    assert mermaid_file.is_file()
    content = mermaid_file.read_text()
    assert "```mermaid" in content
    assert "subgraph test-model" in content


def test_cli_missing_spec_fails() -> None:
    # GIVEN a non-existent spec file
    # WHEN the CLI is invoked
    result = subprocess.run(
        ["bundle-builder-x", "--spec", "/tmp/does-not-exist.yaml"],
        capture_output=True,
        timeout=30,
    )

    # THEN it fails with a non-zero exit code
    assert result.returncode != 0


def test_cli_invalid_spec_fails(tmp_path: Path) -> None:
    # GIVEN a spec file with invalid content (empty models)
    spec_file = tmp_path / "bad-spec.yaml"
    spec_file.write_text(yaml.dump({"models": []}))

    # WHEN the CLI is invoked
    result = subprocess.run(
        ["bundle-builder-x", "--spec", str(spec_file)],
        capture_output=True,
        timeout=30,
    )

    # THEN it fails
    assert result.returncode != 0
