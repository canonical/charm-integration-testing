# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from juju.version import JujuVersion
from utils.juju_releases import UpgradeMode, classify_upgrade_mode, select_upgrade_target


class TestSelectUpgradeTarget:
    def test_selects_latest_above_current(self) -> None:
        # GIVEN a current version and several available versions
        current = JujuVersion(3, 6, 18)
        available = [JujuVersion(3, 6, 21), JujuVersion(3, 6, 20), JujuVersion(3, 6, 17)]

        # WHEN selecting the upgrade target
        result = select_upgrade_target(current, available)

        # THEN the latest version above current is selected
        assert result == JujuVersion(3, 6, 21)

    def test_skips_lower_or_equal_versions(self) -> None:
        # GIVEN no version above current
        current = JujuVersion(4, 0, 5)
        available = [JujuVersion(4, 0, 5), JujuVersion(3, 6, 21), JujuVersion(2, 9, 57)]

        # WHEN selecting the upgrade target
        result = select_upgrade_target(current, available)

        # THEN no target is found
        assert result is None

    def test_prefers_same_minor_patch_over_higher_minor(self) -> None:
        # GIVEN both a same-minor patch and a higher-minor version available
        current = JujuVersion(3, 5, 10)
        available = [JujuVersion(3, 6, 21), JujuVersion(3, 5, 11)]

        # WHEN selecting the upgrade target
        result = select_upgrade_target(current, available)

        # THEN the same-minor patch is preferred
        assert result == JujuVersion(3, 5, 11)

    def test_falls_back_to_higher_minor_when_no_patch(self) -> None:
        # GIVEN only a higher-minor version available (no same-minor patch)
        current = JujuVersion(3, 5, 10)
        available = [JujuVersion(3, 6, 21), JujuVersion(3, 5, 9)]

        # WHEN selecting the upgrade target
        result = select_upgrade_target(current, available)

        # THEN the higher-minor version is chosen
        assert result == JujuVersion(3, 6, 21)

    def test_prefers_same_major_over_higher_major(self) -> None:
        # GIVEN both a same-major higher-minor and a higher-major version
        current = JujuVersion(3, 5, 10)
        available = [JujuVersion(4, 0, 5), JujuVersion(3, 6, 21)]

        # WHEN selecting the upgrade target
        result = select_upgrade_target(current, available)

        # THEN the same-major version is preferred
        assert result == JujuVersion(3, 6, 21)

    def test_crosses_major_boundary_when_allowed(self) -> None:
        # GIVEN a higher major version available
        current = JujuVersion(3, 6, 21)
        available = [JujuVersion(4, 0, 5)]

        # WHEN selecting with allow_higher_major=True
        result = select_upgrade_target(current, available, allow_higher_major=True)

        # THEN the higher-major target is returned
        assert result == JujuVersion(4, 0, 5)

    def test_rejects_higher_major_when_disallowed(self) -> None:
        # GIVEN only a higher major available
        current = JujuVersion(3, 6, 21)
        available = [JujuVersion(4, 0, 5)]

        # WHEN selecting with allow_higher_major=False
        result = select_upgrade_target(current, available, allow_higher_major=False)

        # THEN no target is found
        assert result is None

    def test_returns_none_for_empty_list(self) -> None:
        # GIVEN no available versions
        current = JujuVersion(3, 6, 18)

        # WHEN selecting
        result = select_upgrade_target(current, [])

        # THEN None is returned
        assert result is None


class TestClassifyUpgradeMode:
    def test_patch_same_major_minor(self) -> None:
        # GIVEN same major and minor, different patch
        current = JujuVersion(3, 6, 18)
        target = JujuVersion(3, 6, 21)

        # WHEN classifying
        mode = classify_upgrade_mode(current, target)

        # THEN it is a patch upgrade
        assert mode == UpgradeMode.PATCH

    def test_migration_different_minor(self) -> None:
        # GIVEN same major but different minor
        current = JujuVersion(3, 5, 10)
        target = JujuVersion(3, 6, 21)

        # WHEN classifying
        mode = classify_upgrade_mode(current, target)

        # THEN it is a migration upgrade
        assert mode == UpgradeMode.MIGRATION

    def test_migration_different_major(self) -> None:
        # GIVEN different major
        current = JujuVersion(3, 6, 21)
        target = JujuVersion(4, 0, 5)

        # WHEN classifying
        mode = classify_upgrade_mode(current, target)

        # THEN it is a migration upgrade
        assert mode == UpgradeMode.MIGRATION
