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
import dataclasses
import logging
from pathlib import Path

import pytest
from pydantic.dataclasses import dataclass

from bundle_builder.bundle import Application, ApplicationEndpoint, Integration
from bundle_builder.charm import Charm, CharmChannel
from bundle_builder.charmhub_http import CharmReleaseNotFoundException
from bundle_builder.entrypoint import (
    add_args_to_parser,
    applications_from_args,
    integrations_from_args,
    platform_from_args,
    setup_logging,
    write_to_file,
)

from .test_charm import sample_charm_postgresql_k8s, sample_charm_self_signed_certificates


class TestSetupLogging:
    def test_valid_level(self) -> None:
        # GIVEN a valid log level
        log_level = "DEBUG"

        # WHEN setting up logging
        logger = setup_logging(log_level)

        # THEN can log with logger
        logger.debug("Test logging")


class TestAddArgsToParser:
    @dataclass
    class Params:
        label: str
        args: list[str]
        fail: bool

    good_args = {
        "--charms": "target::postgresql-k8s::default::default::default",
        "--integrations": "target:certificates::neighbor:certificates",
        "--arch": "amd64",
        "--substrate": "kubernetes",
        "--output-file": "bundle.yaml",
        "--charm-metadata-overrides": "some/folder/directory",
        "--charm-platform-overrides": "some/folder/directory",
        "--charm-listing-overrides": "some/folder/directory/file.yaml",
        "--charm-test-configs": "some/folder/directory",
        "--log-level": "DEBUG",
    }

    test_cases = [
        Params(label="all_args", args=[arg for args in good_args.items() for arg in args], fail=False),
        Params(
            label="no_charms",
            args=[arg for args in good_args.items() if args[0] != "--charms" for arg in args],
            fail=True,
        ),
        Params(
            label="unknown_arch",
            args=[arg for args in {**good_args, **{"--arch": "unknown"}}.items() for arg in args],
            fail=True,
        ),
        Params(
            label="unknown_substrate",
            args=[arg for args in {**good_args, **{"--substrate": "unknown"}}.items() for arg in args],
            fail=True,
        ),
        Params(
            label="unknown_log_level",
            args=[arg for args in {**good_args, **{"--log-level": "unknown"}}.items() for arg in args],
            fail=True,
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test(self, params: Params) -> None:
        # GIVEN a new argument parser
        parser = argparse.ArgumentParser()

        # WHEN arguments are added to parser
        add_args_to_parser(parser)

        # THEN the arguments are parsed with the expected error scenario
        try:
            parser.parse_args(args=params.args)
        except SystemExit:
            assert params.fail
        else:
            assert not params.fail


class ArgumentParserStub:
    def error(self, message: str) -> None:
        raise RuntimeError


class TestApplicationFromArgs:
    class CharmhubClientStub:
        def charm_from_store(
            self,
            charm_name: str,
            ubuntu_arch: str,
            charm_channel: str | None = None,
            charm_revision: int | None = None,
            ubuntu_version: str | None = None,
        ) -> Charm:
            if charm_name == "postgresql-k8s":
                charm = sample_charm_postgresql_k8s()
            elif charm_name == "self-signed-certificates":
                charm = sample_charm_self_signed_certificates()
            else:
                raise CharmReleaseNotFoundException(f"Charm release not found: {charm_name}")

            return dataclasses.replace(
                charm,
                ubuntu_arch=ubuntu_arch,
                channel=CharmChannel(charm_channel) if charm_channel is not None else charm.channel,
                revision=charm_revision or charm.revision,
                ubuntu_version=ubuntu_version or charm.ubuntu_version,
            )

    @dataclass
    class Params:
        label: str
        specs: list[str]
        arch: str = "amd64"
        fail: bool = False
        applications: set[Application] | None = None

    test_cases = [
        Params(
            label="parse_charm",
            specs=["target::postgresql-k8s::default::default::default"],
            applications={Application(name="target", charm=sample_charm_postgresql_k8s())},
        ),
        Params(
            label="parse_revision",
            specs=["target::postgresql-k8s::default::111::default"],
            applications={
                Application(name="target", charm=dataclasses.replace(sample_charm_postgresql_k8s(), revision=111))
            },
        ),
        Params(
            label="parse_channel",
            specs=["target::postgresql-k8s::edge::default::default"],
            applications={
                Application(
                    name="target",
                    charm=dataclasses.replace(sample_charm_postgresql_k8s(), channel=CharmChannel("edge")),
                )
            },
        ),
        Params(
            label="parse_base",
            specs=["target::postgresql-k8s::default::default::24.04"],
            applications={
                Application(
                    name="target",
                    charm=dataclasses.replace(sample_charm_postgresql_k8s(), ubuntu_version="24.04"),
                )
            },
        ),
        Params(
            label="parse_multiple",
            specs=[
                "target::postgresql-k8s::default::default::default",
                "neighbor::self-signed-certificates::default::default::default",
            ],
            applications={
                Application(name="target", charm=sample_charm_postgresql_k8s()),
                Application(name="neighbor", charm=sample_charm_self_signed_certificates()),
            },
        ),
        Params(
            label="bad_format",
            specs=["bad::format"],
            fail=True,
        ),
        Params(
            label="unknown_charm",
            specs=["app::unknown::default::default::default"],
            fail=True,
        ),
        Params(
            label="parse_channel_and_revision",
            specs=["target::postgresql-k8s::edge::111::default"],
            applications={
                Application(
                    name="target",
                    charm=dataclasses.replace(sample_charm_postgresql_k8s(), channel=CharmChannel("edge"), revision=111),
                )
            },
        ),
        Params(
            label="parse_channel_and_base",
            specs=["target::postgresql-k8s::stable::default::24.04"],
            applications={
                Application(
                    name="target",
                    charm=dataclasses.replace(sample_charm_postgresql_k8s(), channel=CharmChannel("stable"), ubuntu_version="24.04"),
                )
            },
        ),
        Params(
            label="parse_all_specified",
            specs=["target::postgresql-k8s::edge::111::24.04"],
            applications={
                Application(
                    name="target",
                    charm=dataclasses.replace(
                        sample_charm_postgresql_k8s(),
                        channel=CharmChannel("edge"),
                        revision=111,
                        ubuntu_version="24.04",
                    ),
                )
            },
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test(self, params: Params) -> None:
        # GIVEN stubbed charmhub client
        charmhub_client = self.CharmhubClientStub()
        # AND stubbed argument parser
        parser = ArgumentParserStub()

        # WHEN called with the specs
        try:
            applications = applications_from_args(parser, charmhub_client, params.specs, params.arch)  # type: ignore[arg-type]
        except RuntimeError:
            threw = True
        else:
            threw = False

        # THEN failure scenario thrown if expected
        assert threw == params.fail
        # AND expected applications match if not thrown
        if not threw:
            assert params.applications is not None
            assert applications == frozenset(params.applications)


class TestIntegrationFromArgs:
    @dataclass
    class Params:
        label: str
        specs: list[str]
        fail: bool = False
        integrations: set[Integration] | None = None

    test_cases = [
        Params(
            label="parse_integration",
            specs=["a:b::c:d"],
            integrations={frozenset({ApplicationEndpoint("a", "b"), ApplicationEndpoint("c", "d")})},
        ),
        Params(
            label="parse_multiple",
            specs=[
                "a:b::c:d",
                "e:f::g:h",
            ],
            integrations={
                frozenset({ApplicationEndpoint("a", "b"), ApplicationEndpoint("c", "d")}),
                frozenset({ApplicationEndpoint("e", "f"), ApplicationEndpoint("g", "h")}),
            },
        ),
        Params(
            label="bad_format",
            specs=["bad::format"],
            fail=True,
        ),
        Params(
            label="bad_sub_format",
            specs=["a:b::bad_format"],
            fail=True,
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test(self, params: Params) -> None:
        # GIVEN stubbed argument parser
        parser = ArgumentParserStub()

        # WHEN called with the specs
        try:
            integrations = integrations_from_args(parser, params.specs)  # type: ignore[arg-type]
        except RuntimeError:
            threw = True
        else:
            threw = False

        # THEN failure scenario thrown if expected
        assert threw == params.fail
        # AND expected integrations match if not thrown
        if not threw:
            assert params.integrations is not None
            assert integrations == frozenset(params.integrations)


class TestPlatformFromArgs:
    @dataclass
    class Params:
        label: str
        substrate: str
        fail: bool = False
        platform: str = "kubernetes"

    test_cases = [
        Params(
            label="known_substrate",
            substrate="kubernetes",
            platform="kubernetes",
        ),
        Params(
            label="unknown_substrate",
            substrate="unknown",
            fail=True,
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test(self, params: Params) -> None:
        # GIVEN stubbed argument parser
        parser = ArgumentParserStub()

        # WHEN called with the specs
        try:
            platform = platform_from_args(parser, params.substrate)  # type: ignore[arg-type]
        except RuntimeError:
            threw = True
        else:
            threw = False

        # THEN failure scenario thrown if expected
        assert threw == params.fail
        # AND expected platform match if not thrown
        if not threw:
            assert platform == params.platform


class TestWriteToFile:
    def test_write(self, tmp_path: Path) -> None:
        # GIVEN content to write
        content = "my bundle string"
        # AND a file to write to
        file_path = tmp_path / "generated-bundle.yaml"

        # WHEN called to write to file
        write_to_file(str(file_path.absolute().resolve()), content, logging.getLogger())

        # THEN content is written
        assert file_path.read_text() == "my bundle string"
