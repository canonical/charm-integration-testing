# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
import json
import logging
import os
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path
from uuid import uuid4
from xml.etree.ElementTree import ParseError  # nosec B405

from test_observer_client import TestObserverClient, TestObserverClientError

from bundle_builder_x import Charm, CharmhubClient, CharmReleaseNotFoundException, OverridesClient
from bundle_builder_x.charm import EndpointType

from .config import Config, Scenario, load_config
from .reporting import junit_results

LOGGER = logging.getLogger(__name__)
SUITE_ROOT = Path(__file__).resolve().parents[2]
TEST_SUITE = SUITE_ROOT / "charm_integration_testing" / "test_suite"


def discover(client: CharmhubClient, target: Charm, config: Config, platform: str) -> list[Scenario]:
    """Discover opposite-role neighbors using canonical metadata and existing overrides."""
    scenarios = []
    for endpoint, info in sorted(target.endpoints.items()):
        if info.type == EndpointType.PEERS or (config.endpoints is not None and endpoint not in config.endpoints):
            continue
        opposite = EndpointType.REQUIRES if info.type == EndpointType.PROVIDES else EndpointType.PROVIDES
        candidates = client.find_charms(
            provides=info.interface if opposite == EndpointType.PROVIDES else None,
            requires=info.interface if opposite == EndpointType.REQUIRES else None,
            platform=platform,
        )
        for name in sorted(candidates):
            if config.neighbors is not None and name not in config.neighbors:
                continue
            try:
                neighbor = client.charm_from_store(name, ubuntu_arch=config.arch, platform=platform)
            except CharmReleaseNotFoundException as exc:
                LOGGER.warning("Excluding unavailable neighbor %s: %s", name, exc)
                continue
            for remote, remote_info in sorted(neighbor.endpoints.items()):
                if remote_info.type == opposite and remote_info.interface == info.interface:
                    scenarios.append(Scenario(endpoint=endpoint, neighbor=name, neighbor_endpoint=remote))
    return scenarios


def pytest_command(
    target: Charm, scenario: Scenario, config: Config, cloud: str, platform: str, destination: Path
) -> list[str]:
    """Run the entire suite by default; the scheduler handles selected-test prerequisites."""
    return [
        sys.executable,
        "-m",
        "pytest",
        str(TEST_SUITE),
        *config.pytest_args,
        *scenario.pytest_args,
        "--target-charm",
        target.name,
        "--target-charm-path",
        str(target.local_path),
        "--target-revision",
        "default",
        "--target-resources",
        json.dumps(config.resources),
        "--target-channel",
        config.channel,
        "--target-series",
        target.ubuntu_version,
        "--target-arch",
        config.arch,
        "--target-platform",
        platform,
        "--target-cloud",
        cloud,
        "--target-application",
        "target",
        "--target-endpoint",
        scenario.endpoint,
        "--neighbor-charm",
        scenario.neighbor,
        "--neighbor-application",
        "neighbor",
        "--neighbor-endpoint",
        scenario.neighbor_endpoint,
        "--target-bundle",
        str(destination / "bundle.yaml"),
        "--mermaid-output",
        str(destination / "bundle.mmd"),
        "--charm-overrides",
        str(config.overrides or SUITE_ROOT / "static" / "charm-overrides"),
        "--prefix",
        f"cit-{uuid4().hex[:10]}",
        "--log-dir",
        str(destination / "logs"),
        "--junitxml",
        str(destination / "junit.xml"),
    ]


