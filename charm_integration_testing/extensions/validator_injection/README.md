# Validator Injection Extension (Phase 1)

This extension provides Phase 1 testing infrastructure for the Interface Validators Framework (SQ096).

## Overview

The `ValidatorInjectorExtension` is a `JujuExtension` that hooks into the validation lifecycle to inject and execute validators on charm units. This simulates what the Ops framework will do automatically in Phase 2.

## Architecture

```
Test Code
    │
    └─> juju_client.validate_model(model, level="simple")
            │
            ├─> For each application:
            │   │
            │   ├─> backend.validate_application(model, app, level)
            │   │   └─> Phase 2: Triggers Ops framework (not implemented yet)
            │   │   └─> Phase 1: No-op placeholder
            │   │
            │   └─> extension.post_validate(model, app, level)
            │       └─> ValidatorInjectorExtension (Phase 1 actual work):
            │           ├─> Discover metadata & relations
            │           ├─> For each relation:
            │           │   ├─> Copy validator to unit
            │           │   ├─> Fetch relation data
            │           │   └─> Execute validator on unit
            │           ├─> Aggregate results
            │           └─> Raise ValidationFailureError on failure
```

## Usage

### Register the Extension

```python
import logging
from charm_integration_testing.juju_cmd import JujuCmdBackend
from charm_integration_testing.juju import JujuClient
from charm_integration_testing.extensions.validator_injection import ValidatorInjectorExtension

# Create logger
logger = logging.getLogger(__name__)

# Create backend
backend = JujuCmdBackend(logger=logger)

# Create validator extension
validator_extension = ValidatorInjectorExtension(backend=backend, logger=logger)

# Create client with extension
juju_client = JujuClient(
    backend=backend,
    logger=logger,
    extensions=[validator_extension]  # Register extension
)
```

### Validate Model

```python
from datetime import timedelta

def test_deploy(juju_client, model):
    # Deploy
    juju_client.deploy_bundle_file("bundle.yaml", model=model)
    
    # Wait for idle
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))
    
    # Validate all applications
    juju_client.validate_model(model=model, level="simple")
    # ^ Automatically validates each application
    # ^ Raises ValidationFailureError if any checks fail
```

### Handle Validation Failures

```python
from charm_integration_testing.extensions.validator_injection import ValidationFailureError

try:
    juju_client.validate_model(model="prod", level="deep")
except ValidationFailureError as e:
    # e.result contains validation details for the failed application
    print(f"Validation failed: {e}")
    print(f"Model: {e.result.model}")
    print(f"Overall status: {e.result.overall_status}")
    
    for app_name, app_result in e.result.applications.items():
        print(f"\nApplication: {app_name}")
        print(f"  Status: {app_result.overall_status}")
        
        for relation_name, relation_result in app_result.relations.items():
            print(f"  Relation: {relation_name}")
            print(f"    Interface: {relation_result.interface}")
            print(f"    Status: {relation_result.status}")
            
            for check in relation_result.checks:
                print(f"      - {check.name}: {check.status}")
                if check.status != "PASS":
                    print(f"        {check.message}")
```

## Lifecycle

The extension participates in the validation lifecycle:

1. **`validate_model()`** called on `JujuClient`
2. For each application:
   - **`backend.validate_application(model, app, level)`** (Phase 2 placeholder)
   - **`extension.post_validate(model, app, level)`** (Phase 1 actual work)
3. If any application validation fails, `ValidationFailureError` is raised

## Implementation Status

### ✅ Completed
- Extension architecture
- Lifecycle integration with `post_validate(model, application, level)`
- Data models (ValidationResult, ApplicationValidationResult, etc.)
- Error handling (ValidationFailureError)
- Per-application validation flow

### 🚧 TODO (Implementation Details)
- `_validate_application()`: Discover metadata, iterate relations
- `_copy_validator_to_unit()`: SCP validator packages to unit
- `_fetch_relation_data()`: Get relation databags from Juju
- `_execute_validator()`: Run validator script on unit, parse JSON
- `_discover_metadata()`: Parse metadata.yaml from charm
- `_determine_interface_for_relation()`: Map relation → interface

## Phase Transition

### Phase 1 (Current)
- `ValidatorInjectorExtension` does all validation work
- Called via `post_validate(model, application, level)` for each app
- Copies validators to units, executes remotely
- Raises `ValidationFailureError` on failure

### Phase 2 (Future)
- `backend.validate_application()` triggers Ops framework
- Framework handles validation automatically
- `ValidatorInjectorExtension` DELETED (no longer needed)
- No test code changes required!

## See Also

- [SQ096 Specification](../../../../SQ096-v3.md): Complete framework design
- [Base Validator](../../../../charm_validators/base/): BaseValidator abstract class
- [PostgreSQL Validator](../../../../charm_validators/postgresql_client/): Example validator
- [Models](./models.py): Data models and exceptions

