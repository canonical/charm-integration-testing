# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse

from bundle_builder_x.entrypoint import add_args_to_parser


def _parse(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_args_to_parser(parser)
    return parser.parse_args(args)


class TestAddArgsToParser:
    class TestCharmhubUrl:
        def test_defaults_to_none(self) -> None:
            # GIVEN no --charmhub-url flag is passed
            # WHEN the parser runs
            args = _parse(["--spec", "spec.yaml"])
            # THEN the value is None (env var / hardcoded fallback is resolved by CharmhubHttpClient)
            assert args.charmhub_url is None

        def test_accepts_custom_value(self) -> None:
            # GIVEN a custom --charmhub-url flag
            # WHEN the parser runs
            args = _parse(["--spec", "spec.yaml", "--charmhub-url", "https://staging.charmhub.io"])
            # THEN the custom value is used
            assert args.charmhub_url == "https://staging.charmhub.io"

    class TestSnapcraftUrl:
        def test_defaults_to_none(self) -> None:
            # GIVEN no --snapcraft-url flag is passed
            # WHEN the parser runs
            args = _parse(["--spec", "spec.yaml"])
            # THEN the value is None (env var / hardcoded fallback is resolved by SnapstoreHttpClient)
            assert args.snapcraft_url is None

        def test_accepts_custom_value(self) -> None:
            # GIVEN a custom --snapcraft-url flag
            # WHEN the parser runs
            args = _parse(["--spec", "spec.yaml", "--snapcraft-url", "https://staging.snapcraft.io"])
            # THEN the custom value is used
            assert args.snapcraft_url == "https://staging.snapcraft.io"
