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

"""Unit tests for assertion_tags.py."""

from bundle_builder_x.assertion_tags import (
    Assertions,
    AssertionTag,
    SubordinateBaseMismatchTag,
)


class TestSubordinateBaseMismatchTag:
    """SubordinateBaseMismatchTag: encode/decode and kind."""

    def test_encode_decode_roundtrip(self) -> None:
        tag = SubordinateBaseMismatchTag(
            subordinate_charm_name="nrpe",
            subordinate_charm_id=3,
            subordinate_endpoint="general-info",
            principal_charm_name="ubuntu",
            principal_charm_id=1,
            principal_endpoint="juju-info",
            subordinate_base="24.04",
            principal_base="22.04",
        )
        decoded = AssertionTag.decode(tag.encode())
        assert isinstance(decoded, SubordinateBaseMismatchTag)
        assert decoded.subordinate_charm_name == "nrpe"
        assert decoded.subordinate_charm_id == 3
        assert decoded.subordinate_endpoint == "general-info"
        assert decoded.principal_charm_name == "ubuntu"
        assert decoded.principal_charm_id == 1
        assert decoded.principal_endpoint == "juju-info"
        assert decoded.subordinate_base == "24.04"
        assert decoded.principal_base == "22.04"

    def test_kind_is_subordinate_base_mismatch(self) -> None:
        tag = SubordinateBaseMismatchTag(
            subordinate_charm_name="nrpe",
            subordinate_charm_id=0,
            subordinate_endpoint="general-info",
            principal_charm_name="ubuntu",
            principal_charm_id=1,
            principal_endpoint="juju-info",
            subordinate_base="22.04",
            principal_base="24.04",
        )
        assert tag.kind == Assertions.SUBORDINATE_BASE_MISMATCH
