# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import hmac
import json
import secrets

_IDENTITY_KEY = secrets.token_bytes(32)


def provider_identity(*values: str) -> str:
    """Deduplicate effective configurations without exposing secrets in this process."""
    serialized = json.dumps(values, separators=(",", ":"))
    return hmac.new(_IDENTITY_KEY, serialized.encode(), hashlib.sha256).hexdigest()


def bucket_owner_identity(*values: str) -> str:
    """Return a stable opaque ownership marker from non-secret provider attributes."""
    serialized = json.dumps(values, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()[:32]
