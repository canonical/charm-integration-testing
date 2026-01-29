# Copyright (C) 2025 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Scriptlet invoker for bundle validation.

This module provides a simple event system for validating bundle topologies with scriptlets.
Scriptlets register a handler for the "validate" event, which fires with complete bundle state.

The event object provides:
- event.relations: Dict mapping endpoint names to lists of related application names
- event.reject(field, reason): Method to reject with error code and details

Example:
    >>> scriptlet_code = load_scriptlet('wordpress-k8s')
    >>> invoker = ScriptletInvoker(scriptlet_code)
    >>> relations = {'db': ['mysql-k8s']}
    >>> rejection = invoker.fire_validate_event(relations)
    >>> if rejection and rejection.is_error_code:
    ...     constraint = parse_error_code_rejection(rejection)
    ...     print(f"Constraint type: {constraint.constraint_type}")
"""

import logging
from typing import Any, Callable

from pydantic import BaseModel

__all__ = [
    "ScriptletInvoker",
    "Rejection",
    "ParsedConstraint",
    "parse_error_code_rejection",
    "KNOWN_ERROR_CODES",
]


KNOWN_ERROR_CODES = frozenset({"required", "mutual_exclusion", "limit", "conditional", "data_validation"})


class Rejection(BaseModel):
    """Represents a scriptlet rejection with error code encoding."""

    field: str  # Error code (e.g., 'mutual_exclusion', 'required', 'limit') or legacy endpoint name
    reason: str | list[str]  # Details about the rejection

    @property
    def is_error_code(self) -> bool:
        """Check if this rejection uses error code encoding."""
        return self.field in KNOWN_ERROR_CODES


class ParsedConstraint(BaseModel):
    """Structured constraint data parsed from an error code rejection."""

    constraint_type: str
    details: str | list[str]

    # Constraint-specific fields (populated based on constraint_type)
    conflicting_endpoints: list[str] | None = None
    required_endpoint: str | None = None
    acceptable_endpoints: list[str] | None = None
    endpoint: str | None = None
    max: int | None = None
    current: int | None = None
    validation_issues: list[str] | None = None


class ValidateEvent:
    """
    Simple validation event for bundle topology validation.

    Attributes:
        relations: Dict mapping endpoint names to lists of related application names
        rejected: Whether the event was rejected by a scriptlet
        rejection: The Rejection object if rejected, None otherwise
    """

    def __init__(self, relations: dict[str, list[str]]) -> None:
        """
        Initialize the validation event.

        Args:
            relations: Dict mapping endpoint names to lists of related application names
        """
        self.relations = relations
        self._rejected = False
        self._rejection: Rejection | None = None

    def reject(self, field: str, reason: str | list[str] | None = None) -> None:
        """
        Record a rejection from the scriptlet.

        Supports both error code encoding and legacy formats:
        - event.reject('error_code', 'details') - error code with string details
        - event.reject('error_code', ['detail1', 'detail2']) - error code with list
        - event.reject('single message') - legacy single string format

        Args:
            field: Error code (e.g., 'mutual_exclusion', 'required') or legacy message
            reason: Details about the rejection (string or list), or None for legacy format
        """
        if reason is None:
            # Single string format: event.reject('message')
            self._rejection = Rejection(field="", reason=field)
        else:
            # Field + reason format
            self._rejection = Rejection(field=field, reason=reason)
        self._rejected = True

    @property
    def rejected(self) -> bool:
        """Return True if the event was rejected."""
        return self._rejected

    @property
    def rejection(self) -> Rejection | None:
        """Return the Rejection object if rejected, None otherwise."""
        return self._rejection


class _ScriptletObserverRegistry:
    """Registry for scriptlet event observers (simulates juju.observe API)."""

    def __init__(self) -> None:
        self._observers: dict[str, list[Callable]] = {}

    def observe(self, event_name: str, handler: Callable) -> None:
        """Register an event handler for the given event name."""
        if event_name not in self._observers:
            self._observers[event_name] = []
        self._observers[event_name].append(handler)

    def fire_event(self, event_name: str, event: ValidateEvent) -> ValidateEvent:
        """Fire an event and invoke all registered handlers."""
        if event_name in self._observers:
            for handler in self._observers[event_name]:
                handler(event)
                if event.rejected:
                    return event
        return event


class ScriptletInvoker:
    """
    Invokes scriptlets for bundle topology validation.

    Executes scriptlet code and fires validation events with bundle state.
    Scriptlets register handlers via juju.observe("validate", handler_func).

    Attributes:
        scriptlet_code: The Python source code of the scriptlet
        logger: Logger instance for debugging
    """

    def __init__(self, scriptlet_code: str, logger: logging.Logger | None = None):
        """
        Initialize the scriptlet invoker.

        Args:
            scriptlet_code: Python code defining the scriptlet (with init() and event handlers)
            logger: Optional logger instance. If not provided, creates a default logger.
        """
        self.scriptlet_code = scriptlet_code
        self.logger = logger or logging.getLogger(__name__)
        self._juju_registry = _ScriptletObserverRegistry()
        self._initialized = False

    def _initialize(self) -> None:
        """Execute the scriptlet's init() function to register observers."""
        if self._initialized:
            return

        self.logger.debug("Initializing scriptlet")

        # Create a namespace with the juju registry
        namespace = {
            "juju": self._juju_registry,
        }

        try:
            # Execute the scriptlet code to define functions
            exec(self.scriptlet_code, namespace)

            # Call init() to register observers
            if "init" in namespace:
                namespace["init"]()
            else:
                self.logger.warning("Scriptlet does not define init() function")

            self._initialized = True
            self.logger.debug("Scriptlet initialized successfully")

        except Exception as e:
            self.logger.error(f"Failed to initialize scriptlet: {e}")
            raise RuntimeError(f"Scriptlet initialization failed: {e}") from e

    def fire_validate_event(self, relations: dict[str, list[str]]) -> Rejection | None:
        """
        Fire a validate event with the given bundle topology state.

        Args:
            relations: Dict mapping endpoint names to lists of related application names
                      Example: {'db': ['mysql-k8s', 'postgresql-k8s'], 'cache': ['redis-k8s']}

        Returns:
            Rejection object if scriptlet rejected, None otherwise

        Raises:
            RuntimeError: If scriptlet execution fails
        """
        self._initialize()

        self.logger.debug(f"Firing validate event with relations: {relations}")

        # Create validate event with complete bundle state
        event = ValidateEvent(relations)

        # Fire the validate event through the registry
        self._juju_registry.fire_event("validate", event)

        result = event.rejection
        if result:
            self.logger.debug(f"Validate event rejected: {result.field}")
        else:
            self.logger.debug("Validate event accepted")

        return result

