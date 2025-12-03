# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from pydantic import ConfigDict
from pydantic.dataclasses import dataclass


def serializeable_dataclass(cls: type) -> type:
    return dataclass(config=ConfigDict(alias_generator=lambda s: s.replace("_", "-")))(cls)
