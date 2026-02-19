# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""
Validator injection extension for Phase 1 testing.

This module provides a JujuExtension that injects and executes validators
on charm units during the post_validate lifecycle hook. This simulates what
the Ops framework will do automatically in Phase 2.

Architecture:
- Extends JujuExtension with post_validate() hook
- Discovers relations from charm metadata for a single application
- Imports appropriate validator based on interface
- Copies validator package to charm unit
- Executes validator on unit with relation data
- Fetches and parses results
- Raises ValidationFailureError on failure

This code will be DELETED in Phase 2 when the Ops framework provides native support.
"""

import logging

from juju.backend import JujuBackend
from juju.extension import JujuExtension

from .models import (
    ApplicationValidationResult,
    ModelValidationResult,
    ValidationFailureError,
    ValidationResult,
)


class ValidatorInjectorExtension(JujuExtension):
    """Extension that injects and executes validators on charm units.

    This extension hooks into the validate lifecycle and performs Phase 1
    validation by injecting validators directly onto charm units.

    In Phase 2, this extension will be removed and validation will happen
    automatically within the Ops framework.
    """

    def __init__(self, backend: JujuBackend, logger: logging.Logger):
        """Initialize the validator injector extension.

        Args:
            backend: Juju backend for executing operations
            logger: Logger instance
        """
        self.backend = backend
        self.logger = logger

    def post_validate(self, model: str, application: str, level: str) -> None:
        """Hook called after backend.validate_application() for each application.

        This is where Phase 1 validation actually happens:
        1. Discover metadata and relations for this application
        2. For each relation, inject and execute validator
        3. Aggregate results
        4. Raise exception if validation fails

        Args:
            model: Juju model name
            application: Application name to validate
            level: Validation level ("simple" or "deep")

        Raises:
            ValidationFailureError: If application validation fails
        """
        self.logger.info(f"[ValidatorInjector] Validating application '{application}' (level={level})")

        try:
            # Validate this application
            app_result = self._validate_application(model, application, level)

            # Check if validation failed
            if app_result.overall_status in ("FAIL", "ERROR"):
                # Create a model result with just this application
                model_result = ModelValidationResult(
                    model=model,
                    applications={application: app_result},
                    overall_status=app_result.overall_status,  # type: ignore
                )
                raise ValidationFailureError(model_result)

            self.logger.info(f"[ValidatorInjector] Application '{application}' validation PASSED")

        except ValidationFailureError:
            # Re-raise validation failures
            raise
        except Exception as e:
            self.logger.error(f"[ValidatorInjector] Failed to validate application '{application}': {e}")
            raise

    def _validate_application(self, model: str, application: str, level: str) -> ApplicationValidationResult:
        """Validate all relations for an application.

        This method:
        1. Discovers the application's metadata and relations
        2. For each relation, determines the interface
        3. Injects the appropriate validator to a unit
        4. Executes validation on the unit
        5. Aggregates results

        Args:
            model: Juju model name
            application: Application name to validate
            level: Validation level ("simple" or "deep")

        Returns:
            ApplicationValidationResult with all relation validation results

        Raises:
            NotImplementedError: Implementation pending
        """
        self.logger.info(f"[ValidatorInjector] Validating application '{application}' (level={level})")

        # TODO: Implementation
        # 1. Get application units: self.backend.application_units(model, application)
        # 2. Pick a unit (e.g., leader or first unit)
        # 3. Fetch metadata.yaml from unit: self.backend.scp(...)
        # 4. Parse metadata to discover relations and interfaces
        # 5. For each relation:
        #    a. Determine interface name from metadata
        #    b. Check if validator exists for interface
        #    c. Copy validator package to unit: self._copy_validator_to_unit(...)
        #    d. Fetch relation data: self._fetch_relation_data(...)
        #    e. Execute validator on unit: self._execute_validator(...)
        #    f. Parse results
        # 6. Aggregate results into ApplicationValidationResult
        # 7. Determine overall status (FAIL if any relation failed)

        raise NotImplementedError("_validate_application implementation pending")

    def _copy_validator_to_unit(self, model: str, unit: str, interface: str) -> None:
        """Copy validator package to charm unit.

        Args:
            model: Juju model name
            unit: Unit name (e.g., "postgresql/0")
            interface: Interface name (e.g., "postgresql_client")

        Raises:
            NotImplementedError: Implementation pending
        """
        self.logger.debug(f"Copying validator for interface '{interface}' to unit {unit}")

        # TODO: Implementation
        # 1. Locate validator package in charm_validators/{interface}/
        # 2. Create temporary directory on unit
        # 3. Copy validator files: self.backend.scp(model, local_path, f"{unit}:/tmp/validator/")
        # 4. Copy base validator module as well
        # 5. Install dependencies on unit (psycopg2, etc.)

        raise NotImplementedError("_copy_validator_to_unit implementation pending")

    def _fetch_relation_data(self, model: str, unit: str, relation_name: str) -> dict:
        """Fetch relation databag from unit.

        Args:
            model: Juju model name
            unit: Unit name
            relation_name: Relation name from metadata

        Returns:
            Dictionary of relation data

        Raises:
            NotImplementedError: Implementation pending
        """
        self.logger.debug(f"Fetching relation data for '{relation_name}' from unit {unit}")

        # TODO: Implementation
        # Option 1: Use juju show-unit and parse relation-info
        # Option 2: Execute code on unit to read relation data:
        #   self.backend.exec_unit(model, unit, "relation-get -r <relation-id> - <remote-unit>")
        # Option 3: Use ops library to read relation data

        raise NotImplementedError("_fetch_relation_data implementation pending")

    def _execute_validator(
        self, model: str, unit: str, interface: str, relation_data: dict, level: str
    ) -> ValidationResult:
        """Execute validator on unit with relation data.

        Args:
            model: Juju model name
            unit: Unit name
            interface: Interface name
            relation_data: Relation databag
            level: Validation level

        Returns:
            ValidationResult from validator execution

        Raises:
            NotImplementedError: Implementation pending
        """
        self.logger.debug(f"Executing validator for interface '{interface}' on unit {unit}")

        # TODO: Implementation
        # 1. Create Python script on unit that:
        #    - Imports the validator
        #    - Instantiates with relation_data
        #    - Calls validate_integration(level)
        #    - Prints JSON result
        # 2. Execute script: self.backend.exec_unit(model, unit, "python3 /tmp/validator/run.py")
        # 3. Parse JSON output into ValidationResult
        # 4. Handle errors (import failures, validator exceptions)

        raise NotImplementedError("_execute_validator implementation pending")

    def _discover_metadata(self, model: str, unit: str) -> dict:
        """Fetch and parse metadata.yaml from charm unit.

        Args:
            model: Juju model name
            unit: Unit name

        Returns:
            Parsed metadata dictionary

        Raises:
            NotImplementedError: Implementation pending
        """
        self.logger.debug(f"Discovering metadata from unit {unit}")

        # TODO: Implementation
        # Option 1: Copy metadata.yaml from unit
        #   self.backend.scp(model, f"{unit}:/var/lib/juju/agents/unit-*/charm/metadata.yaml", local_path)
        # Option 2: Use juju status/charm info APIs
        # Option 3: Execute code on unit to read and print metadata

        raise NotImplementedError("_discover_metadata implementation pending")

    def _determine_interface_for_relation(self, model: str, application: str, relation_name: str) -> str:
        """Determine the interface name for a relation.

        Args:
            model: Juju model name
            application: Application name
            relation_name: Relation name

        Returns:
            Interface name (e.g., "postgresql_client")

        Raises:
            NotImplementedError: Implementation pending
        """
        # TODO: Implementation
        # Use self.backend.list_integrations(model) to find the integration
        # Extract the interface name from JujuIntegration

        raise NotImplementedError("_determine_interface_for_relation implementation pending")