def run_scenarios(
    target: Charm, scenarios: list[Scenario], config: Config, cloud: str, platform: str, output: Path
) -> int:
    failed = False
    reporting_failed = False
    if config.observer_execution_id is not None and not (
        os.environ.get("TEST_OBSERVER_API") and os.environ.get("TEST_OBSERVER_TOKEN")
    ):
        raise ValueError("Reporting requires both TEST_OBSERVER_API and TEST_OBSERVER_TOKEN")
    for index, scenario in enumerate(scenarios):
        name = f"{index:04d}-{scenario.endpoint}-{scenario.neighbor}-{scenario.neighbor_endpoint}"
        # Endpoint names are supplied by third parties; keep filesystem paths independent.
        destination = output / f"scenario-{index:04d}"
        destination.mkdir()
        (destination / "scenario.json").write_text(scenario.model_dump_json(indent=2), encoding="utf-8")
        command = pytest_command(target, scenario, config, cloud, platform, destination)
        LOGGER.info("Running scenario %s", name)
        result = subprocess.run(command, cwd=SUITE_ROOT, check=False)  # nosec B603
        failed |= result.returncode != 0
        if not (destination / "junit.xml").is_file():
            LOGGER.error("Scenario %s did not produce JUnit results", name)
            failed = True
            continue
        try:
            results = junit_results(destination / "junit.xml", name)
        except (ValueError, ParseError):
            LOGGER.exception("Scenario %s produced invalid or empty JUnit results", name)
            failed = True
            continue
        if config.observer_execution_id is not None:
            try:
                with TestObserverClient(
                    LOGGER, os.environ["TEST_OBSERVER_API"], os.environ["TEST_OBSERVER_TOKEN"]
                ) as observer:
                    observer.post_results(config.observer_execution_id, [result.model_dump() for result in results])
            except TestObserverClientError:
                LOGGER.exception("Test Observer reporting failed for %s", name)
                reporting_failed = True
    return 1 if failed or reporting_failed else 0


def prepare_overrides(config: Config, output: Path) -> None:
    """Retain shipped overrides unless the consumer replaces a specific charm's file."""
    if config.overrides is None:
        return
    destination = output / "overrides"
    shutil.copytree(SUITE_ROOT / "static" / "charm-overrides", destination)
    shutil.copytree(config.overrides, destination, dirs_exist_ok=True)
    config.overrides = destination


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value}")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description="Run integration scenarios against a locally built charm.")
    parser.add_argument("--charm-path", type=Path, required=True)
    parser.add_argument("--cloud", required=True)
    parser.add_argument("--platform", choices=["kubernetes", "machine"], required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--plan", action="store_true", help="Discover and save scenarios without deploying resources.")
    parser.add_argument(
        "--observer-execution-id",
        type=_positive_int,
        default=os.environ.get("TEST_OBSERVER_EXECUTION_ID") or None,
        help="Existing Test Observer execution to report to; overrides the configuration file.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.environ.setdefault("VALIDATORS_PATH", str(SUITE_ROOT / "validators"))
    config = load_config(args.config)
    if args.observer_execution_id is not None:
        config.observer_execution_id = args.observer_execution_id
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "scenarios.json").exists():
        raise ValueError(f"Output directory already contains a run: {output}")
    prepare_overrides(config, output)
    client = CharmhubClient(
        overrides_client=OverridesClient(overrides=config.overrides or SUITE_ROOT / "static" / "charm-overrides")
    )
    target = client.register_local_charm(
        args.charm_path,
        ubuntu_arch=config.arch,
        ubuntu_version=config.base,
        channel=config.channel,
        resources=config.resources,
    )
    if args.platform not in target.platforms:
        raise ValueError(f"Local charm supports {target.platforms}, not {args.platform}")
    scenarios = config.scenarios if config.scenarios is not None else discover(client, target, config, args.platform)
    if not scenarios:
        raise ValueError("No integration scenarios matched; configure explicit scenarios or broaden the filters")
    for scenario in scenarios:
        if scenario.endpoint not in target.endpoints or target.endpoints[scenario.endpoint].type == EndpointType.PEERS:
            raise ValueError(f"Scenario endpoint {scenario.endpoint!r} is not an integration endpoint of {target.name}")
    with (output / "scenarios.json").open("x", encoding="utf-8") as plan:
        json.dump([scenario.model_dump() for scenario in scenarios], plan, indent=2)
    if args.plan:
        return 0
    return run_scenarios(target, scenarios, config, args.cloud, args.platform, output)
