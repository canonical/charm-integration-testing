# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import secrets
import string

from .normalization import normalize_string, normalize_string_multiline
from .retry_decorator import retry_on_failure


def generate_juju_name(prefix: str = "charmqa") -> str:
    """Generate a unique Juju controller or model name.

    Names follow the pattern ``{prefix}-{random}``. The random suffix ensures
    uniqueness across concurrent runs. In CI, pass a prefix like
    ``charmqa-{run_id}`` so names are traceable to a specific run.
    """
    alphabet = string.ascii_lowercase + string.digits
    random_suffix = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{prefix}-{random_suffix}"


__all__ = ["generate_juju_name", "normalize_string", "normalize_string_multiline", "retry_on_failure"]
