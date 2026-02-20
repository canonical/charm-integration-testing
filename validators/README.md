# Validators Directory (Phase 1)

This directory contains interface validators for Phase 1 testing of the Interface Validators Framework (SQ096).

## Structure

```
validators/
├── base/                          # BaseValidator abstract class
│   ├── __init__.py
│   └── validator.py               # Base class + TypedDict definitions
│
└── postgresql_client/             # PostgreSQL validator
    ├── __init__.py
    ├── validator.py               # PostgreSQLClientValidator
    └── pyproject.toml             # Dependencies
```

## Purpose

These validators are used by the `ValidatorInjectorExtension` during Phase 1 testing:
1. Extension copies validator packages to charm units
2. Extension executes validators on units with relation data
3. Validators return structured results (ValidationResult)

## Phase Timeline

### Phase 1 (Current)
- Validators live in this repository
- Used by ValidatorInjectorExtension for testing
- Executed on charm units via injection

### Phase 2 (Production)
- Published to PyPI as separate packages:
  - `charmlibs-validators-base`
  - `charmlibs-validators-postgresql-client`
- Ops framework discovers and runs automatically
- No injection needed!

## Base Validator

The `BaseValidator` abstract class enforces a consistent API:

```python
from validators.base import BaseValidator, ValidationCheck
from typing import List

class MyValidator(BaseValidator):
    interface_name = "my_interface"
    
    def _validate_schema(self, relation_data: dict):
        # Validate using charmlibs schema
        pass
    
    def _validate_l1(self) -> List[ValidationCheck]:
        # Simple checks: connectivity, auth
        pass
    
    def _validate_l2(self) -> List[ValidationCheck]:
        # Deep checks: L1 + writes with cleanup
        pass
```

## PostgreSQL Validator

Example implementation for the `postgresql_client` interface:

```python
from validators.postgresql_client import PostgreSQLClientValidator

# Initialize with relation data
validator = PostgreSQLClientValidator(relation_data={
    "endpoints": "10.1.1.5:5432",
    "username": "myuser",
    "password": "mypass",
    "database": "mydb"
})

# Run validation
result = validator.validate_integration(level="simple")
# Returns: { status: "PASS", checks: [...], ... }
```

## Dependencies

The PostgreSQL validator requires:
- `psycopg2-binary>=2.9.0` - PostgreSQL client library
- `charmlibs-interfaces-postgresql-client>=1.0` - Schema definitions

## Validation Levels

- **L1 (simple)**: Connectivity, authentication, read-only queries (<5s)
- **L2 (deep)**: L1 + canary writes, read verification, cleanup (<60s)

## Adding New Validators

To add a validator for a new interface:

1. Create directory: `validators/<interface_name>/`
2. Create `validator.py`:
   ```python
   from validators.base import BaseValidator, ValidationCheck
   from charmlibs.interfaces.<interface>.v0 import schema
   from typing import List
   
   class MyInterfaceValidator(BaseValidator):
       interface_name = "<interface_name>"
       
       def _validate_schema(self, relation_data: dict):
           return schema.MyProviderData.parse_obj(relation_data)
       
       def _validate_l1(self) -> List[ValidationCheck]:
           # Implementation
           pass
       
       def _validate_l2(self) -> List[ValidationCheck]:
           # Implementation
           pass
   ```

3. Create `pyproject.toml` with dependencies
4. Create `__init__.py` to export validator class
5. Update `ValidatorInjectorExtension` to discover your validator

## See Also

- [SQ096 Specification](../SQ096-v3.md): Complete framework design
- [ValidatorInjectorExtension](../charm_integration_testing/extensions/validator_injection/): Phase 1 injection tool
- [Juju Extensions](../charm_integration_testing/juju/extension.py): Extension system
