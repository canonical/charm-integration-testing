# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta

from validators.base import ValidationResult

from .backend import JujuBackend
from .extension import JujuExtension
from .models import JujuApplicationInfo, JujuIntegration, JujuIntegrationApplication


class JujuValidationError(Exception):
    """Raised when validation of a Juju model fails."""

    failed_validations: dict[str, list[ValidationResult]]

    def __init__(self, failed_validations: dict[str, list[ValidationResult]]):
        self.failed_validations = failed_validations

        units = ", ".join(failed_validations.keys())
        num_failed_validations = sum(len(validations) for validations in failed_validations.values())
        super().__init__(f"Model validation failed with {num_failed_validations} failed validations (units: {units}).")


class JujuClient:
    backend: JujuBackend
    logger: logging.Logger
    extensions: list[JujuExtension]

    def __init__(self, backend: JujuBackend, logger: logging.Logger, extensions: list[JujuExtension] | None = None):
        self.backend = backend
        self.logger = logger
        self.extensions = extensions or []

    def scale_application(self, application: str, num: int, model: str = "default") -> None:
        self.logger.info(f"Scaling application {application} to {num} units.")
        self.backend.scale_application(model, application, num)

        # Call extensions
        for extension in self.extensions:
            extension.post_scale(model)

    def num_units(self, application: str, model: str = "default") -> int:
        self.logger.info(f"Getting the number of units for {application}.")
        return self.backend.num_units(model, application)

    @staticmethod
    def _waiting_timeout_log(timeout: timedelta | None) -> str:
        if timeout is not None:
            return f"Waiting {timeout}"
        else:
            return "Waiting"

    # Wait for the Juju model to become idle
    def idle_for_period(
        self,
        model: str = "default",
        timeout: timedelta | None = None,
        count: int = 30,
        strict_timeout: bool = False,
    ) -> None:
        self.logger.info(f"{self._waiting_timeout_log(timeout)} to be idle.")
        self.backend.wait_idle(model=model, timeout=timeout, count=count, strict_timeout=strict_timeout)

    def print_status(self, model: str = "default") -> None:
        separator = "-" * 80
        self.logger.info(f"Juju Status:\n{separator}\n{self.backend.juju_status_text(model)}{separator}")

    def integrate(
        self,
        application_1: str,
        application_2: str,
        endpoint_1: str,
        endpoint_2: str,
        model: str = "default",
    ) -> None:
        # Get targets
        target_1 = JujuIntegrationApplication(application_1, endpoint_1)
        target_2 = JujuIntegrationApplication(application_2, endpoint_2)

        # Integrate
        self.logger.info(f"Integrating {target_1} with {target_2}.")
        self.backend.integrate(model, target_1, target_2)

    def remove_integration(
        self,
        application_1: str,
        application_2: str,
        endpoint_1: str,
        endpoint_2: str,
        model: str = "default",
    ) -> None:
        # Get targets
        target_1 = JujuIntegrationApplication(application_1, endpoint_1)
        target_2 = JujuIntegrationApplication(application_2, endpoint_2)

        # Remove integration
        self.logger.info(f"Removing integration between {target_1} and {target_2}.")
        self.backend.remove_integration(model, target_1, target_2)

    def deploy_bundle_file(
        self,
        bundle: str,
        model: str = "default",
    ) -> None:
        self.logger.info(f"Deploying bundle file: '{bundle}'")
        self.backend.deploy_bundle_file(model, bundle)

        # Call extensions
        for extension in self.extensions:
            extension.post_deploy(model)

    def remove_applications(self, *applications: str, model: str = "default") -> None:
        # Call extensions
        for extension in self.extensions:
            extension.pre_remove(model, *applications)

        self.logger.info(f"Removing applications: {', '.join(applications)}.")
        self.backend.remove_applications(model, *applications)

    def wait_for_removal(self, *applications: str, model: str = "default", timeout: timedelta | None = None) -> None:
        self.logger.info(
            f"{self._waiting_timeout_log(timeout)} for removal of application(s) {', '.join(applications)}."
        )
        self.backend.wait_for_removal(model, list(applications), timeout)

    def wait_for_removal_of_integration(
        self,
        application_1: str,
        application_2: str,
        endpoint_1: str,
        endpoint_2: str,
        model: str = "default",
        timeout: timedelta | None = None,
    ) -> None:
        target_1 = JujuIntegrationApplication(application_1, endpoint_1)
        target_2 = JujuIntegrationApplication(application_2, endpoint_2)
        self.logger.info(
            f"{self._waiting_timeout_log(timeout)} for removal of integration between {target_1} and {target_2}."
        )
        self.backend.wait_for_removal_of_integration(model, target_1, target_2, timeout)

    def wait_for_removal_of_units(
        self, *applications: str, model: str = "default", timeout: timedelta | None = None
    ) -> None:
        self.logger.info(
            f"{self._waiting_timeout_log(timeout)} for removal of all units of application(s) {', '.join(applications)}."
        )
        self.backend.wait_for_removal_of_units(model, list(applications), timeout)

    def application_exists(self, application: str, model: str = "default") -> bool:
        self.logger.info(f"Checking that application exists: {application}.")
        return application in self.backend.list_applications(model)

    def integration_exists(
        self, application_1: str, endpoint_1: str, application_2: str, endpoint_2: str, model: str = "default"
    ) -> bool:
        self.logger.info(
            f"Checking that integration exists: {application_1}:{endpoint_1}/{application_2}:{endpoint_2}."
        )
        return self.backend.integration_exists(application_1, endpoint_1, application_2, endpoint_2, model)

    def list_applications(self, model: str = "default") -> dict[str, JujuApplicationInfo]:
        self.logger.info("Getting list of applications.")
        return self.backend.list_applications(model)

    def list_integrations(self, model: str = "default") -> set[JujuIntegration]:
        self.logger.info("Getting list of integrations.")
        return self.backend.list_integrations(model)

    def version(self, model: str = "default") -> str:
        self.logger.info("Collecting Juju version.")
        return self.backend.version(model)

    def validate_model(self, model: str = "default", level: str = "simple") -> None:
        """Validate all applications in the model.

        In Phase 2, this will trigger the Ops framework's native validation.
        In Phase 1, this calls the backend (no-op) then extensions (actual work).

        Args:
            model: Juju model name
            level: Validation level ("simple" or "deep", default: "simple")

        Raises:
            JujuValidationError: If any validation checks fail.
        """
        # Collect applications for validators
        applications = self.backend.list_applications(model)
        self.logger.info(f"Running validators on {len(applications)} applications (level={level})")

        # Run validators on each application
        failed_validations: dict[str, list[ValidationResult]] = {}
        for application in applications:
            results: dict[str, list[ValidationResult]] = {}

            # Phase 2: This will trigger Ops framework validation
            # Phase 1: This is a no-op, just a placeholder
            for unit, unit_results in self.backend.validate_application(model, application, level).items():
                results.setdefault(unit, []).extend(unit_results)

            # Call extensions (Phase 1 validation happens here)
            for extension in self.extensions:
                for unit, unit_results in extension.post_validate(model, application, level).items():
                    results.setdefault(unit, []).extend(unit_results)

            # Log results for this application
            for unit, unit_results in results.items():
                failed = [r for r in unit_results if r.status in ("FAIL", "ERROR")]
                if not failed:
                    self.logger.info(f"Validation passed for unit '{unit}'.")
                else:
                    for result in failed:
                        self.logger.error(
                            f"Validation failed for unit '{unit}' on endpoint '{result.endpoint}' "
                            f"(interface='{result.interface}', relation_id={result.relation_id}, status={result.status})."
                        )
                        for check in result.checks:
                            if not check.passed:
                                self.logger.error(f"  Check '{check.name}' failed: {check.message}")
                        if result.error:
                            self.logger.error(f"  Error: {result.error}")
                    failed_validations.setdefault(unit, []).extend(failed)

        # Raise exception for any failed validation results
        if sum(len(results) for results in failed_validations.values()) > 0:
            raise JujuValidationError(failed_validations)
