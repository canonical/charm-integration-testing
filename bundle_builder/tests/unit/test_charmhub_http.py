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


from typing import Any

import pytest
import yaml
from pydantic import Field
from pydantic.dataclasses import dataclass
from requests import Session

from bundle_builder.charmhub_http import (
    CHARM_FIND_ENDPOINT,
    CHARM_INFO_ENDPOINT,
    CHARM_REFRESH_ENDPOINT,
    CharmhubBase,
    CharmhubHttpClient,
    CharmMetadata,
    FindResponse,
    InfoResponse,
    RefreshAction,
    RefreshResponse,
)


class CustomError(Exception):
    pass


@dataclass
class ResponseStub:
    raise_for_status_error: bool = False

    def raise_for_status(self) -> None:
        if self.raise_for_status_error:
            raise CustomError

    json_result: list[str] | dict[str, Any] | None = None

    def json(self) -> list[str] | dict[str, Any] | None:
        return self.json_result


@dataclass
class SessionStub(Session):
    def mount(self, *args: Any, **kwargs: Any) -> None:
        pass

    get_url: str | None = None
    get_params: dict[str, str] | None = None
    get_headers: dict[str, str] | None = None
    get_timeout: int | None = None
    get_result: ResponseStub | None = None

    def get(self, url: str, params: dict[str, str], headers: dict[str, str], timeout: int) -> ResponseStub:  # type: ignore[override]
        if self.get_url is not None:
            assert url == self.get_url
        if self.get_params is not None:
            assert params == self.get_params
        if self.get_headers is not None:
            assert headers == self.get_headers
        if self.get_timeout is not None:
            assert timeout == self.get_timeout
        assert self.get_result is not None
        return self.get_result

    post_url: str | None = None
    post_json: dict[str, Any] | list[Any] | None = None
    post_headers: dict[str, str] | None = None
    post_timeout: int | None = None
    post_result: ResponseStub | None = None

    def post(self, url: str, json: dict[str, Any] | list[Any], headers: dict[str, str], timeout: int) -> ResponseStub:  # type: ignore[override]
        if self.post_url is not None:
            assert url == self.post_url
        if self.post_json is not None:
            assert json == self.post_json
        if self.post_headers is not None:
            assert headers == self.post_headers
        if self.post_timeout is not None:
            assert timeout == self.post_timeout
        assert self.post_result is not None
        return self.post_result


def sample_find_json() -> dict[str, Any]:
    return {
        "results": [
            {
                "name": "kratos",
                "result": {
                    "deployable-on": ["kubernetes"],
                },
            },
        ]
    }


def sample_find_response() -> list[FindResponse]:
    return [FindResponse(**result) for result in sample_find_json()["results"]]


def sample_refresh_json() -> dict[str, Any]:
    return {
        "results": [
            {
                "name": "kratos",
                "charm": {
                    "bases": [
                        {
                            "architecture": "amd64",
                            "channel": "22.04",
                            "name": "ubuntu",
                        },
                    ]
                },
                "revision": 123,
                "metadata": {
                    "requires": {
                        "pg-database": {
                            "interface": "db",
                            "optional": "false",
                        },
                    },
                },
            },
        ]
    }


def sample_refresh_response() -> RefreshResponse:
    return RefreshResponse(**sample_refresh_json()["results"][0])


def sample_info_json() -> dict[str, dict[str, Any]]:
    return {
        "default-release": {
            "revision": {
                "metadata-yaml": yaml.dump(
                    {
                        "requires": {
                            "pg-database": {
                                "interface": "db",
                                "optional": "false",
                            },
                        },
                    }
                ),
            },
        },
        "result": {
            "deployable-on": ["kubernetes"],
        },
    }


def sample_info_response() -> InfoResponse:
    return InfoResponse(**sample_info_json())


class TestRefreshResponse:
    class TestCharm:
        def test_parse_yaml(self) -> None:
            # GIVEN metadata yaml
            metadata_yaml = yaml.dump(
                {
                    "peers": {"endpoint_1": {"interface": "interface_1", "optional": True}},
                    "requires": {"endpoint_2": {"interface": "interface_2", "optional": False}},
                    "provides": {"endpoint_3": {"interface": "interface_3", "optional": None}},
                }
            )

            # WHEN parse yaml method called
            metadata = RefreshResponse.Charm.parse_yaml(metadata_yaml)

            # THEN metadata matches yaml
            assert metadata == CharmMetadata(
                peers={"endpoint_1": CharmMetadata.Endpoint(interface="interface_1", optional=True)},
                requires={"endpoint_2": CharmMetadata.Endpoint(interface="interface_2", optional=False)},
                provides={"endpoint_3": CharmMetadata.Endpoint(interface="interface_3", optional=None)},
            )


