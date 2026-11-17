# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

import pytest
import yaml
from juju import JujuClient
from pydantic import BaseModel, Field
from utils.git import GitClient

from bundle_builder_x import Charm


class _Links(BaseModel):
    source: str | list[str] | None = None


class _Metadata(BaseModel):
    name: str | None = None
    links: _Links = Field(default_factory=_Links)
    source: str | list[str] | None = None


@dataclass(frozen=True)
class _CharmSource:
    repository: str
    version: str


def _read_source(archive: zipfile.ZipFile) -> _CharmSource:
    if "version" not in archive.namelist():
        raise ValueError("Packed charm has no version file; cannot identify its source revision.")
    version = archive.read("version").decode("utf-8").strip()
    _version_ref(version)

    source: str | list[str] | None = None
    for filename in ("charmcraft.yaml", "metadata.yaml"):
        if filename not in archive.namelist():
            continue
        metadata = _Metadata.model_validate(yaml.safe_load(archive.read(filename)))
        source = metadata.links.source
        if source is None and filename == "metadata.yaml":
            source = metadata.source
        if source is not None:
            break
    if isinstance(source, list):
        if len(source) != 1:
            raise ValueError("Packed charm must identify exactly one source repository.")
        source = source[0]
    if not source:
        raise ValueError(
            "Packed charm has no source repository in charmcraft.yaml links.source or metadata.yaml source."
        )
    url = urlsplit(source)
    if (
        url.scheme != "https"
        or not url.hostname
        or not url.path.strip("/")
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or any(character.isspace() for character in source)
    ):
        raise ValueError("Charm source must be an HTTPS Git repository URL without credentials, query, or fragment.")
    if url.hostname == "github.com" and len(url.path.strip("/").split("/")) != 2:
        raise ValueError("GitHub source must link to the repository root, not a branch or subdirectory.")
    return _CharmSource(repository=source, version=version)


def _version_ref(version: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", version) or version.endswith("-dirty"):
        raise ValueError("Charm version must identify a clean Git commit, tag, or git-describe revision.")
    described = re.fullmatch(r".+-\d+-g([0-9a-f]{7,40})", version)
    if described:
        return described[1]
    if re.fullmatch(r"[0-9a-f]{7,40}", version):
        return version
    # A branch name must never substitute for the revision that was packed.
    return f"refs/tags/{version}"


def _verify_charm_root(source_dir: Path, expected_name: str) -> None:
    for filename in ("charmcraft.yaml", "metadata.yaml"):
        path = source_dir / filename
        if not path.is_file():
            continue
        metadata = _Metadata.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if metadata.name is not None:
            if metadata.name != expected_name:
                raise ValueError(f"Source checkout contains charm {metadata.name!r}, expected {expected_name!r}.")
            return
    raise ValueError(f"Source checkout has no root charm metadata for {expected_name!r}; monorepos are not supported.")


@pytest.fixture(scope="session")
def target_charm_source(
    target_resolved_charm: Charm,
    juju_client: JujuClient,
    logger: logging.Logger,
) -> Iterator[Path]:
    """Check out the source revision recorded in the target charm's packed version file."""
    # Juju's snap cannot write to pytest's hidden /tmp paths.
    with tempfile.TemporaryDirectory(prefix="cit-target-charm-", dir=Path.home()) as cache_dir:
        cache_path = Path(cache_dir)
        charm_archive = cache_path / "target.charm"
        source_dir = cache_path / "source"
        juju_client.download_charm(
            target_resolved_charm.name,
            target_resolved_charm.revision,
            charm_archive,
        )

        with zipfile.ZipFile(charm_archive) as archive:
            source = _read_source(archive)
        commit = GitClient(logger).checkout(source.repository, _version_ref(source.version), source_dir)
        _verify_charm_root(source_dir, target_resolved_charm.name)

        logger.info(
            "Checked out target charm %s revision %s from %s at %s (packed version %s) to %s.",
            target_resolved_charm.name,
            target_resolved_charm.revision,
            source.repository,
            commit,
            source.version,
            source_dir,
        )
        yield source_dir
