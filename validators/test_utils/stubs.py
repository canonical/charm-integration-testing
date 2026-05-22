from dataclasses import dataclass
from validators.base import ValidationRole

@dataclass
class RelationRoleStub:
    """Stub for relation role, commonly used across validator tests."""
    name: ValidationRole
