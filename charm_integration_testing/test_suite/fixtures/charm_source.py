# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterator

import pytest
from juju import JujuClient

from bundle_builder_x import Charm


@pytest.fixture(scope="session")
def target_charm_source(
    target_resolved_charm: Charm,
    juju_client: JujuClient,
    logger: logging.Logger,
) -> Iterator[Path]:
    """Download and unpack the target revision once for tests that inspect its source."""
    # Juju's snap cannot write to pytest's hidden /tmp paths.
    with tempfile.TemporaryDirectory(prefix="cit-target-charm-", dir=Path.home()) as cache_dir:
        cache_path = Path(cache_dir)
        charm_archive = cache_path / "target.charm"
        source_dir = cache_path / "source"
        source_dir.mkdir()
        juju_client.download_charm(
            target_resolved_charm.name,
            target_resolved_charm.revision,
            charm_archive,
        )

        with zipfile.ZipFile(charm_archive) as archive:
            for member in archive.infolist():
                path = PurePosixPath(member.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"Charm archive contains an unsafe path: {member.filename!r}")
            archive.extractall(source_dir)

        logger.info(
            "Downloaded target charm %s revision %s to %s.",
            target_resolved_charm.name,
            target_resolved_charm.revision,
            source_dir,
        )
        yield source_dir
