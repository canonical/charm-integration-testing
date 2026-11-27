import logging
from typing import NoReturn

import pytest
from test_suite.fixtures.charm_source import target_charm_source

from bundle_builder_x import AssumesMismatchError, ReleaseRequest

__all__ = ["target_charm_source"]


@pytest.fixture(scope="session")
def logger() -> logging.Logger:
    return logging.getLogger("charmlint-resolution")


@pytest.fixture(scope="session")
def git_client() -> object:
    return object()


@pytest.fixture(scope="session")
def target_resolved_charm() -> None:
    return None


@pytest.fixture(scope="session")
def bundle_solution() -> NoReturn:
    raise AssumesMismatchError(
        request=ReleaseRequest(charm_name="ceph-radosgw", juju_version="3.6.29"),
        unmet_requirements=("feature=unsupported-openstack",),
        available_features=("juju",),
    )


@pytest.fixture(scope="session")
def juju_client() -> NoReturn:
    pytest.fail("Juju must not be acquired for an unresolved target")
