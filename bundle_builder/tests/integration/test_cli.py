# Copyright (C) 2025 Canonical Ltd

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


import subprocess
from pathlib import Path

import yaml

from bundle_builder import OverridesClient


def test_cli_write_output(
    tmp_path: Path,
    sample_independent_charm: str,
    sample_independent_charm_revision: int,
    overrides_client: OverridesClient,
) -> None:
    # GIVEN an output file
    output_bundle = tmp_path / "output_bundle.yaml"
    # AND the bundle doesn't exist
    assert not output_bundle.exists()

    # WHEN the bundle builder is run from cli
    result = subprocess.run(
        [
            "bundle-builder",
            "--charms",
            f"{sample_independent_charm}::{sample_independent_charm}::{sample_independent_charm_revision}::default",
            "--charm-metadata-overrides",
            overrides_client.charm_metadata_overrides.resolve().absolute(),
            "--output-file",
            output_bundle.absolute(),
        ],
        capture_output=True,
    )

    # THEN the cli succeeds
    result.check_returncode()
    # AND the output exists
    assert output_bundle.is_file()
    # AND the bundle is yaml
    with output_bundle.open("r") as f:
        bundle_specs = yaml.safe_load(f)
    # AND the bundle contains the expected charms
    assert {application for application in bundle_specs.get("applications", {})} == {sample_independent_charm}


def test_cli_unknown_charm() -> None:
    # GIVEN an unknown charm
    app = "app::unknown::default::default"

    # WHEN the bundle builder is run from cli
    result = subprocess.run(
        [
            "bundle-builder",
            "--charms",
            app,
        ],
        capture_output=True,
    )

    # THEN the cli fails
    assert result.returncode == 2
    # AND the error message indicates the unknown charm
    assert "Charm release not found" in result.stderr.decode()


def test_cli_invalid_integration(
    sample_independent_charm: str,
    sample_independent_charm_endpoint: str,
    sample_independent_charm_revision: int,
) -> None:
    # GIVEN two of the same charm
    app_1 = f"app1::{sample_independent_charm}::{sample_independent_charm_revision}::default"
    app_2 = f"app2::{sample_independent_charm}::{sample_independent_charm_revision}::default"
    # AND an invalid integration between them
    integration = f"app1:{sample_independent_charm_endpoint}::app2:{sample_independent_charm_endpoint}"

    # WHEN the bundle builder is run from cli
    result = subprocess.run(
        [
            "bundle-builder",
            "--charms",
            app_1,
            app_2,
            "--integrations",
            integration,
        ],
        capture_output=True,
    )

    # THEN the cli fails
    assert result.returncode == 2
    # AND the error message indicates the invalid integration
    assert "Incompatible endpoint types in integration" in result.stderr.decode()
