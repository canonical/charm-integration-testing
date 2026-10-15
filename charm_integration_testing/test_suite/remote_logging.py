# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


def build_remote_logging_endpoints_by_controller(
    target_controller: str,
    target_endpoint: str | None,
    neighbor_controller: str | None,
    neighbor_endpoint: str | None,
) -> dict[str, str]:
    """Build controller-to-Loki mappings, preferring the target endpoint for a shared controller."""
    endpoints: dict[str, str] = {}
    shared_endpoint = target_endpoint or neighbor_endpoint
    if shared_endpoint:
        endpoints[target_controller] = shared_endpoint
    if neighbor_controller is not None and neighbor_controller != target_controller and neighbor_endpoint:
        endpoints[neighbor_controller] = neighbor_endpoint
    return endpoints


def remote_logging_existing_state_warning(
    remote_logging_configured: bool,
    *,
    controller_already_exists: bool,
) -> str | None:
    """Return the warning for an unsupported resumed-run setup, if needed."""
    if not remote_logging_configured or not controller_already_exists:
        return None
    return (
        "Remote logging is not initialized completely when --current-state reuses an existing controller; "
        "controller logs and pre-existing model logs may be absent."
    )
