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
import logging
from pathlib import Path

from .bundle_builder import BundleBuilder, UncompletableBundleError
from .charmhub import CharmhubClient
from .charmhub_http import DEFAULT_CHARMHUB_API_URL, CharmhubHttpClient
from .overrides import OverridesClient
from .snapstore import SnapstoreClient
from .snapstore_http import DEFAULT_SNAPCRAFT_API_URL, SnapstoreHttpClient
from .spec import SpecFile
from .timing import Timeline


def setup_logging(log_level: str) -> logging.Logger:
    logger = logging.getLogger("bundle_builder")

    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setLevel(getattr(logging, log_level.upper()))

    formatter = logging.Formatter("%(asctime)s - [%(levelname)s] - %(name)s - %(message)s")

    ch.setFormatter(formatter)
    logger.addHandler(ch)
    logger.propagate = False

    return logger


def add_args_to_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--spec",
        type=Path,
        help="Path to the spec YAML file describing models, applications, and integrations.",
        required=True,
    )
    parser.add_argument(
        "--output-bundles",
        type=Path,
        help="Directory to write per-model bundle YAML files.",
        default=None,
    )
    parser.add_argument(
        "--output-mermaid",
        type=Path,
        help="File path to write the Mermaid diagram (e.g. solution.md).",
        default=None,
    )
    parser.add_argument(
        "--output-timeline",
        type=str,
        help="Where to save the build timing timeline (Mermaid Gantt).",
        default=None,
    )
    parser.add_argument(
        "--overrides",
        type=Path,
        help="Path to folder containing per-charm override directories",
        default=None,
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=["INFO", "DEBUG", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
    )
    parser.add_argument(
        "--charmhub-url",
        type=str,
        help="Base URL for the Charmhub API.",
        default=DEFAULT_CHARMHUB_API_URL,
    )
    parser.add_argument(
        "--snapcraft-url",
        type=str,
        help="Base URL for the Snapcraft API.",
        default=DEFAULT_SNAPCRAFT_API_URL,
    )


def write_to_file(filename: str | Path, content: str, logger: logging.Logger) -> None:
    path = Path(filename).absolute().resolve()
    logger.info(f"Writing to '{path}'")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    logger.info("Saved file")


def main() -> None:
    # Get CLI arguments
    parser = argparse.ArgumentParser()
    add_args_to_parser(parser)
    args = parser.parse_args()

    # Get logger
    logger = setup_logging(args.log_level)

    # Validate spec file
    if not args.spec.is_file():
        parser.error(f"Spec file '{args.spec}' does not exist or is not a file.")

    spec = SpecFile.load(args.spec)

    # Create override client
    if args.overrides is not None and not args.overrides.is_dir():
        parser.error(f"The charm overrides path '{args.overrides}' is not a valid directory.")

    # Create timeline if requested
    timeline: Timeline | None = None
    if args.output_timeline:
        timeline = Timeline(logger=logger)

    overrides_client = OverridesClient(
        overrides=args.overrides,
        timeline=timeline,
    )

    # Create HTTP clients
    charmhub_http = CharmhubHttpClient(logger=logger.getChild("charmhub"), base_url=args.charmhub_url)
    snapstore_http = SnapstoreHttpClient(logger=logger, base_url=args.snapcraft_url)

    # Create Charmhub client
    charmhub_client = CharmhubClient(
        http_client=charmhub_http,
        logger=logger,
        overrides_client=overrides_client,
        timeline=timeline,
    )

    # Create Bundle Builder
    bundle_builder = BundleBuilder(
        charmhub_client=charmhub_client,
        snapstore_client=SnapstoreClient(http_client=snapstore_http, logger=logger),
        logger=logger,
        timeline=timeline,
    )

    # Build all models simultaneously
    try:
        solution = bundle_builder.build(spec)
    except UncompletableBundleError as e:
        parser.error(f"Uncompletable bundle: {e}")

    # Output bundles
    for bundle in solution.bundles:
        display_name = bundle.model or "_default"
        bundle_yaml = bundle.export()
        logger.info(f"Model '{display_name}' bundle:\n{'-' * 80}\n{bundle_yaml}{'-' * 80}")

        if args.output_bundles:
            write_to_file(args.output_bundles / f"{display_name}.yaml", bundle_yaml, logger)

    if args.output_mermaid:
        is_markdown = str(args.output_mermaid).endswith(".md")
        write_to_file(
            args.output_mermaid,
            solution.export_mermaid(markdown=is_markdown),
            logger,
        )

    # Export the timeline
    if args.output_timeline and timeline:
        write_to_file(
            args.output_timeline,
            timeline.mermaid(markdown=str(args.output_timeline).endswith(".md")),
            logger,
        )


if __name__ == "__main__":  # pragma: no cover
    main()