def parse_error_code_rejection(rejection: Rejection) -> ParsedConstraint | None:
    """
    Parse a rejection with error code encoding into structured constraint data.

    Extracts constraint-specific information from the rejection's details field
    based on the constraint type. Returns None for legacy (non-error-code) rejections.

    Supported constraint types:
    - required: Single required endpoint
    - mutual_exclusion: List of mutually exclusive endpoints
    - limit: Endpoint with maximum relation count (format: "endpoint:max")
    - conditional: List of endpoints where at least one is required
    - data_validation: List of validation issues with prefixes (e.g., "missing:field")

    Args:
        rejection: The Rejection object from a scriptlet

    Returns:
        ParsedConstraint object if error code format, None for legacy format

    Raises:
        ValueError: If constraint details are malformed for the constraint type

    Example:
        >>> rejection = Rejection(field='mutual_exclusion', reason=['db', 'database'])
        >>> constraint = parse_error_code_rejection(rejection)
        >>> constraint.constraint_type
        'mutual_exclusion'
        >>> constraint.conflicting_endpoints
        ['db', 'database']
    """
    if not rejection.is_error_code:
        return None

    error_code = rejection.field
    details = rejection.reason

    constraint_data: dict[str, Any] = {"constraint_type": error_code, "details": details}

    try:
        # Parse structured details for specific constraint types
        if error_code == "mutual_exclusion":
            if isinstance(details, list):
                constraint_data["conflicting_endpoints"] = details
            else:
                raise ValueError(f"mutual_exclusion expects list, got {type(details).__name__}")

        elif error_code == "required":
            constraint_data["required_endpoint"] = details if isinstance(details, str) else details[0]

        elif error_code == "limit":
            if isinstance(details, str) and ":" in details:
                endpoint, max_val = details.split(":", 1)
                constraint_data["endpoint"] = endpoint
                constraint_data["max"] = int(max_val)
            elif isinstance(details, list) and details:
                constraint_data["endpoint"] = details[0]
                for item in details[1:]:
                    if isinstance(item, str) and ":" in item:
                        key, value = item.split(":", 1)
                        if key == "max":
                            constraint_data["max"] = int(value)
                        elif key == "current":
                            constraint_data["current"] = int(value)
            else:
                raise ValueError(f"limit expects 'endpoint:max' string or list, got {details}")

        elif error_code == "conditional":
            if isinstance(details, list):
                constraint_data["acceptable_endpoints"] = details
            else:
                raise ValueError(f"conditional expects list, got {type(details).__name__}")

        elif error_code == "data_validation":
            if isinstance(details, list):
                constraint_data["validation_issues"] = details
            else:
                raise ValueError(f"data_validation expects list, got {type(details).__name__}")

        return ParsedConstraint(**constraint_data)

    except (ValueError, IndexError, KeyError) as e:
        # Log the error but return a basic ParsedConstraint
        logging.getLogger(__name__).warning(
            f"Failed to parse {error_code} constraint details: {e}. Returning basic constraint."
        )
        return ParsedConstraint(constraint_type=error_code, details=details)
