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

import argparse
import logging

from .charm import Charm
from .charmqa import (
    build_charm_graph,
    dump_selected_bundle_to_file,
    filter_to_shortest_paths,
    find_all_paths,
    group_paths_by_interface,
    render_all_generated_bundles,
)


def setup_logging(loglevel: str):
    logger = logging.getLogger("bundle_builder")

    numeric_level = getattr(logging, loglevel.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError("Invalid log level: %s" % loglevel)

    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setLevel(numeric_level)

    formatter = logging.Formatter("%(asctime)s - [%(levelname)s] - %(name)s - %(message)s")

    ch.setFormatter(formatter)
    logger.addHandler(ch)

    return logger


def channel_or_revision(parsed_arg: str, logger=logging.getLogger("bundle_builder")):
    if parsed_arg.isnumeric():
        logger.debug(f"Using charm revision as second argument of input format was numeric. Input: {parsed_arg}")
        return {"charm_revision": int(parsed_arg)}

    logger.debug(f"Using charm channel as second argument of input format was NOT numeric. Input: {parsed_arg}")
    return {"charm_channel": parsed_arg}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-depth", type=int, default=2, help="How deep to generate the graph.")
    parser.add_argument(
        "--target-charm",
        type=str,
        help="In the format of <charmname>::<charm_channel_or_version>::<arch>::<ubuntu_version>",
        required=True,
    )
    parser.add_argument(
        "--support-charm",
        type=str,
        nargs="+",
        help="In the format of <charmname>::<charm_channel_or_version>::<arch>::<ubuntu_version>",
        required=True,
    )
    parser.add_argument(
        "--target-interface",
        type=str,
        help="For which interface on the target charm are we generating bundles for.",
        required=True,
    )
    parser.add_argument(
        "--output-file", type=str, help="Where to save the generated bundle", default="generated_bundle.yaml"
    )
    parser.add_argument("--log-level", choices=["INFO", "DEBUG", "WARNING", "ERROR", "CRITICAL"], default="WARNING")
    args = parser.parse_args()

    LOGGER = setup_logging(args.log_level)

    tcharm_parts = args.target_charm.split("::", maxsplit=4)

    target = Charm.from_store(
        charm_name=tcharm_parts[0],
        ubuntu_arch=tcharm_parts[2],
        ubuntu_version=tcharm_parts[3],
        logger=LOGGER,
        **channel_or_revision(parsed_arg=tcharm_parts[1], logger=LOGGER),
    )

    support_charms = []

    for scharm in args.support_charm:
        # format: <charmname>::<charm_channel>::<arch>::<ubuntu_version>
        scharm_parts = scharm.split("::", maxsplit=4)
        support_charms.append(
            Charm.from_store(
                charm_name=scharm_parts[0],
                ubuntu_arch=scharm_parts[2],
                ubuntu_version=scharm_parts[3],
                logger=LOGGER,
                **channel_or_revision(parsed_arg=scharm_parts[1], logger=LOGGER),
            )
        )

    graph = build_charm_graph(target, max_depth=args.max_depth, logger=LOGGER)
    all_paths = find_all_paths(graph=graph, root_node=target)
    grouped_paths = group_paths_by_interface(all_paths)
    selected_paths = filter_to_shortest_paths(
        target=target, grouped_paths=grouped_paths, support_charms=support_charms, logger=LOGGER
    )
    rendered_bundles = render_all_generated_bundles(selected_paths=selected_paths, logger=LOGGER)
    dump_selected_bundle_to_file(
        rendered_bundles=rendered_bundles,
        selected_interface=args.target_interface,
        filename=args.output_file,
        logger=LOGGER,
    )


if __name__ == "__main":
    main()
