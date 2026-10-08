# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

from bundle_builder_x import AppSpec, Charm


def application_specs(
    target_charm: str,
    target_channel: str | None,
    target_revision: int | None,
    target_series: str | None,
    local_target_charm: Path | None,
    target_resolved_charm: Charm,
    neighbor_charm: str,
) -> tuple[AppSpec, AppSpec]:
    """Create app specs, using the resolved base for local targets."""
    target_base = target_resolved_charm.ubuntu_version if local_target_charm is not None else target_series
    target_app_spec = AppSpec(
        charm=target_charm,
        channel=str(target_resolved_charm.channel) if local_target_charm is not None else target_channel,
        revision=target_resolved_charm.revision if local_target_charm is not None else target_revision,
        base=target_base,
        local_charm=local_target_charm,
    )
    return target_app_spec, AppSpec(charm=neighbor_charm)
