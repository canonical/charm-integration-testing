# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import re

_UNSAFE_PATH_CHARS = re.compile(r"[^a-zA-Z0-9_\-]")


def sanitize_path_segment(value: str) -> str:
    """Replace characters that are unsafe in resource log file names."""
    return _UNSAFE_PATH_CHARS.sub("-", value)
