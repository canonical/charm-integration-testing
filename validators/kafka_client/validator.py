# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from typing import Any

from kafka import KafkaConsumer, KafkaProducer, TopicPartition  # type: ignore[import-untyped]
from kafka.admin import KafkaAdminClient, NewTopic  # type: ignore[import-untyped]
from kafka.errors import (  # type: ignore[import-untyped]
    TopicAlreadyExistsError,
    UnknownTopicOrPartitionError,
)

from validators.base import (
    BasePersistenceValidator,
    BaseValidator,
    PersistenceNotApplicable,
    PersistenceState,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_CLIENT_TIMEOUT_MS = 5000
_CLIENT_IDLE_MS = 10000
_SIMPLE_LATENCY_TARGET_S = 0.5
_DEEP_LATENCY_TARGET_S = 10.0
_CONSUME_TIMEOUT_S = 5.0

# Topic name prefix for persistence-validator canary topics. Kept as a module constant so
# cleanup() (which has no per-call state to work from) can discover every canary topic it may
# have created by pattern rather than by identifier.
_CANARY_TOPIC_PREFIX = "validator_canary_"

# prepare() masks its identifier to 63 bits, so a genuine canary identifier never exceeds this
# value. cleanup()'s discovery regex only checks a candidate topic name's *shape* (prefix + 20
# digits); this bound lets it also reject an out-of-range look-alike with the right shape that
# prepare() couldn't have produced.
_MAX_CANARY_IDENTIFIER = (1 << 63) - 1


class _KafkaConnectionMixin:
    """Shared credential-resolution and client-construction helpers for kafka_client validators.

    Both ``KafkaClientValidator`` (health probe) and ``KafkaClientPersistenceValidator``
    (durability probe) need to resolve the same relation credentials and build the same kind of
    kafka-python clients, so that logic lives here once instead of being duplicated.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ca_file_path: str | None = None

    def _connection_databag(self) -> dict[str, str]:
        """Return the databag holding kafka_client connection fields for the current role.

        The interface is app scoped. On the requirer side, the connection fields
        (endpoints, topic, credentials, ...) live on the remote provider app's
        databag, which ``BaseValidator.databag`` already exposes (``relation.app``
        is always the *other* application). On the provider side we publish those
        same fields ourselves, so we must read our own app's databag instead.
        """
        if self.role == "provides":  # type: ignore[attr-defined]
            if self.charm.app not in self.relation.data:  # type: ignore[attr-defined]
                return {}
            return dict(self.relation.data[self.charm.app])  # type: ignore[attr-defined]
        return dict(self.databag)  # type: ignore[attr-defined]

    def _resolve_credentials(self, data: dict[str, str]) -> dict[str, str]:
        """Resolve credentials from the given databag or the Juju secrets it references."""
        return {
            **self.resolve_secret("secret-user", "username", "password", data=data),  # type: ignore[attr-defined]
            **self.resolve_secret("secret-tls", "tls", "tls-ca", data=data),  # type: ignore[attr-defined]
        }

    def _build_kafka_client_kwargs(self, data: dict[str, str]) -> dict[str, Any]:
        """Build shared Kafka client kwargs, handling SASL and TLS configuration."""
        bootstrap_servers = [e.strip() for e in data["endpoints"].split(",") if e.strip()]
        kwargs: dict[str, Any] = {
            "bootstrap_servers": bootstrap_servers,
            "security_protocol": "PLAINTEXT",
            "request_timeout_ms": _CLIENT_TIMEOUT_MS,
            "connections_max_idle_ms": _CLIENT_IDLE_MS,
        }

        username = data.get("username", "")
        password = data.get("password", "")
        tls_raw = data.get("tls", "").lower()
        tls_ca = data.get("tls-ca", "")
        # The charm sets "disabled" when TLS is off; treat that as absent.
        tls_enabled = tls_raw not in ("", "disabled")
        tls_ca_pem = tls_ca if tls_ca not in ("", "disabled") else ""

        has_sasl = bool(username and password)
        has_tls = tls_enabled or bool(tls_ca_pem)

        if has_tls and has_sasl:
            kwargs["security_protocol"] = "SASL_SSL"
        elif has_tls:
            kwargs["security_protocol"] = "SSL"
        elif has_sasl:
            kwargs["security_protocol"] = "SASL_PLAINTEXT"

        if has_sasl:
            kwargs["sasl_mechanism"] = "SCRAM-SHA-512"
            kwargs["sasl_plain_username"] = username
            kwargs["sasl_plain_password"] = password

        if tls_ca_pem:
            self._create_temp_ca_file(tls_ca_pem)
            kwargs["ssl_cafile"] = self._ca_file_path

        return kwargs

    def _build_consumer(
        self,
        data: dict[str, str],
        group_id: str | None = None,
        auto_offset_reset: str = "latest",
    ) -> KafkaConsumer:
        """Build a KafkaConsumer with appropriate security and offset settings."""
        kwargs = self._build_kafka_client_kwargs(data)
        # Fall back to prefixed group when no explicit group_id is given.
        default_group = f"{data.get('consumer-group-prefix', '')}validator"
        kwargs["group_id"] = group_id if group_id is not None else default_group
        kwargs["auto_offset_reset"] = auto_offset_reset
        kwargs["enable_auto_commit"] = False
        kwargs["consumer_timeout_ms"] = _CLIENT_TIMEOUT_MS
        return KafkaConsumer(**kwargs)

    def _build_raw_consumer(self, data: dict[str, str]) -> KafkaConsumer:
        """Build a standalone KafkaConsumer with no consumer group (manual partition assignment).

        Used by the persistence validator, which reads canary messages via ``assign()``/``seek``
        rather than ``subscribe()``, so it does not depend on a consumer group or the
        ``consumer-group-prefix`` ACL grant the functional validator's canary round trip needs.
        """
        kwargs = self._build_kafka_client_kwargs(data)
        kwargs["enable_auto_commit"] = False
        kwargs["consumer_timeout_ms"] = _CLIENT_TIMEOUT_MS
        return KafkaConsumer(**kwargs)

    def _build_producer(
        self, data: dict[str, str], *, acks: int | str = 1, enable_idempotence: bool = False
    ) -> KafkaProducer:
        """Build a KafkaProducer with appropriate security settings.

        ``acks`` defaults to kafka-python's own default (1: leader-only acknowledgement), matching
        the functional validator's existing round-trip behavior. The persistence producer passes
        ``acks="all"`` instead (see ``_produce_canary_message``): leader-only acknowledgement lets
        ``future.get()`` return before the write is replicated, so a broker disruption immediately
        after a successful ``prepare()``/``checkpoint()`` can lose the just-written canary and
        make this validator report a false persistence failure for a disruption the application's
        own, fully-replicated data would have survived.

        ``enable_idempotence`` also defaults to off, matching the functional validator's existing
        behavior. The persistence producer passes ``enable_idempotence=True``: ``acks="all"`` alone
        only guarantees a write is replicated once accepted, it does not stop a non-idempotent
        producer from appending a duplicate record if a retry fires after an ack is lost in
        transit. A duplicate canary message would otherwise be seen by checkpoint() as an
        unexpected extra record and reported as data loss even though the canary survived.
        """
        kwargs = self._build_kafka_client_kwargs(data)
        kwargs["acks"] = acks
        kwargs["enable_idempotence"] = enable_idempotence
        return KafkaProducer(**kwargs)

    def _build_admin_client(self, data: dict[str, str]) -> KafkaAdminClient:
        """Build a KafkaAdminClient with appropriate security settings."""
        return KafkaAdminClient(**self._build_kafka_client_kwargs(data))

    def _resolve_canary_replica_assignment(
        self, admin: KafkaAdminClient, application_topic: str | None
    ) -> list[int] | None:
        """Return the application topic's own partition-0 replica node IDs, or ``None``.

        Matching only the replication *factor* doesn't put the canary partition on the same
        brokers as the application topic: Kafka assigns each topic's partitions to brokers
        independently, so with e.g. RF=1 the application's partition could land on broker A while
        a same-RF canary lands on broker B - losing A then destroys the application's only replica
        while the canary, on an unaffected broker, keeps reporting PASS. Returning the application
        topic's actual replica node IDs lets the canary be created with an explicit
        ``replica_assignments`` pinning it to the exact same brokers (the same failure domain),
        instead of merely the same replica *count*.
        """
        if not application_topic:
            return None
        try:
            topics = admin.describe_topics([application_topic])
            partitions = topics[0]["partitions"] if topics else []
            replica_nodes = partitions[0]["replica_nodes"] if partitions else []
            return list(replica_nodes) if replica_nodes else None
        except Exception:  # nosec B110 - best-effort; fall back to the broker-count heuristic
            return None

    def _reject_unsupported_multi_partition_topology(
        self, admin: KafkaAdminClient, application_topic: str | None
    ) -> None:
        """Raise if *application_topic* has more than one partition.

        This validator's canary topic always has exactly one partition, pinned (via
        ``_resolve_canary_replica_assignment``) to the same brokers as the application topic's own
        partition 0. For a multi-partition application topic, Kafka can place other partitions
        (e.g. partition 1) on different brokers with their own, independently-lost replicas - a
        disruption destroying only those partitions' data would leave this single-partition canary
        (and partition 0) unaffected, so ``checkpoint()`` would report PASS despite real
        application data loss. Rather than silently provide that incomplete guarantee, refuse to
        prepare a canary for a topology this validator cannot fully cover.

        Unlike ``_resolve_canary_replica_assignment``'s best-effort fallback, a describe failure
        here is *not* treated as "single partition, proceed": that would just reintroduce the gap
        this check exists to close. It's only safe to skip the check (return without raising) when
        the partition count genuinely can't be determined yet (e.g. the application topic isn't
        describable/doesn't exist yet) - the same case ``_resolve_canary_replica_assignment`` falls
        back from - since there's then no known multi-partition topology to reject.
        """
        if not application_topic:
            return
        try:
            topics = admin.describe_topics([application_topic])
            partitions = topics[0]["partitions"] if topics else []
        except Exception:  # nosec B110 - best-effort; undeterminable partition count isn't a known-unsupported topology
            return
        if len(partitions) > 1:
            raise RuntimeError(
                f"Application topic '{application_topic}' has {len(partitions)} partitions; "
                f"{type(self).__name__} only supports single-partition topics, since a canary "
                "pinned to partition 0's brokers cannot detect data loss confined to another "
                "partition."
            )

    def _resolve_replication_factor(self, admin: KafkaAdminClient) -> int:
        """Pick a fallback replication factor from the live broker count, capped at 3.

        Used only when the application topic is unknown, not yet describable (e.g. the relation
        was just established), or has no partitions yet - see
        ``_resolve_canary_replica_assignment``, which is preferred whenever it can return an
        explicit replica assignment tied to the real application topic's own placement.
        """
        try:
            broker_count = len(admin.describe_cluster()["brokers"])
        except Exception:  # nosec B110 - best-effort; fall back to the safe single-broker default
            return 1
        return max(1, min(3, broker_count))

    def _ensure_topic_exists(self, data: dict[str, str], topic: str, match_application_topology: bool = False) -> bool:
        """Create the topic if it does not already exist. Returns whether it already existed.

        kafka-k8s sets auto.create.topics.enable=false, so the topic must be created explicitly.

        ``match_application_topology`` is ``False`` by default, preserving this method's original
        single-replica functional-probe behavior for ``KafkaClientValidator._validate_deep()``'s own
        probe topic. Only the persistence validator's canary topic (see
        ``KafkaClientPersistenceValidator.prepare()``) opts into pinning its replica count/placement
        to the application topic's own topology (see ``_resolve_canary_replica_assignment``) - a
        functional probe run without that opt-in must keep working on a cluster/credentials set up
        only for a single-replica topic, exactly as it did before persistence support existed.

        For the functional probe, a non-``TopicAlreadyExistsError`` creation failure is still
        swallowed — the subsequent produce step surfaces a meaningful error instead, and the probe
        doesn't care what topology an implicit, broker-side auto-create would use. For persistence
        (``match_application_topology=True``), the same failure is **not** swallowed: silently
        proceeding to ``producer.send()`` on a broker with ``auto.create.topics.enable=true`` would
        let the topic come into existence with cluster defaults instead of the replica
        assignment/replication factor just resolved above, so a later durability check could pass
        against a canary topic with different durability guarantees than the application's own
        topic. Raising here instead lets the caller (``prepare()``) fail loudly.

        For persistence, this also rejects a multi-partition application topic up front via
        ``_reject_unsupported_multi_partition_topology`` - see that method's docstring for why a
        single-partition canary can't be trusted to detect data loss confined to another
        partition.
        """
        admin: KafkaAdminClient | None = None
        try:
            admin = self._build_admin_client(data)
            if match_application_topology:
                self._reject_unsupported_multi_partition_topology(admin, data.get("topic"))
            replica_assignment = (
                self._resolve_canary_replica_assignment(admin, data.get("topic"))
                if match_application_topology
                else None
            )
            if replica_assignment:
                new_topic = NewTopic(topic, replica_assignments={0: replica_assignment})
            elif match_application_topology:
                replication_factor = self._resolve_replication_factor(admin)
                new_topic = NewTopic(topic, num_partitions=1, replication_factor=replication_factor)
            else:
                new_topic = NewTopic(topic, num_partitions=1, replication_factor=1)
            admin.create_topics([new_topic])
            return False
        except TopicAlreadyExistsError:
            return True
        except Exception:
            if match_application_topology:
                raise
            return False  # nosec B110 - functional probe: produce step will catch real failures
        finally:
            self._close_admin(admin)

    def _create_temp_ca_file(self, ca_content: str) -> None:
        """Write CA certificate content to a temporary PEM file."""
        if self._ca_file_path:
            return
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".pem") as ca_file:
            ca_file.write(ca_content)
            self._ca_file_path = ca_file.name

    def _remove_temp_ca_file(self) -> None:
        """Remove the temporary CA certificate file if it exists."""
        if self._ca_file_path:
            try:
                os.remove(self._ca_file_path)
            except OSError:
                pass
            self._ca_file_path = None

    def _close_admin(self, admin: KafkaAdminClient | None) -> None:
        if admin is not None:
            try:
                admin.close()
            except Exception:  # nosec B110 - best-effort cleanup
                pass

    def _close_consumer(self, consumer: KafkaConsumer | None) -> None:
        if consumer is not None:
            try:
                consumer.close()
            except Exception:  # nosec B110 - best-effort cleanup
                pass

    def _close_producer(self, producer: KafkaProducer | None) -> None:
        if producer is not None:
            try:
                producer.close(timeout=5)
            except Exception:  # nosec B110 - best-effort cleanup
                pass


class KafkaClientValidator(_KafkaConnectionMixin, BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role not in ("requires", "provides"):
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)
        if level == "deep":
            return self._validate_deep()
        return self._validate_simple()

    def _validate_simple(self) -> ValidationResult:
        """L1: Schema validation + Kafka consumer connectivity (list_topics)."""
        start_time = time.monotonic()
        checks: list[ValidationCheck] = []

        # --- 1. Remote app presence ---
        error_result = self._check_relation_exists("simple")
        if error_result:
            return error_result

        # --- 2. Resolve credentials ---
        source = self._connection_databag()
        creds = self._resolve_credentials(source)

        # --- 3. Schema check ---
        # consumer-group-prefix is only set by the requirer when it opts into the
        # "consumer" role, so it must not be treated as required (see
        # data_interfaces.py: KafkaProvidesData.set_consumer_group_prefix).
        schema_check = self.validate_schema(["endpoints", "topic", "username", "password"], creds, data=source)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="simple", checks=checks)

        data = source | creds

        # --- 4. Endpoint format check ---
        endpoint_check = self._check_bootstrap_servers(data["endpoints"])
        checks.append(endpoint_check)
        if not endpoint_check.passed:
            return self._make_result(level="simple", checks=checks)

        # --- 5. Connect via consumer and list topics ---
        consumer: KafkaConsumer | None = None
        try:
            consumer = self._build_consumer(data)
            topics = consumer.topics()
            checks.append(
                ValidationCheck(
                    name="connect",
                    passed=True,
                    message=f"Connected to Kafka. Found {len(topics)} accessible topic(s).",
                )
            )
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
        finally:
            self._close_consumer(consumer)
            self._remove_temp_ca_file()

        # --- 6. Latency check ---
        elapsed = time.monotonic() - start_time
        if elapsed > _SIMPLE_LATENCY_TARGET_S:
            checks.append(
                ValidationCheck(
                    name="latency",
                    passed=False,
                    message=f"Simple validation took {elapsed:.2f}s, exceeded {_SIMPLE_LATENCY_TARGET_S}s target.",
                )
            )
        else:
            checks.append(
                ValidationCheck(
                    name="latency",
                    passed=True,
                    message=f"Simple validation completed in {elapsed:.2f}s.",
                )
            )

        return self._make_result(level="simple", checks=checks)

    def _validate_deep(self) -> ValidationResult:
        """L2: Produce a canary message to the granted topic and consume it to verify."""
        start_time = time.monotonic()
        checks: list[ValidationCheck] = []

        # --- 1. Remote app presence ---
        error_result = self._check_relation_exists("deep")
        if error_result:
            return error_result

        # --- 2. Resolve credentials ---
        source = self._connection_databag()
        creds = self._resolve_credentials(source)

        # --- 3. Schema check ---
        # consumer-group-prefix is only set by the requirer when it opts into the
        # "consumer" role (see data_interfaces.py:
        # KafkaProvidesData.set_consumer_group_prefix), so "simple" leaves it
        # optional. Deep validation always consumes the canary message to
        # confirm the round trip, which requires a group covered by the
        # granted ACLs, so it is required here.
        schema_check = self.validate_schema(
            ["endpoints", "topic", "username", "password", "consumer-group-prefix"], creds, data=source
        )
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="deep", checks=checks)

        data = source | creds

        # --- 4. Endpoint format check ---
        endpoint_check = self._check_bootstrap_servers(data["endpoints"])
        checks.append(endpoint_check)
        if not endpoint_check.passed:
            return self._make_result(level="deep", checks=checks)

        topic = data["topic"]
        canary_value = f"validator-probe-{uuid.uuid4().hex[:12]}"
        canary_key = b"validator-canary"
        # Canary consumer group uses the granted prefix so ACLs permit READ.
        canary_group = f"{data['consumer-group-prefix']}probe-{uuid.uuid4().hex[:8]}"

        # --- 5. Ensure topic exists ---
        # kafka-k8s disables auto.create.topics.enable; the validator creates the
        # topic via AdminClient so it does not need operator intervention.
        self._ensure_topic_exists(data, topic)

        producer: KafkaProducer | None = None
        consumer: KafkaConsumer | None = None
        try:
            # --- 6. Produce canary message ---
            produce_succeeded = False
            try:
                producer = self._build_producer(data)
                future = producer.send(topic, key=canary_key, value=canary_value.encode())
                producer.flush(timeout=5)
                future.get(timeout=5)
                checks.append(
                    ValidationCheck(
                        name="produce",
                        passed=True,
                        message=f"Canary message produced to topic '{topic}'.",
                    )
                )
                produce_succeeded = True
            except Exception as exc:
                checks.append(ValidationCheck(name="produce", passed=False, message=str(exc)))

            if not produce_succeeded:
                return self._make_result(level="deep", checks=checks)

            # --- 7. Consume canary message ---
            try:
                consumer = self._build_consumer(data, group_id=canary_group, auto_offset_reset="earliest")
                consumer.subscribe([topic])
                consumed_value: str | None = None
                deadline = time.monotonic() + _CONSUME_TIMEOUT_S
                while time.monotonic() < deadline:
                    records = consumer.poll(timeout_ms=1000, max_records=50)
                    for msgs in records.values():
                        for msg in msgs:
                            if msg.value == canary_value.encode():
                                consumed_value = canary_value
                                break
                        if consumed_value:
                            break
                    if consumed_value:
                        break

                if consumed_value == canary_value:
                    checks.append(
                        ValidationCheck(
                            name="consume",
                            passed=True,
                            message="Canary message consumed and contents verified.",
                        )
                    )
                else:
                    checks.append(
                        ValidationCheck(
                            name="consume",
                            passed=False,
                            message=f"Canary message not found in topic '{topic}' within {_CONSUME_TIMEOUT_S:.0f}s.",
                        )
                    )
            except Exception as exc:
                checks.append(ValidationCheck(name="consume", passed=False, message=str(exc)))

        finally:
            self._close_producer(producer)
            self._close_consumer(consumer)
            self._remove_temp_ca_file()

        # --- 8. Latency check ---
        elapsed = time.monotonic() - start_time
        if elapsed > _DEEP_LATENCY_TARGET_S:
            checks.append(
                ValidationCheck(
                    name="latency",
                    passed=False,
                    message=f"Deep validation took {elapsed:.1f}s, exceeded {_DEEP_LATENCY_TARGET_S:.0f}s target.",
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

    def _check_bootstrap_servers(self, endpoints: str) -> ValidationCheck:
        """Validate each entry in the endpoints field is a valid host:port pair."""
        entries = [e.strip() for e in endpoints.split(",") if e.strip()]
        if not entries:
            return ValidationCheck(
                name="endpoints_format",
                passed=False,
                message="endpoints field is empty.",
            )
        invalid: list[str] = []
        for entry in entries:
            if ":" not in entry:
                invalid.append(entry)
                continue
            host, _, port_str = entry.rpartition(":")
            if not host or not port_str:
                invalid.append(entry)
                continue
            try:
                port = int(port_str)
                if not (1 <= port <= 65535):
                    invalid.append(entry)
            except ValueError:
                invalid.append(entry)
        if invalid:
            return ValidationCheck(
                name="endpoints_format",
                passed=False,
                message=f"Invalid endpoint entries: {', '.join(invalid)}",
            )
        return ValidationCheck(
            name="endpoints_format",
            passed=True,
            message=f"Validated {len(entries)} broker endpoint(s).",
        )

    def _check_relation_exists(self, level: ValidationLevel) -> ValidationResult | None:
        """Return an ERROR result if the remote app is absent, else None."""
        if not self.relation_exists():
            return self._make_result(
                status="ERROR",
                level=level,
                error=f"No remote application on relation '{self.endpoint}'.",
            )
        return None


class KafkaClientPersistenceValidator(_KafkaConnectionMixin, BasePersistenceValidator):
    """Reference-style persistence validator for kafka_client, modeled on
    ``PostgreSQLClientPersistenceValidator`` (see SQ103).

    Each validator instance owns a dedicated canary topic named
    ``validator_canary_{scope_token}_{identifier}``, where ``scope_token`` is a fixed-width hash of
    this model's UUID, relation ID and unit name (see ``_canary_topic_prefix``) and ``identifier``
    is a fixed-width, zero-padded value chosen by ``prepare()`` and carried forward by the caller
    (the test harness) as ``PersistenceState.id``. Every canary message is a small JSON payload
    carrying the random, unguessable ``token`` generated by ``prepare()`` (also carried forward as
    ``PersistenceState.token``) and a ``ref`` counter, so ``checkpoint()`` can detect data loss (or
    a topic silently recreated from scratch) by matching on ``token`` rather than trusting a bare
    message count that a coincidentally-sized but unrelated topic could satisfy.

    Persistence only applies to the requirer side of the relation (the side holding credentials to
    connect out); the provider side raises ``PersistenceNotApplicable``, mirroring the role check
    ``KafkaClientValidator.validate()`` performs for the functional probe.

    Unlike PostgreSQL (where a granted database privilege lets the client create arbitrary
    tables), Kafka ACLs granted by the provider are typically scoped to the single ``topic`` named
    in the relation request. Creating and deleting a separate canary topic therefore requires the
    relation's credentials to carry broader topic-management authority (e.g. the requirer charm
    was related with ``extra-user-roles=admin``) - this is a deployment requirement for exercising
    persistence, not something this validator can work around.
    """

    def prepare(self) -> PersistenceState:
        self._require_requires_role()
        # Masked to 63 bits so the canary topic name - which also carries a fixed-width scope
        # token - stays well within Kafka's 249-character topic name limit, while still leaving
        # far more entropy than a test run could collide on.
        identifier = uuid.uuid4().int & _MAX_CANARY_IDENTIFIER
        topic = self._canary_topic_name(identifier)
        # Random, unguessable per-run token written to every canary message and matched on by
        # checkpoint(). It must not be derivable from `identifier`/`ref`: those are reproducible,
        # so a backend that lost the canary data and recreated the topic from scratch would
        # reproduce the same value and pass falsely.
        token = uuid.uuid4().hex
        try:
            data = self._connection_data()
            topic_already_existed = self._ensure_topic_exists(data, topic, match_application_topology=True)
            if topic_already_existed:
                # The topic can only already exist here if an earlier prepare() call for this
                # same identifier created and wrote it, but its result never reached the caller
                # (e.g. the caller retried after an ambiguous failure). Blindly producing another
                # ref=1 message would leave two records at the same ref with different tokens,
                # which the next checkpoint()'s exact-set check would then reject as corruption -
                # so adopt that earlier call's already-written canary instead of duplicating it.
                reconciled_token = self._reconcile_existing_canary(data, topic)
                if reconciled_token is not None:
                    return PersistenceState(id=identifier, ref=1, token=reconciled_token)
            self._produce_canary_message(data, topic, token, 1)
        finally:
            # _build_kafka_client_kwargs() writes the TLS CA to a temp file and reuses it across
            # every client built above; remove it once this operation is done rather than leaking
            # a PEM file (and a stale cached path) to disk on every prepare() call.
            self._remove_temp_ca_file()
        return PersistenceState(id=identifier, ref=1, token=token)

    def _reconcile_existing_canary(self, data: dict[str, str], topic: str) -> str | None:
        """Return the token of a pre-existing, single, well-formed ref=1 record in *topic*.

        Only a topic holding **exactly one** record, with a valid (non-bool) ``ref`` of ``1``, is
        treated as a clean, adoptable canary from an earlier, ambiguously-acknowledged prepare()
        call for this same identifier. Anything else - no records, more than one, or a malformed
        ref - is not safely reconcilable (it could be genuine corruption, not just a retried
        write), so this returns ``None`` and the caller proceeds to write its own fresh canary.
        """
        records = self._read_canary_messages(data, topic)
        if records is None or len(records) != 1:
            return None
        record = records[0]
        if not isinstance(record, dict):
            return None
        ref = record.get("ref")
        token = record.get("token")
        if isinstance(ref, int) and not isinstance(ref, bool) and ref == 1 and isinstance(token, str) and token:
            return token
        return None

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        self._require_requires_role()
        # expected comes from --refs, a (possibly restored/malformed) PersistenceState rather than
        # a value prepare() just minted - validate it's in range before any read/write, so a
        # truncated/different identifier can't silently target the wrong topic.
        if not 0 <= expected.id <= _MAX_CANARY_IDENTIFIER:
            raise ValueError(f"expected.id {expected.id} is out of range (expected 0..{_MAX_CANARY_IDENTIFIER})")
        if expected.ref < 1:
            # prepare() always returns ref=1 and checkpoint() only ever advances it by 1, so a
            # restored/malformed PersistenceState with ref <= 0 can't have come from a real prior
            # run. Without this check, an empty or partially recreated topic (actual == 0) could
            # satisfy `actual == expected.ref` for ref=0 and report a false PASS.
            raise ValueError(f"expected.ref {expected.ref} is out of range (expected >= 1)")
        topic = self._canary_topic_name(expected.id)
        try:
            data = self._connection_data()
            records = self._read_canary_messages(data, topic)
            # `None` means the consume deadline was hit before every message up to end_offset was
            # read back - treat that as a definite, unverified FAIL rather than evaluating
            # whatever partial record set happened to arrive, which could coincidentally satisfy
            # the exact-ref-set check below and report a false PASS.
            incomplete_read = records is None
            if records is None:
                records = []
            # Require the exact multiset of refs 1..expected.ref tagged with our token, not just a
            # matching count: a topic dropped and recreated from scratch could otherwise
            # coincidentally satisfy a bare count-only check, and deduplicating refs here (e.g. via
            # a set) would let a topic containing a duplicate ref alongside a missing one still
            # equal expected_refs. Compared positionally (rather than via `list(range(1,
            # expected.ref + 1)) == matching_refs`) so verification cost is bounded by the number
            # of real records `_read_canary_messages` actually read back, not by an untrusted,
            # schema-valid but arbitrarily large `expected.ref` from --refs.
            #
            # Collect every same-token record first, then validate each `ref` is a real int
            # (excluding bool, which is an int subclass) - filtering malformed refs out up front
            # would let a same-token record with e.g. a string ref be silently ignored, rather than
            # correctly failing a check whose contract is "verify the exact tagged message set".
            same_token_records = [
                record for record in records if isinstance(record, dict) and record.get("token") == expected.token
            ]
            refs_are_valid = all(
                isinstance(record.get("ref"), int) and not isinstance(record.get("ref"), bool)
                for record in same_token_records
            )
            if incomplete_read:
                matching = len(same_token_records)
                passed = False
                next_ref_already_written = False
            elif refs_are_valid:
                # Bounded to [1, expected.ref] for the *sequence* check: the producer call below
                # writes expected.ref + 1 *before* this method's result/state reach the caller, so
                # if future.get() succeeds but the process or caller is interrupted before
                # receiving that result, a retried checkpoint() is invoked with the same
                # (unadvanced) `expected` while the topic already contains that extra,
                # ambiguously-acknowledged message. Tolerating exactly one record at
                # expected.ref + 1 (mirroring the PostgreSQL/Cassandra reference implementations'
                # `checkpoint_ref BETWEEN 1 AND %s`, plus this single named exception) makes that
                # retry idempotent instead of permanently failing every subsequent attempt.
                #
                # Everything else out of [1, expected.ref] - ref <= 0, a second (duplicate) copy of
                # expected.ref + 1, or any ref beyond expected.ref + 1 - is real corruption, not an
                # ambiguous-retry artifact, and must still fail: silently discarding it here (as an
                # unconditional `1 <= ref <= expected.ref` bound would) let an extra record beyond
                # the tolerated exception produce a false PASS.
                in_range_refs = sorted(
                    record["ref"] for record in same_token_records if 1 <= record["ref"] <= expected.ref
                )
                next_ref_records = [record for record in same_token_records if record["ref"] == expected.ref + 1]
                unexpected_records = [
                    record
                    for record in same_token_records
                    if not (1 <= record["ref"] <= expected.ref) and record["ref"] != expected.ref + 1
                ]
                matching = len(in_range_refs)
                sequence_intact = matching == expected.ref and all(
                    ref == index + 1 for index, ref in enumerate(in_range_refs)
                )
                passed = sequence_intact and len(next_ref_records) <= 1 and not unexpected_records
                # A prior, ambiguously-acknowledged attempt may have already written the next
                # message this call is about to produce - detected without needing any durable,
                # external idempotency tracking, since this ref is already tagged with our own
                # unguessable per-run token. Re-producing it would leave a second, duplicate
                # message at the same ref, which the *next* real checkpoint's exact-set/sequence
                # check would then reject as corruption.
                next_ref_already_written = passed and bool(next_ref_records)
            else:
                matching = len(same_token_records)
                passed = False
                next_ref_already_written = False

            # Only write the next canary message when this checkpoint passed: ValidatorRunner
            # only carries the advanced PersistenceState forward on a PASS result, so writing here
            # unconditionally would grow `actual` past what the harness will ever compare against
            # again, masking the mismatch behind permanent drift.
            if passed and not next_ref_already_written:
                self._produce_canary_message(data, topic, expected.token, expected.ref + 1)
        finally:
            # Same rationale as prepare(): remove the temp CA file built for this call's clients
            # rather than leaking a PEM file to disk on every checkpoint() invocation.
            self._remove_temp_ca_file()

        check = ValidationCheck(
            name="message_count",
            passed=passed,
            message=(
                f"Found expected {matching} marked message(s) in topic '{topic}'."
                if passed
                else (
                    f"Timed out reading topic '{topic}' before confirming every message was "
                    "consumed; cannot verify the exact canary message set."
                    if incomplete_read
                    else (
                        f"Expected {expected.ref} marked message(s) with matching token in topic "
                        f"'{topic}', found {matching}. Data may have been lost, or the topic was "
                        "recreated without the original canary messages."
                    )
                )
            ),
        )
        result = self._make_result(level="deep", checks=[check])
        new_state = PersistenceState(id=expected.id, ref=expected.ref + 1, token=expected.token) if passed else expected
        return result, new_state

    def cleanup(self) -> None:
        """Delete every canary topic this validator instance (or a prior instance of it) created.

        ``cleanup()`` takes no state argument (see ``BasePersistenceValidator.cleanup``), so every
        topic matching this instance's canary name pattern is discovered via ``list_topics()`` and
        deleted, rather than dropping one topic by identifier. This also mops up a topic left
        behind by an interrupted run (e.g. a crash between ``prepare()`` and the next ``cleanup()``).

        Discovery is scoped to a model+relation+unit namespace (see ``_canary_topic_prefix``) so
        concurrent relations sharing a cluster can't drop each other's topics. It does not sweep
        up a stray topic from a relation removed and re-added under a new ID - an accepted
        trade-off, since a fresh ``prepare()`` for the new ID starts its own topic anyway.

        ``list_topics()`` only narrows candidates by name, so every candidate is re-checked against
        ``_canary_topic_regex()`` and ``_MAX_CANARY_IDENTIFIER`` before being deleted. This rejects
        a same-prefixed but unrelated topic that a bare prefix match would otherwise destroy.
        """
        self._require_requires_role()
        # Incomplete credentials mean cleanup can't run: raise PersistenceNotApplicable so the
        # runner records a skip (not a successful cleanup) and keeps the tracked state, rather than
        # forgetting orphaned canary data.
        creds = self._resolve_credentials(self.databag)
        if not self.validate_schema(["endpoints", "username", "password"], creds).passed:
            raise PersistenceNotApplicable(
                "Relation credentials are incomplete; cleanup cannot remove canary data yet."
            )
        data = self.databag | creds
        admin: KafkaAdminClient | None = None
        try:
            admin = self._build_admin_client(data)
            topic_names = admin.list_topics()
            name_regex = self._canary_topic_regex()
            to_delete = []
            for name in topic_names:
                match = name_regex.fullmatch(name)
                if not match or int(match.group("identifier")) > _MAX_CANARY_IDENTIFIER:
                    continue
                to_delete.append(name)
            # Deleted one topic per call, not as a single batch: `KafkaAdminClient.delete_topics()`
            # raises on the first non-NoError, non-ignored per-topic error it finds in the response
            # (raise_errors=True by default), which means a batch call stops reporting as soon as it
            # hits one topic's error - an `UnknownTopicOrPartitionError` for an already-gone topic
            # would otherwise be swallowed here and silently mask a genuine deletion failure on a
            # different topic in the same batch, leaving it behind with cleanup() reporting success.
            for name in to_delete:
                try:
                    admin.delete_topics([name])
                except UnknownTopicOrPartitionError:
                    # Already gone (e.g. a concurrent cleanup, or manual removal) - not an error.
                    pass
        finally:
            self._close_admin(admin)
            # Same rationale as prepare()/checkpoint(): remove the temp CA file built for this
            # call's admin client rather than leaking a PEM file to disk on every cleanup() call.
            self._remove_temp_ca_file()

    def _require_requires_role(self) -> None:
        if self.role != "requires":
            raise PersistenceNotApplicable(f"Role '{self.role}' is not supported by {self.__class__.__name__}.")

    def _connection_data(self) -> dict[str, str]:
        """Resolve and validate the connection fields needed to build a Kafka client.

        Unlike the functional validator's validate()/deep(), these methods have no ValidationCheck
        to report a schema failure through, so a missing credential is raised rather than reaching
        a kafka-python client constructor with incomplete kwargs.
        """
        creds = self._resolve_credentials(self.databag)
        data = self.databag | creds
        schema_check = self.validate_schema(["endpoints", "username", "password"], creds)
        if not schema_check.passed:
            raise RuntimeError(f"Cannot connect for {self.endpoint}: {schema_check.message}")
        return data

    def _produce_canary_message(self, data: dict[str, str], topic: str, token: str, ref: int) -> None:
        producer: KafkaProducer | None = None
        try:
            producer = self._build_producer(data, acks="all", enable_idempotence=True)
            value = json.dumps({"token": token, "ref": ref}).encode()
            # Key must be unique per (token, ref): a constant key would let a broker/topic
            # configured with cleanup.policy=compact collapse every canary message down to just
            # the latest one, silently discarding the earlier refs checkpoint() needs to verify.
            key = f"{token}:{ref}".encode()
            future = producer.send(topic, key=key, value=value)
            producer.flush(timeout=5)
            future.get(timeout=5)
        finally:
            self._close_producer(producer)

    def _read_canary_messages(self, data: dict[str, str], topic: str) -> list[dict[str, Any]] | None:
        """Read every message currently in the canary topic's single partition.

        Uses manual partition assignment (``assign()``/``seek_to_beginning()``) rather than
        ``subscribe()`` with a consumer group, so reading does not depend on a consumer group or
        the ``consumer-group-prefix`` ACL grant the functional validator's round trip needs.

        Returns ``None`` (rather than whatever records were collected so far) if the consume
        deadline is hit before ``position()`` reaches ``end_offset``: without this, a slow/stalled
        broker that delivers only an expected tagged prefix before stalling would let checkpoint()
        accept a partial read and report a false PASS, instead of correctly treating an unverified
        tail of the topic as inconclusive.
        """
        consumer: KafkaConsumer | None = None
        try:
            consumer = self._build_raw_consumer(data)
            topic_partition = TopicPartition(topic, 0)
            consumer.assign([topic_partition])
            try:
                end_offsets = consumer.end_offsets([topic_partition])
            except UnknownTopicOrPartitionError:
                return []
            end_offset = end_offsets.get(topic_partition, 0)
            if end_offset == 0:
                return []
            consumer.seek_to_beginning(topic_partition)
            records: list[dict[str, Any]] = []
            deadline = time.monotonic() + _CONSUME_TIMEOUT_S
            while consumer.position(topic_partition) < end_offset:
                if time.monotonic() >= deadline:
                    return None
                batches = consumer.poll(timeout_ms=1000, max_records=200)
                for messages in batches.values():
                    for message in messages:
                        if message.value is None:
                            continue  # Tombstone record (compaction marker); not a canary message.
                        try:
                            records.append(json.loads(message.value.decode()))
                        except (ValueError, UnicodeDecodeError, AttributeError):
                            continue  # Not one of our canary messages; ignore.
            return records
        finally:
            self._close_consumer(consumer)

    def _canary_topic_prefix(self) -> str:
        """Prefix scoped to this model, relation and unit, so cleanup discovery can't cross boundaries.

        ``self.relation_id`` is stable for the lifetime of a given relation, but relation IDs are
        assigned independently per model and can collide numerically across two different models
        relating to the same cluster. The unit name is also part of the scope: the runner runs
        persistence validators on *every* unit of the application, so two units share both
        ``model.uuid`` and ``relation_id`` while owning separate canary topics (see ``cleanup()``).
        All three are folded into a single fixed-width ``scope_token`` - the first 16 hex characters
        (64 bits) of a SHA-256 hash of ``f"{model_uuid}:{relation_id}:{unit_name}"`` - so the name
        stays well within Kafka's topic name limit and ``cleanup()`` can validate its exact shape
        via ``_canary_topic_regex()``.
        """
        scope_token = self._canary_scope_token()
        return f"{_CANARY_TOPIC_PREFIX}{scope_token}_"

    def _canary_scope_token(self) -> str:
        unit_name = self.charm.model.unit.name
        digest_input = f"{self.charm.model.uuid}:{self.relation_id}:{unit_name}".encode()
        return hashlib.sha256(digest_input).hexdigest()[:16]

    def _canary_topic_regex(self) -> "re.Pattern[str]":
        """Exact-shape match for this relation's canary topics: prefix + fixed-width digits.

        Used by ``cleanup()`` to reject a topic that merely shares the discovery prefix but
        doesn't match the fixed-width zero-padded identifier suffix ``_canary_topic_name()``
        always produces. Matching this shape alone isn't sufficient - see ``cleanup()``, which
        also checks the captured ``identifier`` against ``_MAX_CANARY_IDENTIFIER``.
        """
        return re.compile(re.escape(self._canary_topic_prefix()) + r"(?P<identifier>[0-9]{20})")

    def _canary_topic_name(self, identifier: int) -> str:
        # Zero-padded to a fixed 20 digits (prepare() masks identifiers to 63 bits, so never more
        # than 19) so every canary name has the same shape, which _canary_topic_regex() relies on.
        # checkpoint() passes back an identifier from a possibly restored/malformed state, so
        # range-check it here too rather than letting it silently address the wrong topic.
        if not 0 <= identifier <= _MAX_CANARY_IDENTIFIER:
            raise ValueError(f"canary identifier {identifier} is out of range (expected 0..{_MAX_CANARY_IDENTIFIER})")
        return f"{self._canary_topic_prefix()}{identifier:020d}"
