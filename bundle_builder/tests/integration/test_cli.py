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


def test_cli_write_output(tmp_path: Path, sample_independent_charm: str):
    # GIVEN an output file
    output_bundle = tmp_path / "output_bundle.yaml"
    # AND the bundle doesn't exist
    assert not output_bundle.exists()

    # WHEN the bundle builder is run from cli
    result = subprocess.run(
        [
            "bundle-builder",
            "--charms",
            f"{sample_independent_charm}::{sample_independent_charm}::default::default",
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
