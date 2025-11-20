# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import subprocess  # nosec
from typing import Optional

from pydantic import field_validator
from pydantic.dataclasses import dataclass


@dataclass
class CmdArg:
    value: Optional[str] = None
    name: Optional[str] = None

    @field_validator("value", "name", mode="before")
    @staticmethod
    def to_string(value):
        return str(value)


class CmdError(subprocess.CalledProcessError):
    def __init__(
        self, command: str | list[str], return_code: int, stdout: str | None = None, stderr: str | None = None
    ):
        super().__init__(
            returncode=return_code,
            cmd=command,
            output=stdout,
            stderr=stderr,
        )

    def __str__(self) -> str:
        return f"Command '{self.cmd}' exited with return code '{self.returncode}', stderr: {self.stderr}"


class CmdClient:
    def call(self, *args: list[CmdArg]) -> str:
        # Run the command
        parsed_args = self.parse_args(*args)
        result = subprocess.run(self.parse_args(*args), capture_output=True, text=True)  # nosec

        # Check for error
        if result.returncode != 0:
            raise CmdError(" ".join(parsed_args), result.returncode, stdout=result.stdout, stderr=result.stderr)

        return result.stdout

    def parse_args(self, *args: list[CmdArg]) -> list[str]:
        results = []
        for arg in args:
            if arg.name is not None:
                results.append(f"--{arg.name}")
            if arg.value is not None:
                results.append(arg.value)
        return results
