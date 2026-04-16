# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Utilities for querying Juju releases from GitHub and selecting upgrade targets."""

from __future__ import annotations

from enum import Enum
from typing import Any

import requests
from juju.version import JujuVersion

JUJU_RELEASES_URL = "https://api.github.com/repos/juju/juju/releases"
RELEASES_PER_PAGE = 100


class UpgradeMode(str, Enum):
    """How the controller upgrade should be performed."""

    PATCH = "patch"
    MIGRATION = "migration"


def fetch_stable_juju_versions(
    releases_url: str = JUJU_RELEASES_URL,
    per_page: int = RELEASES_PER_PAGE,
) -> list[JujuVersion]:
    """Fetch non-prerelease Juju versions from GitHub releases.

    Returns versions sorted highest-first.
    """
    response = requests.get(
        releases_url,
        params={"per_page": per_page},
        headers={"Accept": "application/vnd.github+json"},
        timeout=30,
    )
    response.raise_for_status()

    releases: list[dict[str, Any]] = response.json()
    versions: list[JujuVersion] = []
    for release in releases:
        if release.get("prerelease") or release.get("draft"):
            continue
        tag: str = release.get("tag_name", "")
        tag = tag.lstrip("v")
        if not tag:
            continue
        try:
            versions.append(JujuVersion.parse(tag))
        except ValueError:
            continue

    return sorted(versions, reverse=True)


def select_upgrade_target(
    current: JujuVersion,
    available: list[JujuVersion],
    allow_higher_major: bool = True,
) -> JujuVersion | None:
    """Choose the best upgrade target from *available* versions.

    Selection policy (first match wins):
    1. Latest version with the same major and same minor but higher patch.
    2. Latest version with the same major but higher minor.
    3. If *allow_higher_major*, latest version with a higher major.

    Returns ``None`` when no valid upgrade target exists.
    """
    # available is sorted highest-first; pick the first match at each tier
    same_minor: JujuVersion | None = None
    same_major: JujuVersion | None = None
    higher_major: JujuVersion | None = None

    for version in available:
        if version <= current:
            continue
        if version.major == current.major and version.minor == current.minor:
            if same_minor is None:
                same_minor = version
        elif version.major == current.major:
            if same_major is None:
                same_major = version
        elif allow_higher_major:
            if higher_major is None:
                higher_major = version

    return same_minor or same_major or higher_major


def classify_upgrade_mode(current: JujuVersion, target: JujuVersion) -> UpgradeMode:
    """Decide whether the upgrade is a patch or migration.

    Patch: same major *and* same minor (only patch differs).
    Migration: different major or different minor.
    """
    if current.major == target.major and current.minor == target.minor:
        return UpgradeMode.PATCH
    return UpgradeMode.MIGRATION
