# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from validators.base import ValidationResult

from .backend import JujuBackend
from .extension import JujuExtension
from .models import JujuApplicationInfo, JujuConsumedOfferInfo, JujuIntegration, JujuIntegrationApplication
from .version import JujuVersion


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

    def __init__(
        self,
        backend: JujuBackend,
        logger: logging.Logger,
        extensions: list[JujuExtension] | None = None,
    ):
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
        info = f"Juju status for model '{model}'" if model != "default" else "Juju status"
        self.logger.info(f"{info}:\n{separator}\n{self.backend.juju_status_text(model)}{separator}")

    def debug_log(self, model: str = "default") -> str:
        """Retrieve the Juju debug log for the model.

        Args:
            model: Juju model name

        Returns:
            Debug log content as a string
        """
        self.logger.info(f"Collecting debug log from model {model}")
        return self.backend.debug_log(model)

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
        self.backend.deploy_bundle_file(model, bundle, trust=True, force=True)

        # Call extensions
        for extension in self.extensions:
            extension.post_deploy(model)

    def refresh_application(
        self,
        application: str,
        revision: int | None = None,
        channel: str | None = None,
        model: str = "default",
    ) -> None:
        options: list[str] = []
        if revision is not None:
            options.append(f"revision={revision}")
        if channel:
            options.append(f"channel={channel}")
        options_suffix = f" ({', '.join(options)})" if options else ""
        self.logger.info(f"Refreshing application {application}{options_suffix}.")
        self.backend.refresh_application(model, application, revision=revision, channel=channel)

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

    def wait_for_model_to_exist(self, model: str = "default", timeout: timedelta | None = None) -> None:
        self.logger.info(f"Waiting {self._waiting_timeout_log(timeout)} for model {model} to exist before continuing.")
        self.backend.wait_for_model_to_exist(model=model, timeout=timeout)

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

    def list_consumed_offers(self, model: str = "default") -> dict[str, JujuConsumedOfferInfo]:
        self.logger.info("Getting list of consumed offers.")
        return self.backend.list_consumed_offers(model)

    def application_revision(self, application: str, model: str = "default") -> int:
        self.logger.info(f"Getting charm revision for application '{application}'.")
        applications = self.backend.list_applications(model)
        if application not in applications:
            raise KeyError(f"Application '{application}' not found in model '{model}'")
        return applications[application].revision

    def wait_for_application_revision(
        self,
        application: str,
        expected_revision: int,
        timeout: timedelta | None,
        model: str = "default",
    ) -> None:
        self.logger.info(
            f"Waiting {timeout} for application '{application}' to reach charm revision {expected_revision}."
        )
        self.backend.wait_for_application_revision(application, expected_revision, timeout, model)

    def list_integrations(self, model: str = "default") -> set[JujuIntegration]:
        self.logger.info("Getting list of integrations.")
        return self.backend.list_integrations(model)

    def reboot_model_controller(self, model: str = "default") -> None:
        self.logger.info("Restarting model controller.")
        return self.backend.reboot_model_controller(model)

    def version(self, model: str = "default") -> JujuVersion:
        self.logger.info("Collecting Juju model version.")
        return self.backend.version(model)

    def cli_version(self) -> JujuVersion:
        self.logger.info("Collecting Juju CLI version.")
        return self.backend.cli_version()

    def upgrade_model(self, model: str, agent_version: str | None = None) -> None:
        version_suffix = f" to agent version '{agent_version}'" if agent_version else ""
        self.logger.info(f"Upgrading model '{model}'{version_suffix}.")
        self.backend.upgrade_model(model=model, agent_version=agent_version)

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

            if not results:
                self.logger.info(f"No validation results for application '{application}'.")
                continue

            # Log results for this application
            for unit, unit_results in results.items():
                if not unit_results:
                    self.logger.info(f"No validation results for unit '{unit}'.")
                    continue

                elif all(r.status == "SKIPPED" for r in unit_results):
                    self.logger.info(f"Validation skipped for unit '{unit}'.")
                    continue

                failed = [r for r in unit_results if r.status in ("FAIL", "ERROR")]
                if not failed:
                    self.logger.info(f"Validation passed for unit '{unit}' ({len(unit_results)} results)")
                    for result in unit_results:
                        self.logger.debug(
                            f"  endpoint '{result.endpoint}' (interface='{result.interface}', "
                            f"relation_id={result.relation_id}): {result.status}"
                        )
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

    def bootstrap_controller(
        self,
        cloud: str,
        controller: str,
        controller_constraints: dict[str, str],
        bootstrap_configuration: dict[str, str],
        agent_version: str | None = None,
        metadata_source: Path | None = None,
    ) -> None:
        version_suffix = f" at agent version '{agent_version}'" if agent_version else ""
        self.logger.info(
            f"Bootstrapping Juju controller in cloud '{cloud}' with name '{controller}'{version_suffix}, "
            f"using constraints '{controller_constraints}'."
        )
        self.backend.bootstrap_controller(
            cloud=cloud,
            controller=controller,
            controller_constraints=controller_constraints,
            agent_version=agent_version,
            bootstrap_configuration=bootstrap_configuration,
            metadata_source=metadata_source,
        )

        # Call extensions
        for extension in self.extensions:
            extension.post_bootstrap_controller(controller)

    def add_model(self, controller: str, model: str, model_config: dict[str, str]) -> None:
        self.logger.info(
            f"Creating model '{model}' with configuration '{model_config}' on controller '{controller}' and switching to it."
        )
        self.backend.add_model(controller=controller, model=model, model_config=model_config)
        self.backend.switch(controller=controller, model=model)

        # Call extensions
        for extension in self.extensions:
            extension.post_add_model(controller, model)

    def kill_controller(self, controller: str) -> None:
        self.logger.info(f"Killing controller '{controller}'.")

        # Call extensions
        for extension in self.extensions:
            extension.pre_kill_controller(controller)

        self.backend.kill_controller(controller=controller)

        # Call extensions
        for extension in self.extensions:
            extension.post_kill_controller(controller)

    def migrate_model(self, model_name: str, source_controller: str, target_controller: str) -> None:
        self.logger.info(
            f"Migrating model '{model_name}' from source controller '{source_controller}' to target controller '{target_controller}'"
        )
        self.backend.migrate_model(
            model_name=model_name, source_controller=source_controller, target_controller=target_controller
        )

        # Call extensions
        for extension in self.extensions:
            extension.post_migrate_model(model_name, source_controller, target_controller)

    def upgrade_controller(self, controller: str, agent_version: str | None = None) -> None:
        version_suffix = f" to agent version '{agent_version}'" if agent_version else ""
        self.logger.info(f"Upgrading controller '{controller}'{version_suffix}.")
        self.backend.upgrade_controller(controller=controller, agent_version=agent_version)

    def is_controller_kubernetes(self, controller: str) -> bool:
        """Check if a controller runs on Kubernetes."""
        try:
            controller_data = self.backend.show_controller(controller)
            cloud = controller_data.get(controller, {}).get("details", {}).get("cloud-name")
            if not cloud:
                return False
            clouds = self.backend.clouds()
            return bool(cast(dict[str, Any], clouds.get(cloud, {})).get("type") == "k8s")
        except Exception:
            return False

    def get_kubeconfig_for_controller(self, controller: str) -> Path | None:
        """Get kubeconfig path if controller is K8s-based."""
        if not self.is_controller_kubernetes(controller):
            return None
        kubeconfig = os.environ.get("KUBECONFIG", "").strip()
        return Path(kubeconfig) if kubeconfig else None
