# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from serializeable_dataclass import serializeable_dataclass


@serializeable_dataclass
class JujuExecTask:
    @serializeable_dataclass
    class Results:
        return_code: int
        stdout: str = ""
        stderr: str = ""

    results: Results
