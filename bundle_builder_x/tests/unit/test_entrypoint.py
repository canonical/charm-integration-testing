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

import argparse

from bundle_builder_x.charmhub_http import DEFAULT_CHARMHUB_API_URL
from bundle_builder_x.entrypoint import add_args_to_parser
from bundle_builder_x.snapstore_http import DEFAULT_SNAPCRAFT_API_URL


def _parse(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_args_to_parser(parser)
    return parser.parse_args(args)


class TestAddArgsToParser:
    class TestCharmhubUrl:
        def test_defaults_to_production(self) -> None:
            # GIVEN no --charmhub-url flag is passed
            # WHEN the parser runs
            args = _parse(["--spec", "spec.yaml"])
            # THEN the default is the production Charmhub URL
            assert args.charmhub_url == DEFAULT_CHARMHUB_API_URL

        def test_accepts_custom_value(self) -> None:
            # GIVEN a custom --charmhub-url flag
            # WHEN the parser runs
            args = _parse(["--spec", "spec.yaml", "--charmhub-url", "https://staging.charmhub.io"])
            # THEN the custom value is used
            assert args.charmhub_url == "https://staging.charmhub.io"

    class TestSnapcraftUrl:
        def test_defaults_to_production(self) -> None:
            # GIVEN no --snapcraft-url flag is passed
            # WHEN the parser runs
            args = _parse(["--spec", "spec.yaml"])
            # THEN the default is the production Snapcraft URL
            assert args.snapcraft_url == DEFAULT_SNAPCRAFT_API_URL

        def test_accepts_custom_value(self) -> None:
            # GIVEN a custom --snapcraft-url flag
            # WHEN the parser runs
            args = _parse(["--spec", "spec.yaml", "--snapcraft-url", "https://staging.snapcraft.io"])
            # THEN the custom value is used
            assert args.snapcraft_url == "https://staging.snapcraft.io"