class TestCharmhubHttpClient:
    class TestFind:
        @dataclass
        class Params:
            label: str
            provides: str | None = None
            requires: str | None = None
            result: list[FindResponse] = Field(default_factory=list)
            raise_exception: bool = False
            session: SessionStub = Field(default_factory=SessionStub)

        test_cases = [
            Params(
                label="fail",
                raise_exception=True,
                session=SessionStub(get_result=ResponseStub(raise_for_status_error=True)),
            ),
            Params(
                label="no_filters",
                session=SessionStub(
                    get_url=CHARM_FIND_ENDPOINT,
                    get_params={
                        "q": "",
                        "type": "charm",
                        "fields": "result.deployable-on",
                    },
                    get_headers={"Content-Type": "application/json"},
                    get_timeout=180,
                    get_result=ResponseStub(json_result=sample_find_json()),
                ),
                result=sample_find_response(),
            ),
            Params(
                label="all_params",
                provides="db",
                requires="certificates",
                session=SessionStub(
                    get_params={
                        "q": "",
                        "type": "charm",
                        "fields": "result.deployable-on",
                        "provides": "db",
                        "requires": "certificates",
                    },
                    get_result=ResponseStub(json_result=sample_find_json()),
                ),
                result=sample_find_response(),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN the query
            provides = params.provides
            requires = params.requires

            # WHEN find is called
            try:
                result = CharmhubHttpClient(session=params.session).find(provides=provides, requires=requires)
            except CustomError:
                # THEN an exception was expected to be raised
                assert params.raise_exception
            else:
                # OR the result matches
                assert result == params.result

    class TestRefresh:
        @dataclass
        class Params:
            label: str
            action: RefreshAction
            response: RefreshResponse | None = None
            raise_exception: bool = False
            session: SessionStub = Field(default_factory=SessionStub)

        test_cases = [
            Params(
                label="fail",
                action=RefreshAction(
                    charm_name="kratos",
                ),
                raise_exception=True,
                session=SessionStub(post_result=ResponseStub(raise_for_status_error=True)),
            ),
            Params(
                label="no_filters",
                action=RefreshAction(
                    charm_name="kratos",
                ),
                session=SessionStub(
                    post_url=CHARM_REFRESH_ENDPOINT,
                    post_json={
                        "actions": [
                            {
                                "name": "kratos",
                                "action": "install",
                                "instance-key": "1",
                            },
                        ],
                        "context": [],
                        "fields": [
                            "bases",
                            "metadata-yaml",
                            "revision",
                        ],
                    },
                    post_headers={"Content-Type": "application/json"},
                    post_timeout=180,
                    post_result=ResponseStub(json_result=sample_refresh_json()),
                ),
                response=sample_refresh_response(),
            ),
            Params(
                label="all_params",
                action=RefreshAction(
                    charm_name="kratos",
                    charm_revision=123,
                    charm_channel="latest/stable",
                    base=CharmhubBase(
                        architecture="amd64",
                        channel="22.04",
                        name="ubuntu",
                    ),
                ),
                session=SessionStub(
                    post_url=CHARM_REFRESH_ENDPOINT,
                    post_json={
                        "actions": [
                            {
                                "name": "kratos",
                                "action": "install",
                                "instance-key": "1",
                                "revision": 123,
                                "channel": "latest/stable",
                                "base": {
                                    "architecture": "amd64",
                                    "channel": "22.04",
                                    "name": "ubuntu",
                                },
                            },
                        ],
                        "context": [],
                        "fields": [
                            "bases",
                            "metadata-yaml",
                            "revision",
                        ],
                    },
                    post_headers={"Content-Type": "application/json"},
                    post_timeout=180,
                    post_result=ResponseStub(json_result=sample_refresh_json()),
                ),
                response=sample_refresh_response(),
            ),
            Params(
                label="param_always_include_base",
                action=RefreshAction(
                    charm_name="kratos",
                    always_include_base=True,
                ),
                session=SessionStub(
                    post_url=CHARM_REFRESH_ENDPOINT,
                    post_json={
                        "actions": [
                            {
                                "name": "kratos",
                                "action": "install",
                                "instance-key": "1",
                                "base": None,
                            },
                        ],
                        "context": [],
                        "fields": [
                            "bases",
                            "metadata-yaml",
                            "revision",
                        ],
                    },
                    post_headers={"Content-Type": "application/json"},
                    post_timeout=180,
                    post_result=ResponseStub(json_result=sample_refresh_json()),
                ),
                response=sample_refresh_response(),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN the action
            action = params.action

            # WHEN refresh is called
            try:
                response = CharmhubHttpClient(session=params.session).refresh(action)
            except CustomError:
                # THEN an exception was expected to be raised
                assert params.raise_exception
            else:
                # OR the response matches
                assert response == params.response

    class TestInfo:
        @dataclass
        class Params:
            label: str
            charm: str
            response: InfoResponse | None = None
            raise_exception: bool = False
            session: SessionStub = Field(default_factory=SessionStub)

        test_cases = [
            Params(
                label="fail",
                charm="kratos",
                raise_exception=True,
                session=SessionStub(get_result=ResponseStub(raise_for_status_error=True)),
            ),
            Params(
                label="success",
                charm="kratos",
                session=SessionStub(
                    get_url=CHARM_INFO_ENDPOINT.format(charm="kratos"),
                    get_params={
                        "fields": ",".join(
                            [
                                "result.deployable-on",
                                "default-release.revision.metadata-yaml",
                            ]
                        ),
                    },
                    get_headers={"Content-Type": "application/json"},
                    get_timeout=180,
                    get_result=ResponseStub(json_result=sample_info_json()),
                ),
                response=sample_info_response(),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN the charm name
            charm = params.charm

            # WHEN info is called
            try:
                response = CharmhubHttpClient(session=params.session).info(charm)
            except CustomError:
                # THEN an exception was expected to be raised
                assert params.raise_exception
            else:
                # OR the response matches
                assert response == params.response
