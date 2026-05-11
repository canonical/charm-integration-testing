# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import warnings

from .extension import TemporalExtension

warnings.warn(
    "charm_integration_testing.extensions.temporal is deprecated and will be removed in a future release. "
    "temporal-worker-k8s now connects to Temporal via an explicit relation; "
    "TemporalExtension is no longer needed.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["TemporalExtension"]
