# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta
from pathlib import Path

from validators.base import ValidationResult

from .backend import JujuBackend
from .extension import JujuExtension
from .models import (
    JujuApplicationInfo,
    JujuConsumedOfferInfo,
    JujuIntegration,
    JujuIntegrationApplication,
    JujuResolvedIntegration,
)
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

    def find_integration_location(
        self,
        target_model: str,
        target_application: str,
        target_endpoint: str,
        neighbor_application: str,
        neighbor_endpoint: str,
        neighbor_model: str | None = None,
    ) -> JujuResolvedIntegration:
        """Resolve where a CMR integration lives and what SAAS alias to use.

        For same-model integrations this is a pass-through. For CMR tests it determines which
        model holds the consuming side and substitutes the local SAAS alias for the remote
        application name so that Juju commands succeed.

        Resolution order:
        1. ``list_integrations(target_model)`` — if the live integration is visible in the
           target model, extract the actual application names (one side may be a SAAS alias).
        2. ``list_consumed_offers(target_model)`` — if any SAAS entry in the target model
           exposes ``neighbor_endpoint``, the target is the consuming side.  Used when the
           integration has already been removed and step 1 finds nothing.
        3. Same as 1–2 on ``neighbor_model`` with roles swapped (neighbor-as-consumer).
        4. Fallback: treat as a same-model integration and use names as provided.
        """
        # Step 1: look for a live integration in the target model.
        for integration in self.backend.list_integrations(target_model):
            sides = {integration.provider, integration.requirer}
            target_side = next((s for s in sides if s.application == target_application and s.endpoint == target_endpoint), None)
            neighbor_side = next((s for s in sides if s.endpoint == neighbor_endpoint and s.application != target_application), None)
            if target_side is not None and neighbor_side is not None:
                return JujuResolvedIntegration(
                    model=target_model,
                    endpoint_1=target_side,
                    endpoint_2=neighbor_side,
                )

        # Step 2: the integration may be gone already (post-remove restore path).
        # Check consumed offers in the target model for a SAAS that exposes neighbor_endpoint.
        consumed_in_target = self.backend.list_consumed_offers(target_model)
        for saas_name, offer_info in consumed_in_target.items():
            if neighbor_endpoint in offer_info.endpoints:
                return JujuResolvedIntegration(
                    model=target_model,
                    endpoint_1=JujuIntegrationApplication(target_application, target_endpoint),
                    endpoint_2=JujuIntegrationApplication(saas_name, neighbor_endpoint),
                )

        # Steps 3a/3b: check the neighbor model (neighbor-as-consumer case).
        if neighbor_model is not None:
            # Step 3a: live integration visible in the neighbor model.
            for integration in self.backend.list_integrations(neighbor_model):
                sides = {integration.provider, integration.requirer}
                neighbor_side = next((s for s in sides if s.application == neighbor_application and s.endpoint == neighbor_endpoint), None)
                target_side = next((s for s in sides if s.endpoint == target_endpoint and s.application != neighbor_application), None)
                if neighbor_side is not None and target_side is not None:
                    return JujuResolvedIntegration(
                        model=neighbor_model,
                        endpoint_1=target_side,
                        endpoint_2=neighbor_side,
                    )

            # Step 3b: consumed offers in the neighbor model expose target_endpoint.
            consumed_in_neighbor = self.backend.list_consumed_offers(neighbor_model)
            for saas_name, offer_info in consumed_in_neighbor.items():
                if target_endpoint in offer_info.endpoints:
                    return JujuResolvedIntegration(
                        model=neighbor_model,
                        endpoint_1=JujuIntegrationApplication(saas_name, target_endpoint),
                        endpoint_2=JujuIntegrationApplication(neighbor_application, neighbor_endpoint),
                    )

        # Step 4: same-model (or no CMR context available) — use as provided.
        return JujuResolvedIntegration(
            model=target_model,
            endpoint_1=JujuIntegrationApplication(target_application, target_endpoint),
            endpoint_2=JujuIntegrationApplication(neighbor_application, neighbor_endpoint),
        )

    def integrate(
        self,
        application_1: str,
        application_2: str,
        endpoint_1: str,
        endpoint_2: str,
        model: str = "default",
        neighbor_model: str | None = None,
    ) -> None:
        resolved = self.find_integration_location(
            target_model=model,
            target_application=application_1,
            target_endpoint=endpoint_1,
            neighbor_application=application_2,
            neighbor_endpoint=endpoint_2,
            neighbor_model=neighbor_model,
        )
        self.logger.info(f"Integrating {resolved.endpoint_1} with {resolved.endpoint_2}.")
        self.backend.integrate(resolved.model, resolved.endpoint_1, resolved.endpoint_2)

    def remove_integration(
        self,
        application_1: str,
        application_2: str,
        endpoint_1: str,
        endpoint_2: str,
        model: str = "default",
        neighbor_model: str | None = None,
    ) -> None:
        resolved = self.find_integration_location(
            target_model=model,
            target_application=application_1,
            target_endpoint=endpoint_1,
            neighbor_application=application_2,
            neighbor_endpoint=endpoint_2,
            neighbor_model=neighbor_model,
        )
        self.logger.info(f"Removing integration between {resolved.endpoint_1} and {resolved.endpoint_2}.")
        self.backend.remove_integration(resolved.model, resolved.endpoint_1, resolved.endpoint_2)

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
        neighbor_model: str | None = None,
    ) -> None:
        resolved = self.find_integration_location(
            target_model=model,
            target_application=application_1,
            target_endpoint=endpoint_1,
            neighbor_application=application_2,
            neighbor_endpoint=endpoint_2,
            neighbor_model=neighbor_model,
        )
        self.logger.info(
            f"{self._waiting_timeout_log(timeout)} for removal of integration between "
            f"{resolved.endpoint_1} and {resolved.endpoint_2}."
        )
        self.backend.wait_for_removal_of_integration(resolved.model, resolved.endpoint_1, resolved.endpoint_2, timeout)

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
