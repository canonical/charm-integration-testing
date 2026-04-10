# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import re

from .immutable_dataclass import immutable_dataclass

# Matches a Juju version string of the form major[.minor[.patch]][{-|+}suffix].
# Examples: "4", "3.6", "3.6.21", "3.6-beta2", "4.0.6-d20c9e8", "4.1-beta1-c7c73d7",
# "3.6.21-genericlinux-amd64". Rejects channel strings like "3/stable".
_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+)(?:\.(\d+))?)?(?:[-+][a-zA-Z0-9.+\-]+)?", re.ASCII)


@immutable_dataclass(order=True)
class JujuVersion:
    major: int
    minor: int
    patch: int = 0

    @classmethod
    def parse(cls, version: str) -> "JujuVersion":
        match = _VERSION_RE.fullmatch(version)
        if not match:
            raise ValueError(
                f"Cannot parse Juju version from string: {version!r}. "
                "Expected a version starting with digits (e.g. '3.6', '3.6.21', '3.6.21-genericlinux-amd64')."
            )
        return cls(
            major=int(match.group(1)),
            minor=int(match.group(2)) if match.group(2) is not None else 0,
            patch=int(match.group(3)) if match.group(3) is not None else 0,
        )

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"
