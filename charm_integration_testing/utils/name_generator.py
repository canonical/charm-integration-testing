# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import secrets
import string


def generate_juju_name(prefix: str = "charmqa") -> str:
    """Generate a unique Juju controller or model name.

    Names follow the pattern ``{prefix}-{random}``. The random suffix ensures
    uniqueness across concurrent runs. In CI, pass a prefix like
    ``charmqa-{run_id}`` so names are traceable to a specific run.
    """
    random_suffix = "".join(secrets.choice(string.digits) for _ in range(8))
    return f"{prefix}-{random_suffix}"
