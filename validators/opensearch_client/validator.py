# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import os
import tempfile
import uuid
from typing import Any

from opensearchpy import OpenSearch, RequestsHttpConnection
from opensearchpy.exceptions import NotFoundError

from validators.base import (
    BasePersistenceValidator,
    BaseValidator,
    PersistenceNotApplicable,
    PersistenceState,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_CONNECT_TIMEOUT = 5
_REQUEST_TIMEOUT = 10
_HEALTHY_STATUSES = {"green", "yellow"}
_REQUIRED_FIELDS = ["endpoints", "username", "password"]

# prepare() masks its identifier to 63 bits (see PersistenceState.id); this bound lets
# checkpoint() reject an out-of-range identifier that prepare() couldn't have produced.
_MAX_CANARY_IDENTIFIER = (1 << 63) - 1

# Document field names used to tag canary documents. Prefixed to make collision with genuine
# application data (stored in the same, charm-granted index - see
# OpenSearchClientPersistenceValidator) unlikely.
_MARKER_FIELD = "validator_marker"
_SCOPE_FIELD = "validator_scope"
_REF_FIELD = "validator_checkpoint_ref"
# Present (with this fixed value) on every canary document this validator writes, and required by
# every count()/search() query alongside validator_scope/validator_marker. Without it, an
# application document that happens to carry the same field name/value as validator_scope (however
# unlikely) would otherwise be indistinguishable from validator-owned data and could be counted or
# deleted by cleanup().
#
# Deliberately a single token with no underscores/hyphens/spaces: _exact_match_filter() falls back
# to matching the plain (non-".keyword") field directly when a pre-existing index mapped it as
# analyzed "text" with no ".keyword" multi-field. A standard analyzer splits on those separator
# characters, so a multi-word value (e.g. containing "_") would be indexed as several terms and
# could never satisfy a single `term` query for the whole string - silently matching zero
# documents even though they exist.
_KIND_FIELD = "validator_kind"
_KIND_VALUE = "opensearchclientpersistencecanary"

# Page size for each cleanup() search page that discovers this relation/unit's canary documents.
# _delete_matching_documents() pages through results (see its docstring), so this bounds memory per
# page rather than the total number of documents cleanup() can remove.
_CLEANUP_SEARCH_SIZE = 10_000


def _exact_match_filter(field: str, value: str) -> dict[str, Any]:
    """Build a query clause that exact-matches ``value`` in ``field``, independent of mapping.

    The granted credentials can't read the shared, charm-granted index's mapping (see the
    persistence validator's class docstring: ``indices:admin/mappings/get`` is forbidden), so
    whether OpenSearch's dynamic mapping resolved ``field`` to a plain ``keyword`` type or to
    analyzed ``text`` with a ``.keyword`` multi-field is unknown and could vary. Matching only
    against ``field.keyword`` would return zero hits (and a false checkpoint FAIL / no-op cleanup)
    if the field turned out to be mapped as plain ``keyword`` with no ``.keyword`` multi-field to
    query. Matching this validator's own values (always plain hex strings, so never split
    differently by an analyzer) against either shape covers both cases.
    """
    return {
        "bool": {
            "should": [
                {"term": {field: value}},
                {"term": {f"{field}.keyword": value}},
            ],
            "minimum_should_match": 1,
        }
    }


class _OpenSearchConnectionMixin:
    """Shared credential-resolution and connection helpers for opensearch_client validators.

    Both ``OpenSearchClientValidator`` (health probe) and
    ``OpenSearchClientPersistenceValidator`` (durability probe) need to resolve the same relation
    credentials and open the same kind of opensearch-py client, so that logic lives here once
    instead of being duplicated.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ca_file_path: str | None = None

    def _resolve_credentials(self) -> dict[str, str]:
        """Resolve username, password, and optional tls-ca from the relation databag."""
        return {
            **self.resolve_secret("secret-user", "username", "password"),  # type: ignore[attr-defined]
            **self.resolve_secret("secret-tls", "tls-ca"),  # type: ignore[attr-defined]
        }

    def _build_client(self, creds: dict[str, str]) -> OpenSearch:
        """Construct an OpenSearch client from relation data."""
        raw_endpoints = (creds.get("endpoints") or self.databag.get("endpoints", "")).strip()  # type: ignore[attr-defined]
        hosts = []
        for ep in raw_endpoints.split(","):
            ep = ep.strip()
            if not ep:
                continue
            if ":" in ep:
                host, port_str = ep.rsplit(":", 1)
                try:
                    hosts.append({"host": host, "port": int(port_str)})
                except ValueError:
                    hosts.append({"host": host, "port": 9200})
            else:
                hosts.append({"host": ep, "port": 9200})

        if not hosts:
            # A non-blank but unusable "endpoints" value (e.g. ", " or a bare comma) would
            # otherwise leave hosts empty; opensearch-py silently defaults an empty host list to
            # localhost:9200 rather than raising, which could make this connect to an unintended
            # local service instead of the related OpenSearch cluster.
            endpoint = self.endpoint  # type: ignore[attr-defined]
            raise RuntimeError(f"Cannot open a connection for {endpoint}: no usable hosts parsed from 'endpoints'.")

        ca_certs = self._write_ca_file(creds.get("tls-ca"))
        use_ssl = ca_certs is not None

        try:
            return OpenSearch(
                hosts=hosts,
                http_auth=(creds.get("username", ""), creds.get("password", "")),
                use_ssl=use_ssl,
                verify_certs=use_ssl,
                ca_certs=ca_certs,
                connection_class=RequestsHttpConnection,
                timeout=_CONNECT_TIMEOUT,
            )
        except Exception:
            self._remove_ca_file()
            raise

    def _close_client(self, client: OpenSearch) -> None:
        """Close the OpenSearch client transport."""
        try:
            client.close()
        except Exception:  # nosec B110
            pass
        self._remove_ca_file()

    def _write_ca_file(self, ca_content: str | None) -> str | None:
        """Write CA cert to a temp file; return the path, or None if no cert provided."""
        if not ca_content:
            return None
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".pem") as f:
            f.write(ca_content)
            self._ca_file_path = f.name
        return self._ca_file_path

    def _remove_ca_file(self) -> None:
        if self._ca_file_path and os.path.exists(self._ca_file_path):
            os.remove(self._ca_file_path)
            self._ca_file_path = None


class OpenSearchClientValidator(_OpenSearchConnectionMixin, BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")
        if level == "deep":
            return self._validate_deep()
        return self._validate_simple()

    # ------------------------------------------------------------------
    # L1: Schema + cluster connectivity + health
    # ------------------------------------------------------------------

    def _validate_simple(self) -> ValidationResult:
        """L1: Resolve credentials, connect to cluster, confirm health is green or yellow."""
        checks: list[ValidationCheck] = []
        creds = self._resolve_credentials()

        schema_check = self.validate_schema(_REQUIRED_FIELDS, creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="simple", checks=checks)

        try:
            client = self._build_client(creds)
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._make_result(level="simple", checks=checks)

        try:
            health_check = self._check_cluster_health(client)
            checks.append(health_check)
        finally:
            self._close_client(client)

        return self._make_result(level="simple", checks=checks)

    # ------------------------------------------------------------------
    # L2: Schema + health + canary index (create → index → get → delete)
    # ------------------------------------------------------------------

    def _validate_deep(self) -> ValidationResult:
        """L2: All L1 checks, then write a canary document into the granted index, retrieve it, and delete it."""
        checks: list[ValidationCheck] = []
        creds = self._resolve_credentials()

        schema_check = self.validate_schema(_REQUIRED_FIELDS, creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="deep", checks=checks)

        try:
            client = self._build_client(creds)
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._make_result(level="deep", checks=checks)

        try:
            health_check = self._check_cluster_health(client)
            checks.append(health_check)
            if not health_check.passed:
                return self._make_result(level="deep", checks=checks)

            checks.extend(self._check_canary_index(client))
        finally:
            self._close_client(client)

        return self._make_result(level="deep", checks=checks)

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    def _check_cluster_health(self, client: OpenSearch) -> ValidationCheck:
        """GET /_cluster/health and confirm status is green or yellow."""
        try:
            health = client.cluster.health(request_timeout=_REQUEST_TIMEOUT)
            status = health.get("status", "unknown")
            if status in _HEALTHY_STATUSES:
                return ValidationCheck(
                    name="cluster_health",
                    passed=True,
                    message=f"Cluster health is '{status}'.",
                )
            return ValidationCheck(
                name="cluster_health",
                passed=False,
                message=f"Cluster health is '{status}'; expected green or yellow.",
            )
        except Exception as exc:
            return ValidationCheck(name="cluster_health", passed=False, message=str(exc))

    def _check_canary_index(self, client: OpenSearch) -> list[ValidationCheck]:
        """Write a canary document into the granted index, retrieve it, then delete it."""
        checks: list[ValidationCheck] = []
        index_name = self.databag.get("index", "")
        if not index_name:
            return [ValidationCheck(name="index_document", passed=False, message="No 'index' in relation databag.")]
        doc_id = f"validator-canary-{uuid.uuid4().hex[:8]}"
        doc_body = {"validator": "opensearch_client", "canary": True}

        # 1. Index document
        try:
            client.index(index=index_name, id=doc_id, body=doc_body, request_timeout=_REQUEST_TIMEOUT)
            checks.append(ValidationCheck(name="index_document", passed=True, message="Document indexed."))
        except Exception as exc:
            checks.append(ValidationCheck(name="index_document", passed=False, message=str(exc)))
            return checks

        try:
            # 2. Retrieve document
            try:
                result = client.get(index=index_name, id=doc_id, request_timeout=_REQUEST_TIMEOUT)
                retrieved = result.get("_source", {})
                if retrieved.get("canary") is True:
                    checks.append(
                        ValidationCheck(name="document_get", passed=True, message="Document retrieved and verified.")
                    )
                else:
                    checks.append(
                        ValidationCheck(
                            name="document_get",
                            passed=False,
                            message=f"Retrieved document does not match: {retrieved}",
                        )
                    )
            except Exception as exc:
                checks.append(ValidationCheck(name="document_get", passed=False, message=str(exc)))
        finally:
            # 3. Delete canary document (always clean up)
            try:
                client.delete(index=index_name, id=doc_id, request_timeout=_REQUEST_TIMEOUT)
                checks.append(ValidationCheck(name="document_delete", passed=True, message="Canary document deleted."))
            except Exception as exc:
                checks.append(ValidationCheck(name="document_delete", passed=False, message=str(exc)))

        return checks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------


class OpenSearchClientPersistenceValidator(_OpenSearchConnectionMixin, BasePersistenceValidator):
    """Reference-style persistence validator for the opensearch_client interface.

    Unlike the MongoDB/PostgreSQL persistence validators, this validator cannot create or drop
    its own dedicated canary index: the credentials granted over an ``opensearch_client`` relation
    are scoped by the OpenSearch security plugin to document-level operations (index/get/delete/
    search/count) on exactly the one index named in the relation databag (``index``) - live
    verification confirmed the granted user gets ``security_exception`` (403) for
    ``indices:admin/create``, ``indices:admin/delete`` and even ``indices:admin/mappings/get``.

    So canary *documents* are written into that shared, charm-granted index instead, each tagged
    with:

    - ``validator_scope``: a fixed-width hash of this model's UUID, relation ID and unit name (see
      ``_canary_scope_token``), so this validator instance's documents can be told apart from
      another relation/unit's canary documents sharing the same index, and from the target
      application's own data.
    - ``validator_marker``: a random, unguessable per-``prepare()`` token, matched via a
      mapping-agnostic exact-match query (see ``_exact_match_filter``) rather than a plain document
      count. Matching only on ``validator_scope`` would let a backend that lost the canary data and
      then had a *different* prepare() run write fresh documents into the same index still satisfy
      a bare count - the token, unlike the scope, is never reused across a fresh prepare() call.
    - ``validator_checkpoint_ref``: the same monotonically increasing counter tracked in
      ``PersistenceState.ref``. ``checkpoint()`` reads this back from every matching document
      (not just their count) to require the exact multiset of refs ``1..expected.ref``, so an
      index containing e.g. a duplicate ref alongside a missing one can't coincidentally satisfy
      a count-only check.
    - ``validator_kind``: a fixed sentinel value present on every canary document. Required
      alongside ``validator_scope``/``validator_marker`` by every count()/search() query, so an
      application document that happens to carry the same field name/value as one of those (e.g.
      ``validator_scope``, however unlikely) is never mistaken for validator-owned data and
      counted or deleted.

    Persistence only applies to the requirer side of the relation (the side holding credentials to
    connect out); the provider side raises ``PersistenceNotApplicable``, mirroring the role check
    ``OpenSearchClientValidator.validate()`` performs for the functional probe.
    """

    def prepare(self) -> PersistenceState:
        self._require_requires_role()
        # Not used to address any OpenSearch object (there's no per-run index/collection to name),
        # but still assigned and carried in PersistenceState.id for parity with the other
        # persistence validators, and range-checked by checkpoint() as a defense against a
        # restored/malformed state.
        identifier = uuid.uuid4().int & _MAX_CANARY_IDENTIFIER
        # Random, unguessable per-run token written to every canary document and matched on by
        # checkpoint(). It must not be derivable from `identifier`/`ref`: those are reproducible,
        # so a backend that lost the canary data and recreated it from scratch would otherwise
        # still satisfy the check and report a false PASS.
        token = uuid.uuid4().hex
        client = self._open_client()
        try:
            index_name = self._target_index_name()
            scope = self._canary_scope_token()
            # "Drop and recreate": since the granted credentials can't drop/recreate the shared
            # index itself, clear out any canary documents this relation/unit left behind (e.g.
            # from an interrupted prior run) before writing the fresh baseline document.
            self._delete_matching_documents(client, index_name, scope)
            # refresh=True makes the write visible to an immediate count()/search() - without it,
            # OpenSearch's near-real-time indexing could let a checkpoint() run shortly after
            # prepare() see zero matching documents even though the write succeeded.
            self._write_canary_document(client, index_name, scope, token, checkpoint_ref=1)
        finally:
            self._close_client(client)
        return PersistenceState(id=identifier, ref=1, token=token)

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        self._require_requires_role()
        if expected.ref < 1:
            # prepare() always returns ref=1 and checkpoint() only ever advances it, so a
            # restored/malformed PersistenceState with ref <= 0 can't have come from a real prior
            # run. Without this check, an empty index (actual == 0) could satisfy
            # `actual == expected.ref` for ref=0 and report a false PASS.
            raise ValueError(f"expected.ref {expected.ref} is out of range (expected >= 1)")
        if not expected.token:
            # An empty token can only come from a state serialised before the token existed, or
            # otherwise restored/malformed - it can never have come from a real prepare() call.
            raise ValueError("expected.token must not be empty")
        if not 0 <= expected.id <= _MAX_CANARY_IDENTIFIER:
            raise ValueError(f"expected.id {expected.id} is out of range (expected 0..{_MAX_CANARY_IDENTIFIER})")

        client = self._open_client()
        try:
            index_name = self._target_index_name()
            scope = self._canary_scope_token()
            # Require the exact multiset of refs 1..expected.ref tagged with our scope/token/kind,
            # not just a matching count: an index whose canary documents hold e.g. a duplicate ref
            # alongside a missing one could otherwise coincidentally satisfy a bare
            # `count() == expected.ref` check since the totals happen to match. Compared
            # positionally (rather than via `list(range(1, expected.ref + 1)) == matching_refs`)
            # so verification cost is bounded by the number of real documents actually found, not
            # by an untrusted, schema-valid but arbitrarily large `expected.ref` from --refs.
            #
            # Collect every matching document's ref first, then validate each is a real int
            # (excluding bool, which is an int subclass) - filtering malformed refs out up front
            # would let a matching document with e.g. a string ref be silently ignored, rather than
            # correctly failing a check whose contract is "verify the exact tagged document set".
            matching_refs = self._collect_matching_refs(client, index_name, scope, expected.token)
            refs_are_valid = all(isinstance(ref, int) and not isinstance(ref, bool) for ref in matching_refs)
            if refs_are_valid:
                sorted_refs = sorted(matching_refs)
                matching = len(sorted_refs)
                passed = matching == expected.ref and all(ref == index + 1 for index, ref in enumerate(sorted_refs))
            else:
                matching = len(matching_refs)
                passed = False
            # Only write the next canary document when this checkpoint passed: ValidatorRunner
            # only carries the advanced PersistenceState forward on a PASS result, so writing here
            # unconditionally would grow `actual` past what the harness will ever compare against
            # again, masking the mismatch behind permanent drift.
            if passed:
                next_ref = expected.ref + 1
                self._write_canary_document(client, index_name, scope, expected.token, checkpoint_ref=next_ref)
        finally:
            self._close_client(client)

        check = ValidationCheck(
            name="document_count",
            passed=passed,
            message=(
                f"Found expected {matching} marked document(s) in '{index_name}'."
                if passed
                else (
                    f"Expected {expected.ref} marked document(s) with matching identity in "
                    f"'{index_name}', found {matching}. Data may have been lost, or the index "
                    "was recreated without the original canary documents."
                )
            ),
        )
        result = self._make_result(level="deep", checks=[check])
        new_state = PersistenceState(id=expected.id, ref=expected.ref + 1, token=expected.token) if passed else expected
        return result, new_state

    def cleanup(self) -> None:
        """Delete every canary document this validator instance (or a prior instance) wrote.

        ``cleanup()`` takes no state argument (see ``BasePersistenceValidator.cleanup``), so every
        document tagged with this relation/unit's ``validator_scope`` is discovered by search and
        deleted individually, rather than deleting one document by identifier. This also mops up
        documents left behind by an interrupted run (e.g. a crash between ``prepare()`` and the
        next ``cleanup()``).

        Unlike the MongoDB/PostgreSQL persistence validators, this can't drop a whole
        collection/table: the granted OpenSearch credentials only permit document-level operations
        on the shared, charm-granted index (see the class docstring), so only documents matching
        this exact ``validator_scope`` are ever touched - never the target application's own data,
        and never another relation/unit's canary documents sharing the same index.
        """
        self._require_requires_role()
        # Incomplete credentials mean cleanup can't run: raise PersistenceNotApplicable so the
        # runner records a skip (not a successful cleanup) and keeps the tracked state, rather than
        # forgetting orphaned canary data.
        creds = self._resolve_credentials()
        if not self.validate_schema(["endpoints", "username", "password"], creds).passed:
            raise PersistenceNotApplicable(
                "Relation credentials are incomplete; cleanup cannot remove canary data yet."
            )
        client = self._open_client()
        try:
            index_name = self._target_index_name()
            scope = self._canary_scope_token()
            self._delete_matching_documents(client, index_name, scope)
        finally:
            self._close_client(client)

    def _require_requires_role(self) -> None:
        if self.role != "requires":
            raise PersistenceNotApplicable(f"Role '{self.role}' is not supported by {self.__class__.__name__}.")

    def _open_client(self) -> OpenSearch:
        creds = self._resolve_credentials()
        # Unlike the functional validator's validate(), these methods have no ValidationCheck to
        # report a schema failure through, so a missing/blank required field is raised rather than
        # silently producing a client with no usable hosts.
        schema_check = self.validate_schema(["endpoints", "username", "password"], creds)
        if not schema_check.passed:
            raise RuntimeError(f"Cannot open a connection for {self.endpoint}: {schema_check.message}")
        raw_endpoints = (creds.get("endpoints") or self.databag.get("endpoints", "")).strip()
        if not raw_endpoints:
            # validate_schema() only sees the raw "endpoints" string, so a non-blank value that is
            # still unusable once split/stripped (e.g. a leading comma) passes that check but would
            # otherwise reach _build_client() with no usable hosts, which opensearch-py silently
            # defaults to localhost:9200 rather than raising.
            raise RuntimeError(f"Cannot open a connection for {self.endpoint}: 'endpoints' is blank")
        return self._build_client(creds)

    def _target_index_name(self) -> str:
        """Return the single index name granted by this relation.

        The granted credentials only permit document-level operations against this one,
        charm-created index (see the class docstring) - there is no index to create or address by
        identifier, unlike the MongoDB/PostgreSQL persistence validators.
        """
        index_name = self.databag.get("index", "").strip()
        if not index_name:
            raise RuntimeError(f"Cannot run persistence checks for {self.endpoint}: no 'index' in relation databag.")
        return index_name

    def _write_canary_document(
        self, client: OpenSearch, index_name: str, scope: str, token: str, checkpoint_ref: int
    ) -> None:
        client.index(
            index=index_name,
            body={
                _SCOPE_FIELD: scope,
                _MARKER_FIELD: token,
                _REF_FIELD: checkpoint_ref,
                _KIND_FIELD: _KIND_VALUE,
            },
            refresh=True,
            request_timeout=_REQUEST_TIMEOUT,
        )

    def _collect_matching_refs(self, client: OpenSearch, index_name: str, scope: str, token: str) -> list[Any]:
        """Return every ``validator_checkpoint_ref`` value for documents matching scope/token/kind.

        Paginates via ``search_after`` rather than a single ``size=_CLEANUP_SEARCH_SIZE`` page: an
        interrupted run or repeated checkpoints could in principle leave more than one page of
        same-token documents (e.g. after a replay), and only inspecting the first page would
        silently under-count them. Sorts by ``_doc`` (Lucene's internal, always-available document
        order) rather than ``_id``: OpenSearch disables fielddata on the ``_id`` field by default,
        so sorting on it raises ``illegal_argument_exception`` on a real cluster even though the
        in-memory test stub would happily accept it - ``_doc`` needs no such opt-in and is the
        documented choice for pagination that doesn't care about result ordering. Returns raw
        (possibly non-int) values rather than filtering them out, so a malformed ``_REF_FIELD``
        (e.g. a string) is reported back to checkpoint() instead of being silently ignored.
        """
        refs: list[Any] = []
        search_after: list[Any] | None = None
        while True:
            body: dict[str, Any] = {
                "query": {
                    "bool": {
                        "filter": [
                            _exact_match_filter(_SCOPE_FIELD, scope),
                            _exact_match_filter(_MARKER_FIELD, token),
                            _exact_match_filter(_KIND_FIELD, _KIND_VALUE),
                        ]
                    }
                },
                "size": _CLEANUP_SEARCH_SIZE,
                "sort": [{"_doc": "asc"}],
            }
            if search_after is not None:
                body["search_after"] = search_after
            try:
                response = client.search(index=index_name, body=body, request_timeout=_REQUEST_TIMEOUT)
            except NotFoundError:
                return refs
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                return refs
            for hit in hits:
                refs.append(hit.get("_source", {}).get(_REF_FIELD))
            search_after = hits[-1].get("sort")
            if search_after is None or len(hits) < _CLEANUP_SEARCH_SIZE:
                return refs

    def _delete_matching_documents(self, client: OpenSearch, index_name: str, scope: str) -> None:
        # Deletes by individually resolved document _id rather than via the OpenSearch
        # `_delete_by_query` API: the granted credentials return `security_exception` for
        # `indices:admin/refresh` when `_delete_by_query` is called with `refresh=true`, and
        # skipping `refresh` there left deletions silently invisible to an immediately following
        # count() in live testing. Searching for matching document ids and deleting each one
        # individually (an operation the granted credentials do permit - confirmed in live
        # testing) avoids that path entirely.
        #
        # Loops in pages of `_CLEANUP_SEARCH_SIZE` rather than fetching once: each `delete()` call
        # uses `refresh=true`, so a scoped document deleted in one page is no longer returned by the
        # next page's search, meaning this always converges on zero remaining scoped hits instead of
        # only ever clearing the first page.
        #
        # Filters on validator_kind as well as validator_scope: this is the only thing that lets
        # cleanup() tell a validator-owned document apart from an application document that happens
        # to carry the same field name/value as validator_scope (see _KIND_FIELD's comment).
        while True:
            try:
                response = client.search(
                    index=index_name,
                    body={
                        "query": {
                            "bool": {
                                "filter": [
                                    _exact_match_filter(_SCOPE_FIELD, scope),
                                    _exact_match_filter(_KIND_FIELD, _KIND_VALUE),
                                ]
                            }
                        },
                        "size": _CLEANUP_SEARCH_SIZE,
                    },
                    request_timeout=_REQUEST_TIMEOUT,
                )
            except NotFoundError:
                return
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                return
            for hit in hits:
                doc_id = hit.get("_id")
                if not doc_id:
                    continue
                try:
                    client.delete(index=index_name, id=doc_id, refresh=True, request_timeout=_REQUEST_TIMEOUT)
                except NotFoundError:
                    pass

    def _canary_scope_token(self) -> str:
        """Fixed-width hash scoping canary documents to this model, relation and unit.

        ``self.relation_id`` is stable for the lifetime of a given relation, but relation IDs are
        assigned independently per model and can collide numerically across two different models
        relating to the same backend. The unit name is also part of the scope: the runner runs
        persistence validators on *every* unit of the application, so two units share both
        ``model.uuid`` and ``relation_id`` while owning separate canary documents (see
        ``cleanup()``). All three are folded into the first 16 hex characters (64 bits) of a
        SHA-256 hash of ``f"{model_uuid}:{relation_id}:{unit_name}"``.
        """
        unit_name = self.charm.model.unit.name
        digest_input = f"{self.charm.model.uuid}:{self.relation_id}:{unit_name}".encode()
        return hashlib.sha256(digest_input).hexdigest()[:16]
