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

        @serializeable_dataclass
        class Unit:
            @serializeable_dataclass
            class WorkloadStatus:
                current: str

            @serializeable_dataclass
            class JujuStatus:
                current: str

            workload_status: WorkloadStatus
            juju_status: JujuStatus

        @serializeable_dataclass
        class Integration:
            interface: str
            integrated_application: str = Field(alias="related-application")

        charm: str
        application_status: ApplicationStatus
        integrations: dict[str, list[Integration]] = Field(default_factory=dict, alias="relations")
        units: dict[str, Unit] = Field(default_factory=dict)

    applications: dict[str, Application]


@serializeable_dataclass
class JujuModel:
    type: str
