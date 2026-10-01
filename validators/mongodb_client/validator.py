# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import os
import re
import tempfile
import time
import uuid
from typing import Any
from urllib.parse import quote_plus

from pymongo import MongoClient
from pymongo.database import Database

from validators.base import (
    BasePersistenceValidator,
    BaseValidator,
    PersistenceNotApplicable,
    PersistenceState,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

# Collection name prefix for persistence-validator canary collections. Kept as a module constant
# so cleanup() (which has no per-call state to work from) can discover every canary collection it
# may have created by pattern rather than by identifier.
_CANARY_COLLECTION_PREFIX = "validator_canary_"

# prepare() masks its identifier to 63 bits, so a genuine canary identifier never exceeds this
# value. cleanup()'s discovery regex only checks a candidate collection name's *shape* (prefix +
# 20 digits); this bound lets it also reject an out-of-range look-alike with the right shape that
# prepare() couldn't have produced.
_MAX_CANARY_IDENTIFIER = (1 << 63) - 1


class _MongoDBConnectionMixin:
    """Shared credential-resolution and connection helpers for mongodb_client validators.

    Both ``MongoDBClientValidator`` (health probe) and ``MongoDBClientPersistenceValidator``
    (durability probe) need to resolve the same relation credentials and open the same kind of
    pymongo client, so that logic lives here once instead of being duplicated.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ca_file_path: str | None = None

    def _resolve_credentials(self) -> dict[str, str]:
        """Resolve credentials from relation data/secrets."""
        return {
            **self.resolve_secret("secret-user", "username", "password"),  # type: ignore[attr-defined]
            **self.resolve_secret("secret-tls", "tls-ca"),  # type: ignore[attr-defined]
        }

    def _build_mongodb_client(self, creds: dict[str, str]) -> MongoClient[Any]:
        """Build and return MongoDB client with TLS/timeout config."""
        endpoint = self.databag["endpoints"].split(",")[0].strip()  # type: ignore[attr-defined]
        client_kwargs: dict[str, Any] = {
            "serverSelectionTimeoutMS": 5000,
            "connectTimeoutMS": 5000,
            "socketTimeoutMS": 10000,
            "appname": "mongodb-client-validator",
        }

        uri = f"mongodb://{quote_plus(creds['username'])}:{quote_plus(creds['password'])}@{endpoint}"
        if creds.get("tls-ca"):
            client_kwargs["tls"] = True
            self._create_temp_ca_file(creds["tls-ca"])
            client_kwargs["tlsCAFile"] = self.ca_file_path

        try:
            return MongoClient(uri, **client_kwargs)
        except Exception:
            # Ensure temporary CA file is removed if client construction fails.
            self._remove_temp_ca_file()
            raise

    def _cleanup_client(self, mongodb_client: MongoClient[Any] | None) -> None:
        """Clean up MongoDB client and temporary CA file."""
        if mongodb_client is not None:
            mongodb_client.close()

        self._remove_temp_ca_file()

    def _create_temp_ca_file(self, ca_content: str) -> None:
        """Create a temporary file with the given CA content and store path in self.ca_file_path."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".pem") as ca_file:
            ca_file.write(ca_content)
            self.ca_file_path = ca_file.name

    def _remove_temp_ca_file(self) -> None:
        """Remove the temporary CA file."""
        if self.ca_file_path and os.path.exists(self.ca_file_path):
            os.remove(self.ca_file_path)
            self.ca_file_path = None


class MongoDBClientValidator(_MongoDBConnectionMixin, BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)

        if level == "uat":
            return self._skipped_result_due_to_level(level)

        if level == "simple":
            return self._validate_simple()
        elif level == "deep":
            return self._validate_deep()
        else:
            return self._skipped_result_due_to_level(level)

    def _validate_simple(self) -> ValidationResult:
        """L1: Connectivity & Auth with read-only canary query."""
        checks: list[ValidationCheck] = []

        # --- 1. Remote app presence ---
        error_result = self._check_relation_exists("simple")
        if error_result:
            return error_result

        # --- 2. Resolve credentials (plain fields or Juju secrets) ---
        creds = self._resolve_credentials()

        # --- 3. Schema check ---
        schema = ["endpoints", "database", "username", "password"]
        schema_check = self.validate_schema(schema, creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="simple", checks=checks)

        # --- 4. Connect & ping ---
        endpoint = self.databag["endpoints"].split(",")[0].strip()
        try:
            mongodb_client = self._build_mongodb_client(creds)
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._make_result(level="simple", checks=checks)

        try:
            connect_check = self._attempt_connection(mongodb_client, endpoint)
            checks.append(connect_check)
            if not connect_check.passed:
                return self._make_result(level="simple", checks=checks)

            # --- 5. Canary read-only query ---
            try:
                db = mongodb_client[self.databag["database"]]
                db.list_collections()
                checks.append(
                    ValidationCheck(name="query", passed=True, message="Retrieved collection list successfully.")
                )
            except Exception as exc:
                checks.append(ValidationCheck(name="query", passed=False, message=str(exc)))
        finally:
            self._cleanup_client(mongodb_client)

        return self._make_result(level="simple", checks=checks)

    def _validate_deep(self) -> ValidationResult:
        """L2: Read/Write Capability with canary collection (create, write, read-verify, cleanup)."""
        start_time = time.time()
        timeout_secs = 10
        checks: list[ValidationCheck] = []

        # --- 1. Remote app presence ---
        error_result = self._check_relation_exists("deep")
        if error_result:
            return error_result

        # --- 2. Resolve credentials (plain fields or Juju secrets) ---
        creds = self._resolve_credentials()

        # --- 3. Schema check ---
        schema = ["endpoints", "database", "username", "password"]
        schema_check = self.validate_schema(schema, creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="deep", checks=checks)

        # --- 4. Connect ---
        endpoint = self.databag["endpoints"].split(",")[0].strip()
        try:
            mongodb_client = self._build_mongodb_client(creds)
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._make_result(level="deep", checks=checks)

        try:
            connect_check = self._attempt_connection(mongodb_client, endpoint)
            checks.append(connect_check)
            if not connect_check.passed:
                return self._make_result(level="deep", checks=checks)

            # --- 5. Create canary collection, write, read-verify, cleanup ---
            canary_collection = f"__canary_{uuid.uuid4().hex[:8]}"
            try:
                db = mongodb_client[self.databag["database"]]
                col = db[canary_collection]

                # Write a test document
                test_doc = {"_test": True, "timestamp": time.time()}
                result = col.insert_one(test_doc)
                inserted_id = result.inserted_id

                # Read back and verify
                read_doc = col.find_one({"_id": inserted_id})
                if read_doc is None or not read_doc.get("_test"):
                    checks.append(
                        ValidationCheck(
                            name="write_read_verify",
                            passed=False,
                            message="Failed to verify written document.",
                        )
                    )
                else:
                    checks.append(
                        ValidationCheck(
                            name="write_read_verify",
                            passed=True,
                            message="Successfully wrote, read, and verified test document.",
                        )
                    )

            except Exception as exc:
                checks.append(
                    ValidationCheck(
                        name="write_read_verify",
                        passed=False,
                        message=str(exc),
                    )
                )

            # --- 6. Cleanup: drop canary collection ---
            cleanup_passed = False
            cleanup_message = ""
            try:
                if mongodb_client is not None:
                    db = mongodb_client[self.databag["database"]]
                    db.drop_collection(canary_collection)
                    cleanup_passed = True
                    cleanup_message = "Dropped canary collection."
            except Exception as exc:  # nosec B110 - best-effort cleanup
                cleanup_message = f"Failed to drop canary collection: {exc}"

            checks.append(
                ValidationCheck(
                    name="cleanup",
                    passed=cleanup_passed,
                    message=cleanup_message,
                )
            )
        finally:
            self._cleanup_client(mongodb_client)

        # --- 7. Latency check ---
        elapsed = time.time() - start_time
        if elapsed > timeout_secs:
            checks.append(
                ValidationCheck(
                    name="latency",
                    passed=False,
                    message=f"Deep validation took {elapsed:.1f}s, exceeded {timeout_secs}s limit.",
                )
            )
        else:
            checks.append(
                ValidationCheck(
                    name="latency",
                    passed=True,
                    message=f"Deep validation completed in {elapsed:.1f}s.",
                )
            )

        return self._make_result(level="deep", checks=checks)

    def _check_relation_exists(self, level: ValidationLevel) -> ValidationResult | None:
        """Check if remote app exists on relation. Returns error result if missing, else None."""
        if not self.relation_exists():
            return self._make_result(
                status="ERROR",
                level=level,
                error=f"No remote application on relation '{self.endpoint}'.",
            )
        return None

    def _attempt_connection(self, mongodb_client: MongoClient[Any] | None, endpoint: str) -> ValidationCheck:
        """Attempt to connect and ping the MongoDB server."""
        try:
            if mongodb_client is None:
                raise ValueError("MongoDB client is None")
            mongodb_client.admin.command("ping")
            return ValidationCheck(name="connect", passed=True, message=f"Connected to {endpoint}.")
        except Exception as exc:
            return ValidationCheck(name="connect", passed=False, message=str(exc))


class MongoDBClientPersistenceValidator(_MongoDBConnectionMixin, BasePersistenceValidator):
    """Reference-style persistence validator for the mongodb_client interface.

    Each validator instance owns a dedicated canary collection named
    ``validator_canary_{scope_token}_{identifier}``, where ``scope_token`` is a fixed-width hash of
    this model's UUID, relation ID and unit name (see ``_canary_collection_prefix``) and
    ``identifier`` is a fixed-width, zero-padded value chosen by ``prepare()`` and carried forward
    by the caller (the test harness) as ``PersistenceState.id``. Every document also carries a
    random, unguessable ``token`` generated by ``prepare()`` and carried forward as
    ``PersistenceState.token``, so ``checkpoint()`` can detect data loss (or a collection silently
    recreated from scratch) by counting only documents tagged with that token, rather than trusting
    a plain document count that a coincidentally-sized but unrelated collection could satisfy.

    Persistence only applies to the requirer side of the relation (the side holding credentials to
    connect out); the provider side raises ``PersistenceNotApplicable``, mirroring the role check
    ``MongoDBClientValidator.validate()`` performs for the functional probe.
    """

    def prepare(self) -> PersistenceState:
        self._require_requires_role()
        # Masked to 63 bits so the canary collection name - which also carries a fixed-width scope
        # token - stays reasonably sized, while still leaving far more entropy than a test run
        # could collide on.
        identifier = uuid.uuid4().int & _MAX_CANARY_IDENTIFIER
        collection_name = self._canary_collection_name(identifier)
        # Random, unguessable per-run token written to every canary document and matched on by
        # checkpoint(). It must not be derivable from `identifier`/`ref`: those are reproducible,
        # so a backend that lost the canary data and recreated the collection from scratch would
        # reproduce the same value and pass falsely.
        token = uuid.uuid4().hex
        client, db = self._open_database()
        try:
            db.drop_collection(collection_name)
            db[collection_name].insert_one({"marker": token, "checkpoint_ref": 1})
        finally:
            self._cleanup_client(client)
        return PersistenceState(id=identifier, ref=1, token=token)

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        self._require_requires_role()
        if expected.ref < 1:
            # prepare() always returns ref=1 and checkpoint() only ever advances it, so a
            # restored/malformed PersistenceState with ref <= 0 can't have come from a real prior
            # run. Without this check, an empty or partially recreated collection (actual == 0)
            # could satisfy `actual == expected.ref` for ref=0 and report a false PASS.
            raise ValueError(f"expected.ref {expected.ref} is out of range (expected >= 1)")
        # Also validates expected.id is within range prepare() could have produced.
        collection_name = self._canary_collection_name(expected.id)
        client, db = self._open_database()
        try:
            col = db[collection_name]
            # Filter on the random token written by prepare(), not a bare document count: a
            # collection dropped and recreated from scratch could otherwise coincidentally satisfy
            # a count-only check.
            #
            # Require the exact set of refs 1..expected.ref, not just a matching count: e.g. a
            # duplicated ref alongside a missing one would otherwise still satisfy a bare
            # `count_documents() == expected.ref` check since the totals happen to match.
            #
            # Validate each ref is a real int (excluding bool, which is an int subclass) before
            # sorting/comparing - a same-token document with e.g. a missing or non-int
            # `checkpoint_ref` must correctly fail this check rather than crash it or be silently
            # skipped.
            same_token_docs = list(col.find({"marker": expected.token}))
            refs_are_valid = all(
                isinstance(doc.get("checkpoint_ref"), int) and not isinstance(doc.get("checkpoint_ref"), bool)
                for doc in same_token_docs
            )
            if refs_are_valid:
                matching_refs = sorted(doc["checkpoint_ref"] for doc in same_token_docs)
                matching = len(matching_refs)
                # Compared positionally rather than via `list(range(1, expected.ref + 1)) ==
                # matching_refs`: expected.ref comes from an untrusted --refs payload, only checked
                # above to be >= 1, so materializing a range up to it would let a malformed state
                # with a very large ref exhaust memory. This keeps the cost bounded by the number
                # of real documents matching_refs actually holds.
                passed = matching == expected.ref and all(ref == index + 1 for index, ref in enumerate(matching_refs))
            else:
                matching = len(same_token_docs)
                passed = False
            # Only write the next canary document when this checkpoint passed: ValidatorRunner
            # only carries the advanced PersistenceState forward on a PASS result, so writing here
            # unconditionally would grow `actual` past what the harness will ever compare against
            # again, masking the mismatch behind permanent drift.
            if passed:
                next_ref = expected.ref + 1
                col.insert_one({"marker": expected.token, "checkpoint_ref": next_ref})
        finally:
            self._cleanup_client(client)

        check = ValidationCheck(
            name="document_count",
            passed=passed,
            message=(
                f"Found expected {matching} marked document(s) in '{collection_name}'."
                if passed
                else (
                    f"Expected {expected.ref} marked document(s) with matching identity in "
                    f"'{collection_name}', found {matching}. Data may have been lost, or the "
                    "collection was recreated without the original canary documents."
                )
            ),
        )
        result = self._make_result(level="deep", checks=[check])
        new_state = PersistenceState(id=expected.id, ref=expected.ref + 1, token=expected.token) if passed else expected
        return result, new_state

    def cleanup(self) -> None:
        """Drop every canary collection this validator instance (or a prior instance) created.

        ``cleanup()`` takes no state argument (see ``BasePersistenceValidator.cleanup``), so every
        collection matching this instance's canary name pattern is discovered and dropped, rather
        than dropping one collection by identifier. This also mops up a collection left behind by
        an interrupted run (e.g. a crash between ``prepare()`` and the next ``cleanup()``).

        Discovery is scoped to a model+relation+unit namespace (see ``_canary_collection_prefix``)
        so concurrent relations sharing a database can't drop each other's collections. It does not
        sweep up a stray collection from a relation removed and re-added under a new ID - an
        accepted trade-off, since a fresh ``prepare()`` for the new ID starts its own collection
        anyway.

        The ``listCollections`` query only narrows candidates by *prefix* (regex can't cheaply
        assert an exact suffix shape server-side), so every candidate is re-checked against
        ``_canary_collection_regex()`` and ``_MAX_CANARY_IDENTIFIER`` before being dropped. This
        rejects a same-prefixed but unrelated collection (e.g. a hand-created ``..._backup``) that
        a bare prefix match would otherwise destroy.
        """
        self._require_requires_role()
        # Incomplete credentials mean cleanup can't run: raise PersistenceNotApplicable so the
        # runner records a skip (not a successful cleanup) and keeps the tracked state, rather than
        # forgetting orphaned canary data.
        creds = self._resolve_credentials()
        if not self.validate_schema(["endpoints", "database", "username", "password"], creds).passed:
            raise PersistenceNotApplicable(
                "Relation credentials are incomplete; cleanup cannot remove canary data yet."
            )
        client, db = self._open_database()
        try:
            prefix = self._canary_collection_prefix()
            # regex is anchored to the escaped prefix; MongoDB's $regex is a substring/pattern
            # match, not a full-string match, but the escaped literal prefix has no metacharacters
            # of its own so this only narrows candidates - the exact-shape re-check below is what
            # guards against destroying an unrelated, same-prefixed collection.
            names = db.list_collection_names(filter={"name": {"$regex": f"^{re.escape(prefix)}"}})
            name_regex = self._canary_collection_regex()
            for name in names:
                match = name_regex.fullmatch(name)
                if not match or int(match.group("identifier")) > _MAX_CANARY_IDENTIFIER:
                    continue
                db.drop_collection(name)
        finally:
            self._cleanup_client(client)

    def _require_requires_role(self) -> None:
        if self.role != "requires":
            raise PersistenceNotApplicable(f"Role '{self.role}' is not supported by {self.__class__.__name__}.")

    def _open_database(self) -> tuple[MongoClient[Any], Database[Any]]:
        creds = self._resolve_credentials()
        # Unlike the functional validator's validate()/deep(), these methods have no ValidationCheck
        # to report a schema failure through, so a missing/blank field is raised rather than passed
        # on to MongoClient, which would silently attempt an unauthenticated/malformed connection.
        schema_check = self.validate_schema(["endpoints", "database", "username", "password"], creds)
        if not schema_check.passed:
            raise RuntimeError(f"Cannot open a connection for {self.endpoint}: {schema_check.message}")
        endpoint = self.databag["endpoints"].split(",")[0].strip()
        if not endpoint:
            # validate_schema() only sees the raw "endpoints" string, so a non-blank value that is
            # still unusable once split/stripped (e.g. a leading comma) passes that check but would
            # otherwise reach MongoClient() with a blank host.
            raise RuntimeError(f"Cannot open a connection for {self.endpoint}: first entry in 'endpoints' is blank")
        client = self._build_mongodb_client(creds)
        try:
            return client, client[self.databag["database"]]
        except Exception:
            # client[...] can raise (e.g. an invalid database name containing "$" or a NUL) before
            # the caller ever reaches its own try/finally cleanup scope - clean up here so a bad
            # database value doesn't leak the client connection or its temporary CA file.
            self._cleanup_client(client)
            raise

    def _canary_collection_prefix(self) -> str:
        """Prefix scoped to this model, relation and unit, so cleanup discovery can't cross boundaries.

        ``self.relation_id`` is stable for the lifetime of a given relation, but relation IDs are
        assigned independently per model and can collide numerically across two different models
        relating to the same backend. The unit name is also part of the scope: the runner runs
        persistence validators on *every* unit of the application, so two units share both
        ``model.uuid`` and ``relation_id`` while owning separate canary collections (see
        ``cleanup()``). All three are folded into a single fixed-width ``scope_token`` - the first
        16 hex characters (64 bits) of a SHA-256 hash of
        ``f"{model_uuid}:{relation_id}:{unit_name}"`` - so ``cleanup()`` can validate its exact
        shape via ``_canary_collection_regex()``.
        """
        scope_token = self._canary_scope_token()
        return f"{_CANARY_COLLECTION_PREFIX}{scope_token}_"

    def _canary_scope_token(self) -> str:
        unit_name = self.charm.model.unit.name
        digest_input = f"{self.charm.model.uuid}:{self.relation_id}:{unit_name}".encode()
        return hashlib.sha256(digest_input).hexdigest()[:16]

    def _canary_collection_regex(self) -> "re.Pattern[str]":
        """Exact-shape match for this relation's canary collections: prefix + fixed-width digits.

        Used by ``cleanup()`` to reject a collection that merely shares the discovery prefix (e.g.
        a hand-created ``..._backup``) but doesn't match the fixed-width zero-padded identifier
        suffix ``_canary_collection_name()`` always produces. Matching this shape alone isn't
        sufficient though - see ``cleanup()``, which also checks the captured ``identifier``
        against ``_MAX_CANARY_IDENTIFIER``.
        """
        return re.compile(re.escape(self._canary_collection_prefix()) + r"(?P<identifier>[0-9]{20})")

    def _canary_collection_name(self, identifier: int) -> str:
        # Zero-padded to a fixed 20 digits (prepare() masks identifiers to 63 bits, so never more
        # than 19) so every canary name has the same shape, which _canary_collection_regex()
        # relies on. checkpoint() passes back an identifier from a possibly restored/malformed
        # state, so range-check it here too rather than letting it silently name the wrong
        # collection.
        if not 0 <= identifier <= _MAX_CANARY_IDENTIFIER:
            raise ValueError(f"canary identifier {identifier} is out of range (expected 0..{_MAX_CANARY_IDENTIFIER})")
        return f"{self._canary_collection_prefix()}{identifier:020d}"
