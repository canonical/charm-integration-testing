# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

from pydantic import Field
from serializeable_dataclass import serializeable_dataclass


@serializeable_dataclass
class JujuStatus:
    @serializeable_dataclass
    class Application:
        @serializeable_dataclass
        class ApplicationStatus:
            current: str
            message: str = ""

        @serializeable_dataclass
        class Unit:
            @serializeable_dataclass
            class WorkloadStatus:
                current: str
                message: str = ""

            @serializeable_dataclass
            class JujuStatus:
                current: str

            workload_status: WorkloadStatus
            juju_status: JujuStatus

        @serializeable_dataclass
        class Integration:
            interface: str
            integrated_application: str = Field(alias="related-application")

        status_error: str | None = None
        charm: str | None = None
        application_status: ApplicationStatus | None = None
        integrations: dict[str, list[Integration]] = Field(default_factory=dict, alias="relations")
        units: dict[str, Unit] = Field(default_factory=dict)
        scale: int = 0

    applications: dict[str, Application]


@serializeable_dataclass
class JujuModel:
    type: str


@serializeable_dataclass
class JujuSecretInfo:
    name: str | None = None
    content: dict[str, str] | None = None
