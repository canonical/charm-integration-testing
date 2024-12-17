# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import subprocess
from typing import Optional
import sys

from pydantic.dataclasses import dataclass


@dataclass
class CmdArg:
    value: Optional[str]
    name: Optional[str] = None

class CmdError(RuntimeError):
    def __init__(self, command: str, return_code: int, stdout: str = "", stderr: str = ""):
        super().__init__(f"Command '{command}' exited with return code '{return_code}'")

        self.command = command
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr


class CmdClient:
    def call(self, *args: list[CmdArg]) -> None:
        # Run the command
        parsed_args = self.parse_args(*args)
        result = subprocess.run(self.parse_args(*args), capture_output=True, text=True)

        # Print the results
        print(result.stdout, end="")
        print(result.stderr, file=sys.stderr, end="")

        # Check for error
        if result.returncode != 0:
            raise CmdError(" ".join(parsed_args), result.returncode, stdout=result.stdout, stderr=result.stderr)

    def parse_args(self, *args: list[CmdArg]) -> list[str]:
        results = []
        for arg in args:
            if arg.value is None:
                continue
            if arg.name is not None:
                results.append(f"--{arg.name}")
            results.append(arg.value)
        return results
