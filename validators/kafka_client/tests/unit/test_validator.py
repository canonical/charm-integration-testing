# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import patch

import ops
import pytest
from kafka import TopicPartition  # type: ignore[import-untyped]
from kafka.errors import (  # type: ignore[import-untyped]
    KafkaError,
    TopicAlreadyExistsError,
    TopicAuthorizationFailedError,
    UnknownTopicOrPartitionError,
)
from pydantic import ValidationError

from validators.base import PersistenceNotApplicable, PersistenceState
from validators.kafka_client.validator import (
    _MAX_CANARY_IDENTIFIER,
    KafkaClientPersistenceValidator,
    KafkaClientValidator,
)
from validators.test_utils.helpers import make_charm_from_relation, make_charm_from_relation_and_secrets
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _make_validator(
    databag: dict[str, str],
    endpoint: str = "kafka",
    role: RelationRoleStub = RelationRoleStub.requires,
    remote_extra: dict[str, str] | None = None,
) -> KafkaClientValidator:
    remote_app = ApplicationStub(name="remote-app")
    # On the provider role, kafka_client fields live on the *local* app databag
    # (charm.app), not the remote one, so seed it there instead.
    remote_databag = (remote_extra or {}) if role == RelationRoleStub.provides else databag
    relation = RelationStub(name=endpoint, id=0, app=remote_app, data={remote_app: remote_databag})
    charm_stub = make_charm_from_relation(relation, interface_name="kafka_client", role=role)
    if role == RelationRoleStub.provides:
        relation.data[charm_stub.app] = databag
    charm = cast(ops.CharmBase, charm_stub)
    return KafkaClientValidator(charm, cast(ops.Relation, relation))


# Arbitrary non-empty token used by checkpoint() tests; prepare() generates a random one per run.
TEST_TOKEN = "test-token-abc123"

# Scope token produced by the default relation_id=0, model_uuid, unit_name below - computed the
# same way PostgreSQLClientPersistenceValidator's tests hardcode theirs (same inputs, same hash).
TEST_SCOPE_TOKEN = "da7d88bc9ad4d4fd"

PERSISTENCE_VALID_DATABAG: dict[str, str] = {
    "endpoints": "10.1.2.3:9092,10.1.2.4:9092",
    "username": "kafka-user",
    "password": "s3cr3t",
}


def _make_persistence_validator(
    databag: dict[str, str],
    endpoint: str = "kafka",
    role: RelationRoleStub = RelationRoleStub.requires,
    relation_id: int = 0,
    model_uuid: str = "11111111-1111-1111-1111-111111111111",
    unit_name: str = "app/0",
) -> KafkaClientPersistenceValidator:
    remote_app = ApplicationStub(name="remote-app")
    relation = RelationStub(name=endpoint, id=relation_id, app=remote_app, data={remote_app: databag})
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(
            relation,
            interface_name="kafka_client",
            role=role,
            local_model_uuid=model_uuid,
            local_unit_name=unit_name,
        ),
    )
    return KafkaClientPersistenceValidator(charm, cast(ops.Relation, relation))


@dataclass
class FutureStub:
    """Minimal stand-in for kafka FutureRecordMetadata; raises send_error if set."""

    send_error: Exception | None = None

    def get(self, timeout: float | None = None) -> None:
        if self.send_error:
            raise self.send_error


@dataclass
class ConsumerRecordStub:
    """Minimal stand-in for kafka ConsumerRecord."""

    value: bytes | None = None


@dataclass
class KafkaConsumerStub:
    """Minimal stand-in for kafka.KafkaConsumer."""

    topics_result: set[str] = field(default_factory=set)
    topics_error: Exception | None = None
    poll_batches: list[dict[Any, list[ConsumerRecordStub]]] = field(default_factory=list)
    poll_call_count: int = field(default=0, init=False, repr=False)
    subscribe_calls: list[list[str]] = field(default_factory=list)

    def topics(self) -> set[str]:
        if self.topics_error:
            raise self.topics_error
        return self.topics_result

    def subscribe(self, topics: list[str]) -> None:
        self.subscribe_calls.append(topics)

    def poll(self, timeout_ms: int = 0, max_records: int | None = None) -> dict[Any, list[ConsumerRecordStub]]:
        if self.poll_call_count < len(self.poll_batches):
            batch = self.poll_batches[self.poll_call_count]
            self.poll_call_count += 1
            return batch
        self.poll_call_count += 1
        return {}

    def close(self) -> None:
        pass


@dataclass
class KafkaAdminClientStub:
    """Minimal stand-in for kafka.admin.KafkaAdminClient."""

    create_error: Exception | None = None
    broker_count: int = 1
    created_topics: list[Any] = field(default_factory=list, init=False, repr=False)

    def create_topics(self, new_topics: list[Any]) -> dict[str, Any]:
        if self.create_error:
            raise self.create_error
        self.created_topics.extend(new_topics)
        return {}

    def describe_cluster(self) -> dict[str, Any]:
        return {"brokers": [{"node_id": i} for i in range(self.broker_count)]}

    def describe_topics(self, topics: list[str]) -> list[dict[str, Any]]:
        # Simulates the application topic not (yet) being describable, so callers fall back to
        # the broker-count heuristic - matching tests written before describe_topics() existed.
        return []

    def close(self) -> None:
        pass


@dataclass
class KafkaProducerStub:
    """Minimal stand-in for kafka.KafkaProducer."""

    future: FutureStub = field(default_factory=FutureStub)
    send_error: Exception | None = None
    flush_error: Exception | None = None
    sent: list[tuple[str, bytes | None, bytes | None]] = field(default_factory=list, init=False, repr=False)

    def send(self, topic: str, key: bytes | None = None, value: bytes | None = None) -> FutureStub:
        if self.send_error:
            raise self.send_error
        self.sent.append((topic, key, value))
        return self.future

    def flush(self, timeout: float | None = None) -> None:
        if self.flush_error:
            raise self.flush_error

    def close(self, timeout: float | None = None) -> None:
        pass


@dataclass
class PersistenceKafkaAdminClientStub:
    """Minimal stand-in for kafka.admin.KafkaAdminClient used by persistence prepare()/cleanup()."""

    topics: list[str] = field(default_factory=list)
    create_error: Exception | None = None
    list_error: Exception | None = None
    delete_error: Exception | None = None
    delete_errors_by_topic: dict[str, Exception] = field(default_factory=dict)
    broker_count: int = 1
    describe_cluster_error: Exception | None = None
    application_topic_replica_count: int | None = None
    application_topic_partition_count: int = 1
    deleted: list[list[str]] = field(default_factory=list, init=False, repr=False)
    created_replication_factors: list[int] = field(default_factory=list, init=False, repr=False)
    created_replica_assignments: list[dict[int, list[int]]] = field(default_factory=list, init=False, repr=False)

    def create_topics(self, new_topics: list[Any]) -> dict[str, Any]:
        if self.create_error:
            raise self.create_error
        for new_topic in new_topics:
            self.created_replication_factors.append(new_topic.replication_factor)
            self.created_replica_assignments.append(new_topic.replica_assignments)
            if new_topic.name not in self.topics:
                self.topics.append(new_topic.name)
        return {}

    def describe_cluster(self) -> dict[str, Any]:
        if self.describe_cluster_error:
            raise self.describe_cluster_error
        return {"brokers": [{"node_id": i} for i in range(self.broker_count)]}

    def describe_topics(self, topics: list[str]) -> list[dict[str, Any]]:
        # None (the default) simulates the application topic not being describable - e.g. not
        # yet created - so callers fall back to the broker-count heuristic.
        if self.application_topic_replica_count is None:
            return []
        return [
            {
                "name": topics[0],
                "partitions": [
                    {
                        "partition_index": partition_index,
                        "replica_nodes": list(range(self.application_topic_replica_count)),
                    }
                    for partition_index in range(self.application_topic_partition_count)
                ],
            }
        ]

    def list_topics(self) -> list[str]:
        if self.list_error:
            raise self.list_error
        return list(self.topics)

    def delete_topics(self, topics: list[str]) -> dict[str, Any]:
        if self.delete_error:
            raise self.delete_error
        for name in topics:
            if name in self.delete_errors_by_topic:
                raise self.delete_errors_by_topic[name]
        self.deleted.append(list(topics))
        self.topics = [t for t in self.topics if t not in topics]
        return {}

    def close(self) -> None:
        pass


@dataclass
class PersistenceKafkaConsumerStub:
    """Minimal stand-in for kafka.KafkaConsumer used by checkpoint()'s canary reads.

    Unlike KafkaConsumerStub (used by the functional deep validator's subscribe()-based round
    trip), this mimics the manual assign()/seek_to_beginning()/position() flow
    KafkaClientPersistenceValidator._read_canary_messages uses to read a canary topic without a
    consumer group.
    """

    records_by_topic: dict[str, list[ConsumerRecordStub]] = field(default_factory=dict)
    missing_topics: set[str] = field(default_factory=set)
    # When True, poll() never advances position() past 0 - simulating a broker that stalls after
    # delivering nothing (or only a partial prefix), so the read deadline is hit before
    # end_offset is reached.
    stall: bool = False
    assigned: list[TopicPartition] = field(default_factory=list, init=False, repr=False)
    _position: int = field(default=0, init=False, repr=False)
    _delivered: bool = field(default=False, init=False, repr=False)

    def assign(self, partitions: list[TopicPartition]) -> None:
        self.assigned = partitions

    def end_offsets(self, partitions: list[TopicPartition]) -> dict[TopicPartition, int]:
        tp = partitions[0]
        if tp.topic in self.missing_topics:
            raise UnknownTopicOrPartitionError()
        return {tp: len(self.records_by_topic.get(tp.topic, []))}

    def seek_to_beginning(self, *partitions: TopicPartition) -> None:
        self._position = 0

    def position(self, partition: TopicPartition) -> int:
        return self._position

    def poll(self, timeout_ms: int = 0, max_records: int | None = None) -> dict[TopicPartition, list[Any]]:
        if self.stall:
            return {}
        if self._delivered:
            return {}
        self._delivered = True
        tp = self.assigned[0]
        records = self.records_by_topic.get(tp.topic, [])
        self._position = len(records)
        return {tp: records}

    def close(self) -> None:
        pass


def _canary_record(token: str, ref: int) -> ConsumerRecordStub:
    return ConsumerRecordStub(value=json.dumps({"token": token, "ref": ref}).encode())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_DATABAG: dict[str, str] = {
    "endpoints": "10.1.2.3:9092,10.1.2.4:9092",
    "topic": "my-topic",
    "consumer-group-prefix": "relation-8-",
    "username": "kafka-user",
    "password": "s3cr3t",
}

# ---------------------------------------------------------------------------
# Tests — simple level
# ---------------------------------------------------------------------------


class TestKafkaClientValidatorSimple:
    def test_returns_skipped_for_unsupported_level(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    @pytest.mark.parametrize(
        "role,should_skip",
        [
            (RelationRoleStub.requires, False),
            (RelationRoleStub.provides, False),
            (RelationRoleStub.peer, True),
        ],
    )
    def test_skips_based_on_role(self, role: RelationRoleStub, should_skip: bool) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, role=role)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert (result.status == "SKIPPED") == should_skip

    def test_fails_schema_check_when_required_fields_missing(self) -> None:
        # GIVEN a completely empty databag
        validator = _make_validator({})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "endpoints" in schema_check.message
        assert "topic" in schema_check.message
        assert "username" in schema_check.message
        assert "password" in schema_check.message

    def test_schema_check_passes_without_consumer_group_prefix(self) -> None:
        # GIVEN consumer-group-prefix is absent, as it is optional and only set
        # when the requirer opts into the "consumer" role.
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "consumer-group-prefix"}
        validator = _make_validator(databag)
        consumer_stub = KafkaConsumerStub(topics_result={"my-topic"})

        # WHEN
        with patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer_stub):
            result = validator.validate(level="simple")

        # THEN
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed

    @pytest.mark.parametrize(
        "bad_value,description",
        [
            ("10.1.2.3", "missing port"),
            ("10.1.2.3:notaport", "non-numeric port"),
            ("10.1.2.3:0", "port zero"),
            ("10.1.2.3:99999", "port out of range"),
            (":9092", "missing host"),
        ],
    )
    def test_fails_bootstrap_server_format_check(self, bad_value: str, description: str) -> None:
        # GIVEN a databag with a non-empty but structurally invalid endpoints value
        databag = {**VALID_DATABAG, "endpoints": bad_value}
        validator = _make_validator(databag)

        # WHEN
        result = validator.validate(level="simple")

        # THEN the endpoints_format check is present and failed
        assert result.status == "FAIL", f"Expected FAIL for {description}"
        bs_check = next(c for c in result.checks if c.name == "endpoints_format")
        assert not bs_check.passed

    def test_fails_schema_check_when_bootstrap_server_empty(self) -> None:
        # GIVEN a databag where endpoints is an empty string
        # An empty value is caught by validate_schema before the format check runs.
        databag = {**VALID_DATABAG, "endpoints": ""}
        validator = _make_validator(databag)

        # WHEN
        result = validator.validate(level="simple")

        # THEN the schema check fails (empty string treated as missing)
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "endpoints" in schema_check.message

    def test_passes_bootstrap_server_with_single_valid_entry(self) -> None:
        # GIVEN a databag with one valid endpoint
        databag = {**VALID_DATABAG, "endpoints": "kafka.example.com:9093"}
        validator = _make_validator(databag)
        consumer_stub = KafkaConsumerStub(topics_result={"my-topic"})

        with patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer_stub):
            result = validator.validate(level="simple")

        bs_check = next(c for c in result.checks if c.name == "endpoints_format")
        assert bs_check.passed
        assert "1 broker endpoint" in bs_check.message

    def test_passes_with_all_required_fields(self) -> None:
        # GIVEN a complete databag and a successful Kafka connection
        validator = _make_validator(VALID_DATABAG)
        consumer_stub = KafkaConsumerStub(topics_result={"my-topic", "other-topic"})

        with patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer_stub):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert connect_check.passed
        assert "2" in connect_check.message

    def test_fails_connect_check_when_kafka_unreachable(self) -> None:
        # GIVEN a complete databag but Kafka refuses the connection
        validator = _make_validator(VALID_DATABAG)
        consumer_stub = KafkaConsumerStub(topics_error=Exception("Connection refused"))

        with patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer_stub):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "Connection refused" in connect_check.message

    def test_fails_when_consumer_constructor_raises(self) -> None:
        # GIVEN a complete databag but the KafkaConsumer constructor raises
        validator = _make_validator(VALID_DATABAG)

        with patch(
            "validators.kafka_client.validator.KafkaConsumer",
            side_effect=Exception("NoBrokersAvailable"),
        ):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        # GIVEN
        validator = _make_validator(VALID_DATABAG, endpoint="my-kafka")
        consumer_stub = KafkaConsumerStub(topics_result={"my-topic"})

        with patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer_stub):
            result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-kafka"
        assert result.interface == "kafka_client"

    def test_includes_latency_check(self) -> None:
        # GIVEN a successful connection
        validator = _make_validator(VALID_DATABAG)
        consumer_stub = KafkaConsumerStub(topics_result={"my-topic"})

        with patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer_stub):
            result = validator.validate(level="simple")

        # THEN a latency check is always present
        latency_check = next(c for c in result.checks if c.name == "latency")
        assert latency_check is not None


# ---------------------------------------------------------------------------
# Tests — deep level
# ---------------------------------------------------------------------------


class TestKafkaClientValidatorDeep:
    def test_fails_schema_check_when_required_fields_missing(self) -> None:
        # GIVEN an empty databag
        validator = _make_validator({})

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed

    def test_fails_schema_check_when_consumer_group_prefix_missing(self) -> None:
        # GIVEN consumer-group-prefix is absent. Deep validation always consumes
        # the canary message, which needs a group covered by the granted ACLs,
        # so (unlike "simple") this field is required for "deep".
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "consumer-group-prefix"}
        validator = _make_validator(databag)

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "consumer-group-prefix" in schema_check.message

    def test_fails_bootstrap_server_format_check_in_deep(self) -> None:
        # GIVEN an invalid endpoints value
        databag = {**VALID_DATABAG, "endpoints": "not-valid"}
        validator = _make_validator(databag)

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        bs_check = next(c for c in result.checks if c.name == "endpoints_format")
        assert not bs_check.passed

    def test_creates_the_probe_topic_with_a_single_replica_regardless_of_cluster_size(self) -> None:
        # GIVEN a multi-broker cluster. Regression test for: the persistence validator's canary
        # topic creation mirrors the application topic's own replica count/placement (see
        # _resolve_canary_replica_assignment / _resolve_replication_factor), but this shared
        # helper is also used by the functional validator's own probe topic here - which must keep
        # its original single-replica creation policy regardless of cluster size or application
        # topic replication, since it's unrelated to persistence verification.
        validator = _make_validator(VALID_DATABAG)
        admin = KafkaAdminClientStub(broker_count=3)
        producer_stub = KafkaProducerStub()
        consumer_stub = KafkaConsumerStub()
        consumer_stub.poll_batches = [{"tp": [ConsumerRecordStub(value=b"validator-canary")]}]

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer_stub),
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer_stub),
        ):
            # WHEN
            validator.validate(level="deep")

        # THEN
        assert len(admin.created_topics) == 1
        assert admin.created_topics[0].replication_factor == 1

    def test_passes_when_canary_message_produced_and_consumed(self) -> None:
        # GIVEN a complete databag, a producer that sends successfully, and a consumer
        # that returns the exact canary value on first poll.
        validator = _make_validator(VALID_DATABAG)

        # The canary value is generated inside the validator, so we capture it via a
        # side-effect that inspects what was sent to the producer.
        captured_value: list[bytes] = []

        class CapturingProducerStub(KafkaProducerStub):
            def send(self, topic: str, key: bytes | None = None, value: bytes | None = None) -> FutureStub:
                if value:
                    captured_value.append(value)
                return self.future

        producer_stub = CapturingProducerStub()
        consumer_stub = KafkaConsumerStub()

        def make_consumer(**kwargs: Any) -> KafkaConsumerStub:
            # Return a consumer whose first poll batch contains the captured canary.
            if captured_value:
                record = ConsumerRecordStub(value=captured_value[0])
                consumer_stub.poll_batches = [{"tp": [record]}]
            return consumer_stub

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=KafkaAdminClientStub()),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer_stub),
            patch("validators.kafka_client.validator.KafkaConsumer", side_effect=make_consumer),
        ):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "PASS"
        produce_check = next(c for c in result.checks if c.name == "produce")
        assert produce_check.passed
        consume_check = next(c for c in result.checks if c.name == "consume")
        assert consume_check.passed
        latency_check = next(c for c in result.checks if c.name == "latency")
        assert latency_check.passed

    def test_fails_when_producer_constructor_raises(self) -> None:
        # GIVEN the KafkaProducer constructor raises immediately
        validator = _make_validator(VALID_DATABAG)

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=KafkaAdminClientStub()),
            patch(
                "validators.kafka_client.validator.KafkaProducer",
                side_effect=Exception("NoBrokersAvailable"),
            ),
        ):
            result = validator.validate(level="deep")

        # THEN produce check fails and we stop before consume
        assert result.status == "FAIL"
        produce_check = next(c for c in result.checks if c.name == "produce")
        assert not produce_check.passed
        assert not any(c.name == "consume" for c in result.checks)

    def test_fails_when_send_future_raises(self) -> None:
        # GIVEN send() returns a future whose get() raises
        validator = _make_validator(VALID_DATABAG)
        producer_stub = KafkaProducerStub(future=FutureStub(send_error=Exception("produce error")))

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=KafkaAdminClientStub()),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer_stub),
        ):
            result = validator.validate(level="deep")

        # THEN produce check fails and consume is skipped
        assert result.status == "FAIL"
        produce_check = next(c for c in result.checks if c.name == "produce")
        assert not produce_check.passed
        assert "produce error" in produce_check.message
        assert not any(c.name == "consume" for c in result.checks)

    def test_fails_when_canary_message_not_found_within_timeout(self) -> None:
        # GIVEN a producer that succeeds but a consumer that always returns empty polls
        validator = _make_validator(VALID_DATABAG)
        producer_stub = KafkaProducerStub()
        consumer_stub = KafkaConsumerStub(poll_batches=[])  # always empty

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=KafkaAdminClientStub()),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer_stub),
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer_stub),
            patch("validators.kafka_client.validator._CONSUME_TIMEOUT_S", 0.1),
        ):
            result = validator.validate(level="deep")

        # THEN consume check fails
        assert result.status == "FAIL"
        consume_check = next(c for c in result.checks if c.name == "consume")
        assert not consume_check.passed
        assert "not found" in consume_check.message

    def test_fails_when_consumer_poll_raises(self) -> None:
        # GIVEN a producer that succeeds but consumer.poll() raises
        validator = _make_validator(VALID_DATABAG)
        producer_stub = KafkaProducerStub()

        class RaisingConsumerStub(KafkaConsumerStub):
            def poll(self, timeout_ms: int = 0, max_records: int | None = None) -> dict[Any, list[ConsumerRecordStub]]:
                raise Exception("poll error")

        consumer_stub = RaisingConsumerStub()

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=KafkaAdminClientStub()),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer_stub),
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer_stub),
        ):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        consume_check = next(c for c in result.checks if c.name == "consume")
        assert not consume_check.passed
        assert "poll error" in consume_check.message

    def test_uses_unique_consumer_group_for_canary_probe(self) -> None:
        # GIVEN a successful produce + consume
        validator = _make_validator(VALID_DATABAG)
        captured_group: list[str] = []
        canary_value_ref: list[bytes] = []

        class TrackingProducerStub(KafkaProducerStub):
            def send(self, topic: str, key: bytes | None = None, value: bytes | None = None) -> FutureStub:
                if value:
                    canary_value_ref.append(value)
                return self.future

        def make_consumer(**kwargs: Any) -> KafkaConsumerStub:
            captured_group.append(kwargs.get("group_id", ""))
            record = ConsumerRecordStub(value=canary_value_ref[0] if canary_value_ref else None)
            return KafkaConsumerStub(poll_batches=[{"tp": [record]}])

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=KafkaAdminClientStub()),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=TrackingProducerStub()),
            patch("validators.kafka_client.validator.KafkaConsumer", side_effect=make_consumer),
        ):
            validator.validate(level="deep")

        # THEN the consumer group used for the probe starts with the relation's prefix
        assert captured_group, "Expected at least one consumer to be created"
        assert captured_group[0].startswith(VALID_DATABAG["consumer-group-prefix"])
        assert "probe" in captured_group[0]

    def test_includes_latency_check(self) -> None:
        # GIVEN a successful produce + consume
        validator = _make_validator(VALID_DATABAG)
        canary_ref: list[bytes] = []

        class CapturingProducer(KafkaProducerStub):
            def send(self, topic: str, key: bytes | None = None, value: bytes | None = None) -> FutureStub:
                if value:
                    canary_ref.append(value)
                return self.future

        def make_consumer(**kwargs: Any) -> KafkaConsumerStub:
            record = ConsumerRecordStub(value=canary_ref[0] if canary_ref else None)
            return KafkaConsumerStub(poll_batches=[{"tp": [record]}])

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=KafkaAdminClientStub()),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=CapturingProducer()),
            patch("validators.kafka_client.validator.KafkaConsumer", side_effect=make_consumer),
        ):
            result = validator.validate(level="deep")

        latency_check = next(c for c in result.checks if c.name == "latency")
        assert latency_check is not None
        assert latency_check.passed


# ---------------------------------------------------------------------------
# Tests — provider role (fields live on the local app databag)
# ---------------------------------------------------------------------------


class TestKafkaClientValidatorProvidesSimple:
    def test_passes_with_all_required_fields(self) -> None:
        # GIVEN a complete provider-side databag and a successful Kafka connection
        validator = _make_validator(VALID_DATABAG, role=RelationRoleStub.provides)
        consumer_stub = KafkaConsumerStub(topics_result={"my-topic"})

        with patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer_stub):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert connect_check.passed

    def test_fails_schema_check_when_required_fields_missing(self) -> None:
        # GIVEN an empty local databag (provider has published nothing)
        validator = _make_validator({}, role=RelationRoleStub.provides)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed

    def test_fails_connect_check_when_broker_unreachable(self) -> None:
        # GIVEN a complete databag but the broker refuses the connection
        validator = _make_validator(VALID_DATABAG, role=RelationRoleStub.provides)
        consumer_stub = KafkaConsumerStub(topics_error=Exception("Connection refused"))

        with patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer_stub):
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        connect_check = next(c for c in result.checks if c.name == "connect")
        assert not connect_check.passed
        assert "Connection refused" in connect_check.message

    def test_reads_fields_from_local_app_databag_not_remote(self) -> None:
        # GIVEN a remote (requirer) databag with unrelated fields, and the real
        # connection fields published on the local (provider) app databag
        validator = _make_validator(
            VALID_DATABAG, role=RelationRoleStub.provides, remote_extra={"extra-user-roles": "admin"}
        )
        consumer_stub = KafkaConsumerStub(topics_result={"my-topic"})

        with patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer_stub):
            result = validator.validate(level="simple")

        # THEN validation still passes using the local app databag
        assert result.status == "PASS"

    def test_resolves_credentials_from_secret_on_local_app_databag(self) -> None:
        # GIVEN a local (provider) app databag that references a Juju secret
        # instead of publishing username/password inline
        remote_app = ApplicationStub(name="remote-app")
        databag = {
            "endpoints": "10.1.2.3:9092",
            "topic": "my-topic",
            "secret-user": "secret:kafka-creds",
        }
        relation = RelationStub(name="kafka", id=0, app=remote_app, data={remote_app: {}})
        secrets = {"secret:kafka-creds": {"username": "kafka-user", "password": "s3cr3t"}}
        charm_stub = make_charm_from_relation_and_secrets(relation, secrets, role=RelationRoleStub.provides)
        relation.data[charm_stub.app] = databag
        validator = KafkaClientValidator(cast(ops.CharmBase, charm_stub), cast(ops.Relation, relation))
        consumer_stub = KafkaConsumerStub(topics_result={"my-topic"})

        # WHEN
        with patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer_stub):
            result = validator.validate(level="simple")

        # THEN the secret is resolved and validation passes
        assert result.status == "PASS"
        assert charm_stub.model.requested_ids == ["secret:kafka-creds"]


class TestKafkaClientValidatorProvidesDeep:
    def test_passes_when_canary_message_produced_and_consumed(self) -> None:
        # GIVEN a complete provider-side databag, a producer that sends successfully,
        # and a consumer that returns the exact canary value on first poll.
        validator = _make_validator(VALID_DATABAG, role=RelationRoleStub.provides)
        captured_value: list[bytes] = []

        class CapturingProducerStub(KafkaProducerStub):
            def send(self, topic: str, key: bytes | None = None, value: bytes | None = None) -> FutureStub:
                if value:
                    captured_value.append(value)
                return self.future

        consumer_stub = KafkaConsumerStub()

        def make_consumer(**kwargs: Any) -> KafkaConsumerStub:
            if captured_value:
                record = ConsumerRecordStub(value=captured_value[0])
                consumer_stub.poll_batches = [{"tp": [record]}]
            return consumer_stub

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=KafkaAdminClientStub()),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=CapturingProducerStub()),
            patch("validators.kafka_client.validator.KafkaConsumer", side_effect=make_consumer),
        ):
            result = validator.validate(level="deep")

        # THEN
        assert result.status == "PASS"
        produce_check = next(c for c in result.checks if c.name == "produce")
        assert produce_check.passed
        consume_check = next(c for c in result.checks if c.name == "consume")
        assert consume_check.passed

    def test_fails_schema_check_when_required_fields_missing(self) -> None:
        # GIVEN an empty local databag
        validator = _make_validator({}, role=RelationRoleStub.provides)

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed

    def test_fails_schema_check_when_consumer_group_prefix_missing(self) -> None:
        # GIVEN consumer-group-prefix is absent on the local (provider) app databag
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "consumer-group-prefix"}
        validator = _make_validator(databag, role=RelationRoleStub.provides)

        # WHEN
        result = validator.validate(level="deep")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "consumer-group-prefix" in schema_check.message

    def test_fails_when_producer_constructor_raises(self) -> None:
        # GIVEN the KafkaProducer constructor raises immediately
        validator = _make_validator(VALID_DATABAG, role=RelationRoleStub.provides)

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=KafkaAdminClientStub()),
            patch(
                "validators.kafka_client.validator.KafkaProducer",
                side_effect=Exception("NoBrokersAvailable"),
            ),
        ):
            result = validator.validate(level="deep")

        # THEN produce check fails and we stop before consume
        assert result.status == "FAIL"
        produce_check = next(c for c in result.checks if c.name == "produce")
        assert not produce_check.passed
        assert not any(c.name == "consume" for c in result.checks)


# ---------------------------------------------------------------------------
# Tests — bootstrap server format edge cases
# ---------------------------------------------------------------------------


class TestCheckBootstrapServers:
    @pytest.mark.parametrize(
        "value,expected_passed",
        [
            ("10.1.2.3:9092", True),
            ("10.1.2.3:9092,10.1.2.4:9092", True),
            ("kafka.example.com:9093", True),
            ("10.1.2.3:1,10.1.2.4:65535", True),
            ("", False),
            ("10.1.2.3", False),
            ("10.1.2.3:notaport", False),
            ("10.1.2.3:0", False),
            ("10.1.2.3:65536", False),
            (":9092", False),
            ("10.1.2.3:9092,:9093", False),  # second entry invalid
        ],
    )
    def test_endpoint_validation(self, value: str, expected_passed: bool) -> None:
        # GIVEN a validator with the given endpoints value
        validator = _make_validator({**VALID_DATABAG, "endpoints": value})

        # WHEN
        check = validator._check_bootstrap_servers(value)

        # THEN
        assert check.passed == expected_passed, f"endpoints='{value}' expected passed={expected_passed}"


# ---------------------------------------------------------------------------
# Tests — persistence validator
# ---------------------------------------------------------------------------


class TestKafkaClientPersistenceValidatorRole:
    @pytest.mark.parametrize("role", [RelationRoleStub.provides, RelationRoleStub.peer])
    def test_prepare_raises_not_applicable_for_non_requires_role(self, role: RelationRoleStub) -> None:
        # GIVEN a validator on the non-requires side of the relation
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG, role=role)

        # WHEN / THEN
        with pytest.raises(PersistenceNotApplicable):
            validator.prepare()

    def test_checkpoint_raises_not_applicable_for_non_requires_role(self) -> None:
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG, role=RelationRoleStub.provides)

        with pytest.raises(PersistenceNotApplicable):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=1))

    def test_cleanup_raises_not_applicable_for_non_requires_role(self) -> None:
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG, role=RelationRoleStub.provides)

        with pytest.raises(PersistenceNotApplicable):
            validator.cleanup()


class TestKafkaClientPersistenceValidatorConnection:
    def test_prepare_raises_when_endpoints_is_missing(self) -> None:
        # GIVEN a databag missing the required "endpoints" field
        databag = {k: v for k, v in PERSISTENCE_VALID_DATABAG.items() if k != "endpoints"}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="endpoints"):
            validator.prepare()

    def test_checkpoint_raises_when_credentials_are_missing(self) -> None:
        # GIVEN a databag missing the required "password" field
        databag = {k: v for k, v in PERSISTENCE_VALID_DATABAG.items() if k != "password"}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="password"):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=1))

    def test_cleanup_raises_not_applicable_when_credentials_are_missing(self) -> None:
        # GIVEN a relation with no usable credentials yet (still being established)
        validator = _make_persistence_validator({})

        # WHEN / THEN cleanup treats incomplete credentials as a skip, not an error, so the
        # harness keeps any tracked state rather than treating this as a successful cleanup.
        with pytest.raises(PersistenceNotApplicable):
            validator.cleanup()


class TestKafkaClientPersistenceValidatorPrepare:
    def test_creates_canary_topic_and_produces_message_returns_state(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        admin = PersistenceKafkaAdminClientStub()
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            state = validator.prepare()

        # THEN
        assert isinstance(state, PersistenceState)
        assert state.ref == 1
        assert state.token
        expected_topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{state.id:020d}"
        assert expected_topic in admin.topics
        assert len(producer.sent) == 1
        topic, key, value = producer.sent[0]
        assert topic == expected_topic
        payload = json.loads(value.decode())  # type: ignore[union-attr]
        assert payload == {"token": state.token, "ref": 1}

    def test_produces_canary_message_with_acks_all_for_durability(self) -> None:
        # GIVEN. Regression test for: the persistence producer used kafka-python's default
        # acks=1 (leader-only acknowledgement), so future.get() returning successfully did not
        # confirm replication to the other replicas; a broker disruption immediately after could
        # lose the just-written canary and cause a false persistence failure.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        admin = PersistenceKafkaAdminClientStub()
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer) as mock_producer_cls,
        ):
            # WHEN
            validator.prepare()

        # THEN the persistence producer requires every replica to acknowledge the write
        assert mock_producer_cls.call_args.kwargs["acks"] == "all"

    def test_produces_canary_message_with_idempotence_enabled(self) -> None:
        # GIVEN. Regression test for: acks="all" alone only guarantees a write is replicated once
        # accepted, it does not stop a non-idempotent producer from appending a duplicate record
        # if a retry fires after an ack is lost in transit. A duplicate canary message would then
        # be seen by checkpoint() as an unexpected extra record and reported as data loss even
        # though the canary survived.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        admin = PersistenceKafkaAdminClientStub()
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer) as mock_producer_cls,
        ):
            # WHEN
            validator.prepare()

        # THEN the persistence producer is idempotent, so a transport-level retry cannot append
        # a duplicate record on top of an already-acknowledged write
        assert mock_producer_cls.call_args.kwargs["enable_idempotence"] is True

    def test_creates_canary_topic_with_a_replication_factor_matching_the_live_broker_count(self) -> None:
        # GIVEN a 3-broker cluster: hard-coding replication_factor=1 would mean a single broker
        # loss can remove the canary topic's only replica even though the cluster itself (and the
        # application's own topic) could tolerate it, producing a false persistence failure.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        admin = PersistenceKafkaAdminClientStub(broker_count=3)
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            validator.prepare()

        # THEN the topic is created with a replication factor that uses the available brokers
        assert admin.created_replication_factors == [3]

    def test_caps_replication_factor_at_three_on_a_larger_cluster(self) -> None:
        # GIVEN a cluster with more than 3 brokers: requesting an unnecessarily high replication
        # factor isn't wrong, but 3 is the conventional production ceiling, so this asserts the
        # validator doesn't over-request replicas for a canary topic.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        admin = PersistenceKafkaAdminClientStub(broker_count=7)
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            validator.prepare()

        # THEN
        assert admin.created_replication_factors == [3]

    def test_pins_the_canary_to_the_application_topics_own_replica_assignment(self) -> None:
        # GIVEN a 7-broker cluster where the related application topic (named in the databag) is
        # itself only replicated twice, on brokers 0 and 1. Regression test for: matching only the
        # replication *factor* (e.g. creating an RF=2 canary without specifying which brokers)
        # lets Kafka assign the canary's partition to a different pair of brokers than the
        # application topic - a broker failure that destroys the application's only replicas could
        # leave an unaffected canary replica reporting a false PASS. The canary must be pinned to
        # the exact same brokers as the application topic's own partition 0, not merely the same
        # replica count.
        databag = PERSISTENCE_VALID_DATABAG | {"topic": "app-topic"}
        validator = _make_persistence_validator(databag)
        admin = PersistenceKafkaAdminClientStub(broker_count=7, application_topic_replica_count=2)
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            validator.prepare()

        # THEN the canary topic's partition 0 is explicitly pinned to the same broker IDs as the
        # application topic's partition 0, not just given a matching replication factor
        assert admin.created_replica_assignments == [{0: [0, 1]}]

    def test_rejects_a_multi_partition_application_topic(self) -> None:
        # GIVEN the related application topic has more than one partition. Regression test for:
        # this validator's canary always has exactly one partition, pinned to the same brokers as
        # the application topic's own partition 0. A disruption confined to another partition
        # (e.g. partition 1, possibly on different brokers) could destroy application data while
        # leaving partition 0 and this canary untouched, letting checkpoint() report a false PASS.
        # Since the canary can't cover every partition, prepare() must refuse this topology rather
        # than silently provide an incomplete guarantee.
        databag = PERSISTENCE_VALID_DATABAG | {"topic": "app-topic"}
        validator = _make_persistence_validator(databag)
        admin = PersistenceKafkaAdminClientStub(application_topic_replica_count=1, application_topic_partition_count=2)
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN / THEN
            with pytest.raises(RuntimeError, match="partitions"):
                validator.prepare()
        # AND no canary topic/message was created for this unsupported topology.
        assert admin.topics == []
        assert producer.sent == []

    def test_proceeds_when_the_application_topic_partition_count_cannot_be_determined(self) -> None:
        # GIVEN the application topic isn't describable yet (e.g. the relation was just
        # established): an unknown partition count must not be treated as a known-unsupported
        # multi-partition topology - that would block every prepare() call on a brand-new
        # relation, the same transient state _resolve_canary_replica_assignment already falls
        # back from.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        admin = PersistenceKafkaAdminClientStub()  # application_topic_replica_count=None (default)
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN / THEN
            state = validator.prepare()
        assert isinstance(state, PersistenceState)

    def test_falls_back_to_a_replication_factor_of_one_when_describe_cluster_fails(self) -> None:
        # GIVEN describe_cluster() fails (e.g. the credentials lack cluster-describe authority):
        # topic creation must still proceed with the safe single-broker default rather than
        # raising and abandoning the whole prepare() call.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        admin = PersistenceKafkaAdminClientStub(describe_cluster_error=Exception("not authorized"))
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            state = validator.prepare()

        # THEN
        assert isinstance(state, PersistenceState)
        assert admin.created_replication_factors == [1]

    def test_generates_distinct_identifiers_across_calls(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        admin = PersistenceKafkaAdminClientStub()
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            first = validator.prepare()
            second = validator.prepare()

        # THEN
        assert first.id != second.id
        assert first.token != second.token

    def test_removes_temporary_ca_file_after_the_call(self) -> None:
        # GIVEN TLS is configured, so _build_kafka_client_kwargs() writes the CA content to a temp
        # PEM file that's reused across every client built within this call.
        databag = {**PERSISTENCE_VALID_DATABAG, "tls": "enabled", "tls-ca": "FAKE-CA-CONTENT"}
        validator = _make_persistence_validator(databag)
        admin = PersistenceKafkaAdminClientStub()
        producer = KafkaProducerStub()
        created_paths: list[str] = []
        original_create_ca_file = KafkaClientPersistenceValidator._create_temp_ca_file

        def spy_create_ca_file(self: KafkaClientPersistenceValidator, ca_content: str) -> None:
            original_create_ca_file(self, ca_content)
            if self._ca_file_path:
                created_paths.append(self._ca_file_path)

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
            patch.object(KafkaClientPersistenceValidator, "_create_temp_ca_file", spy_create_ca_file),
        ):
            # WHEN
            validator.prepare()

        # THEN the CA file actually created on disk for this call is gone, and the cached path
        # cleared - not left behind to leak credentials across future persistence runs.
        assert created_paths
        assert validator._ca_file_path is None
        assert not os.path.exists(created_paths[0])

    def test_prepare_is_idempotent_for_an_existing_topic(self) -> None:
        # GIVEN a resumed run reuses the same UUID-derived identifier (e.g. a restored RNG seed)
        # and the topic already holds the single, clean canary record the first call wrote: the
        # second prepare() must hit the already-exists path, adopt that record's token instead of
        # writing a duplicate ref=1 message, and still return a usable state.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        admin = PersistenceKafkaAdminClientStub()
        producer = KafkaProducerStub()
        fixed_uuid = uuid.uuid4()

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
            patch("validators.kafka_client.validator.uuid.uuid4", return_value=fixed_uuid),
        ):
            # WHEN prepare() is called twice with the same underlying identifier
            first = validator.prepare()
            admin.create_error = TopicAlreadyExistsError()
            expected_topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{first.id:020d}"
            # The reconciliation read sees exactly what the first call actually produced - not a
            # stub hand-built to hide a duplicate write.
            consumer = PersistenceKafkaConsumerStub(records_by_topic={expected_topic: [_canary_record(first.token, 1)]})
            with patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer):
                second = validator.prepare()

        # THEN both calls target the same topic and the same (adopted, not re-minted) token, and
        # only the first call's message was ever produced - no duplicate ref=1 record was written.
        assert first.id == second.id
        assert first.token == second.token
        assert len(producer.sent) == 1

        # AND the resulting state still checkpoints cleanly against the complete topic history.
        checkpoint_consumer = PersistenceKafkaConsumerStub(
            records_by_topic={expected_topic: [_canary_record(second.token, 1)]}
        )
        with patch("validators.kafka_client.validator.KafkaConsumer", return_value=checkpoint_consumer):
            with patch("validators.kafka_client.validator.KafkaProducer", return_value=producer):
                result, _ = validator.checkpoint(second)
        assert result.status == "PASS"

    def test_prepare_writes_a_fresh_canary_when_existing_topic_is_not_reconcilable(self) -> None:
        # GIVEN the topic already exists but holds something other than a single, clean ref=1
        # record (e.g. leftover corruption, or a topic some other process created) - this can't be
        # safely adopted, so prepare() must fall back to writing its own fresh canary rather than
        # silently returning a token for data it never verified.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        admin = PersistenceKafkaAdminClientStub()
        admin.create_error = TopicAlreadyExistsError()
        producer = KafkaProducerStub()
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{0:020d}"
        consumer = PersistenceKafkaConsumerStub(
            records_by_topic={topic: [_canary_record("stale-token", 1), _canary_record("stale-token", 2)]}
        )

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.uuid.uuid4", return_value=uuid.UUID(int=0)),
        ):
            # WHEN
            state = validator.prepare()

        # THEN a brand-new canary message was produced rather than adopting the unreconcilable
        # existing content.
        assert len(producer.sent) == 1
        _, _, value = producer.sent[0]
        assert value is not None
        assert json.loads(value.decode())["token"] == state.token
        assert state.token != "stale-token"

    def test_persistence_prepare_propagates_a_genuine_topic_creation_failure(self) -> None:
        # GIVEN topic creation fails for a reason other than the topic already existing (e.g. an
        # authorization or broker-config error): silently proceeding to produce() could let a
        # broker with auto.create.topics.enable=true implicitly create the canary topic with
        # cluster-default durability settings instead of the resolved application topology,
        # masking a real admin failure behind an apparently successful persistence run.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        admin = PersistenceKafkaAdminClientStub()
        admin.create_error = KafkaError("broker rejected topic creation")
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN / THEN
            with pytest.raises(KafkaError):
                validator.prepare()
        # AND no canary message was ever sent for this failed creation.
        assert producer.sent == []

    def test_canary_messages_use_a_unique_key_per_ref(self) -> None:
        # GIVEN a topic that could be configured with cleanup.policy=compact: a constant message
        # key would let compaction collapse every canary record down to just the latest one,
        # silently discarding the earlier refs checkpoint() needs to verify.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        admin = PersistenceKafkaAdminClientStub()
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            state = validator.prepare()

        # THEN the key is not a constant literal and is unique to this (token, ref) pair
        _, key, _ = producer.sent[0]
        assert key == f"{state.token}:{state.ref}".encode()


class TestKafkaClientPersistenceValidatorCheckpoint:
    def test_passes_when_message_count_matches_expected_ref(self) -> None:
        # GIVEN the canary topic has exactly the expected number of tagged messages
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{42:020d}"
        consumer = PersistenceKafkaConsumerStub(
            records_by_topic={topic: [_canary_record(TEST_TOKEN, 1), _canary_record(TEST_TOKEN, 2)]}
        )
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN
        assert result.status == "PASS"
        check = next(c for c in result.checks if c.name == "message_count")
        assert check.passed
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=3)
        # A new message is still produced to continue the chain, keyed uniquely per ref so
        # topic compaction can't collapse it onto an earlier record (see
        # test_canary_messages_use_a_unique_key_per_ref).
        assert len(producer.sent) == 1
        _, key, value = producer.sent[0]
        assert json.loads(value.decode()) == {"token": TEST_TOKEN, "ref": 3}  # type: ignore[union-attr]
        assert key == f"{TEST_TOKEN}:3".encode()

    def test_passes_and_does_not_re_produce_when_the_next_message_is_already_present(self) -> None:
        # GIVEN the topic already contains the "next" message (ref 3) a previous checkpoint()
        # attempt produced: Kafka's acks="all" guarantees the write was durable, but if
        # future.get() succeeded and the process or caller was interrupted before this call's
        # PASS result/advanced state reached the caller, the harness retries checkpoint() with the
        # same (unadvanced) expected.ref=2 state. The extra, already-written ref=3 message must not
        # make this retry fail, and must not be re-produced a second time.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{42:020d}"
        consumer = PersistenceKafkaConsumerStub(
            records_by_topic={
                topic: [_canary_record(TEST_TOKEN, 1), _canary_record(TEST_TOKEN, 2), _canary_record(TEST_TOKEN, 3)]
            }
        )
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN the retry is recognized as a PASS and the already-present next message is not
        # produced again
        assert result.status == "PASS"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=3)
        assert producer.sent == []

    def test_fails_when_a_duplicate_next_ref_is_present(self) -> None:
        # GIVEN the topic contains TWO copies of the "next" message (ref 3), not one. Regression
        # test for: the ambiguous-retry tolerance must accept at most a single extra record at
        # expected.ref + 1 - a second copy is real corruption (e.g. two independent producer
        # retries, or data duplication), not a single ambiguous acknowledgement, and must still
        # fail rather than being silently absorbed by the same exception.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{42:020d}"
        consumer = PersistenceKafkaConsumerStub(
            records_by_topic={
                topic: [
                    _canary_record(TEST_TOKEN, 1),
                    _canary_record(TEST_TOKEN, 2),
                    _canary_record(TEST_TOKEN, 3),
                    _canary_record(TEST_TOKEN, 3),
                ]
            }
        )
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN the duplicate next-ref is not silently tolerated
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=2)
        assert producer.sent == []

    def test_fails_when_a_same_token_record_has_a_ref_beyond_the_tolerated_next_ref(self) -> None:
        # GIVEN the topic contains refs 1, 2 (matching expected.ref=2) and ref 4 - beyond the
        # single `expected.ref + 1 == 3` exception the ambiguous-retry tolerance allows.
        # Regression test for: a prior version's range filter (`1 <= ref <= expected.ref`) simply
        # discarded any ref outside that window with no upper bound beyond expected.ref + 1,
        # so an out-of-range ref like this one was silently ignored instead of failing the check.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{42:020d}"
        consumer = PersistenceKafkaConsumerStub(
            records_by_topic={
                topic: [
                    _canary_record(TEST_TOKEN, 1),
                    _canary_record(TEST_TOKEN, 2),
                    _canary_record(TEST_TOKEN, 4),
                ]
            }
        )
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=2)
        assert producer.sent == []

    def test_fails_when_a_same_token_record_has_a_non_positive_ref(self) -> None:
        # GIVEN the topic contains an out-of-range ref of 0 alongside the expected refs 1 and 2.
        # Regression test for: the prior unconditional `1 <= ref <= expected.ref` range filter
        # silently discarded a ref=0 (or negative) record instead of failing on it.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{42:020d}"
        consumer = PersistenceKafkaConsumerStub(
            records_by_topic={
                topic: [
                    _canary_record(TEST_TOKEN, 0),
                    _canary_record(TEST_TOKEN, 1),
                    _canary_record(TEST_TOKEN, 2),
                ]
            }
        )
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=2)
        assert producer.sent == []

    def test_fails_when_message_count_is_lower_than_expected(self) -> None:
        # GIVEN data loss: fewer matching messages than expected
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{7:020d}"
        consumer = PersistenceKafkaConsumerStub(records_by_topic={topic: [_canary_record(TEST_TOKEN, 1)]})
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=7, ref=3))

        # THEN
        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "message_count")
        assert not check.passed
        assert "3" in check.message and "1" in check.message
        # Regression test for: checkpoint() must not write a new message and advance ref on FAIL -
        # ValidatorRunner only carries the returned state forward on PASS, so writing here would
        # grow the actual message count past what a later checkpoint could compare against.
        assert new_state == PersistenceState(token=TEST_TOKEN, id=7, ref=3)
        assert producer.sent == []

    def test_fails_when_topic_is_not_found(self) -> None:
        # GIVEN the canary topic doesn't exist (e.g. it was deleted or never created)
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{7:020d}"
        consumer = PersistenceKafkaConsumerStub(missing_topics={topic})
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=7, ref=1))

        # THEN
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=7, ref=1)
        assert producer.sent == []

    def test_fails_when_topic_was_deleted_and_recreated_with_same_message_count(self) -> None:
        # GIVEN a topic deleted and recreated from scratch: a fresh single message happens to
        # reproduce the same message count as a legitimate ref=1 state, but carries a different
        # (freshly minted) token. Only the random per-run token distinguishes the original canary
        # messages from the recreated ones.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{42:020d}"
        consumer = PersistenceKafkaConsumerStub(records_by_topic={topic: [_canary_record("a-different-token", 1)]})
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=1))

        # THEN the recreated topic is detected as data loss, not a false PASS
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=1)
        assert producer.sent == []

    def test_uses_canary_topic_name_derived_from_expected_identifier(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{99:020d}"
        consumer = PersistenceKafkaConsumerStub(records_by_topic={topic: [_canary_record(TEST_TOKEN, 1)]})

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=KafkaProducerStub()),
        ):
            # WHEN
            result, _ = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=99, ref=1))

        # THEN
        assert result.status == "PASS"
        assert consumer.assigned[0].topic == topic

    def test_rejects_state_without_a_token(self) -> None:
        # GIVEN a state serialised before the token existed (or otherwise restored/malformed).
        # The base protocol rejects such a state at construction, so it can never reach checkpoint().
        with pytest.raises(ValidationError):
            PersistenceState(id=1, ref=1)

    def test_result_endpoint_and_interface_are_set(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG, endpoint="my-kafka")
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{1:020d}"
        consumer = PersistenceKafkaConsumerStub(records_by_topic={topic: [_canary_record(TEST_TOKEN, 1)]})

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=KafkaProducerStub()),
        ):
            # WHEN
            result, _ = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=1))

        # THEN
        assert result.endpoint == "my-kafka"
        assert result.interface == "kafka_client"
        assert result.level == "deep"

    def test_raises_when_expected_identifier_is_out_of_range(self) -> None:
        # GIVEN a restored/malformed PersistenceState with an out-of-range id
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        out_of_range_id = 1 << 63  # one past _MAX_CANARY_IDENTIFIER

        # WHEN / THEN
        with pytest.raises(ValueError, match="out of range"):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=out_of_range_id, ref=1))

    def test_raises_when_expected_identifier_is_negative(self) -> None:
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)

        with pytest.raises(ValueError, match="out of range"):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=-1, ref=1))

    def test_raises_when_expected_ref_is_zero(self) -> None:
        # GIVEN a restored/malformed PersistenceState with ref=0 (prepare() always returns ref=1)
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)

        with pytest.raises(ValueError, match="out of range"):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=0))

    def test_raises_when_expected_ref_is_negative(self) -> None:
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)

        with pytest.raises(ValueError, match="out of range"):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=-1))

    def test_large_expected_ref_fails_without_allocating_proportionally_to_it(self) -> None:
        # GIVEN a schema-valid but implausibly large ref and only a handful of real records: the
        # comparison must cost O(len(matching_refs)), not O(expected.ref), so an untrusted,
        # oversized ref can't force an unbounded allocation/iteration before returning a result.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{1:020d}"
        consumer = PersistenceKafkaConsumerStub(records_by_topic={topic: [_canary_record(TEST_TOKEN, 1)]})
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=10**9))

        # THEN it correctly reports data loss rather than raising or hanging
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=1, ref=10**9)
        assert producer.sent == []

    def test_checkpoint_chain_stays_valid_indefinitely(self) -> None:
        # Regression test: an earlier fix capped expected.ref at a fixed upper bound to address the
        # allocation concern above, but that made the state PASS just returned unusable on the very
        # next checkpoint call once ref reached the cap - a real PASS chain must never dead-end like
        # that. This runs several checkpoints back-to-back, each consuming the exact state the
        # previous call returned, to confirm the chain keeps working.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{1:020d}"
        state = PersistenceState(token=TEST_TOKEN, id=1, ref=1)

        for next_ref in range(2, 6):
            records = [_canary_record(TEST_TOKEN, ref) for ref in range(1, next_ref)]
            consumer = PersistenceKafkaConsumerStub(records_by_topic={topic: records})
            producer = KafkaProducerStub()
            with (
                patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
                patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
            ):
                result, state = validator.checkpoint(state)
            assert result.status == "PASS"
            assert state == PersistenceState(token=TEST_TOKEN, id=1, ref=next_ref)

    def test_fails_when_topic_has_a_duplicate_ref_alongside_a_missing_one(self) -> None:
        # GIVEN a topic containing a duplicate ref (1 twice) instead of the missing ref 2: a bare
        # count-only or deduplicated-set comparison would wrongly match expected_refs=[1, 2].
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{42:020d}"
        consumer = PersistenceKafkaConsumerStub(
            records_by_topic={topic: [_canary_record(TEST_TOKEN, 1), _canary_record(TEST_TOKEN, 1)]}
        )
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN the duplicate is not mistaken for the missing ref 2
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=2)
        assert producer.sent == []

    def test_fails_when_the_read_deadline_is_hit_before_end_offset_is_reached(self) -> None:
        # GIVEN a topic that actually contains the expected ref 1, but a stalled/slow broker that
        # never delivers it before the read deadline: a naive "stop at the deadline and evaluate
        # whatever was collected" approach would see 0 matching records and happen to fail here,
        # but could instead produce a false PASS if a partial tagged prefix had already arrived.
        # This asserts on the explicit, deliberate "incomplete read" signal rather than relying on
        # that coincidence.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{42:020d}"
        consumer = PersistenceKafkaConsumerStub(records_by_topic={topic: [_canary_record(TEST_TOKEN, 1)]}, stall=True)
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
            patch("validators.kafka_client.validator._CONSUME_TIMEOUT_S", 0.05),
        ):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=1))

        # THEN this is reported as an unverified FAIL, not a false PASS or a crash, and the state
        # is not advanced so the next checkpoint retries from the same expected ref
        assert result.status == "FAIL"
        assert "Timed out" in result.checks[0].message
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=1)
        assert producer.sent == []

    def test_fails_when_a_same_token_record_has_a_malformed_ref(self) -> None:
        # GIVEN the topic has the exact expected ref 1, plus an extra same-token record whose ref
        # isn't a real int (e.g. a corrupted message): a filter-out-then-count approach would
        # silently ignore the malformed record and still report PASS.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{42:020d}"
        malformed = ConsumerRecordStub(value=json.dumps({"token": TEST_TOKEN, "ref": "1"}).encode())
        consumer = PersistenceKafkaConsumerStub(records_by_topic={topic: [_canary_record(TEST_TOKEN, 1), malformed]})
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=1))

        # THEN the malformed record correctly fails the check instead of being silently dropped
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=1)
        assert producer.sent == []

    def test_fails_when_a_same_token_record_has_a_bool_ref(self) -> None:
        # GIVEN a same-token record whose ref is a bool: bool is an int subclass in Python, so a
        # naive `isinstance(ref, int)` check alone would wrongly accept it as a valid ref.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{42:020d}"
        bool_ref = ConsumerRecordStub(value=json.dumps({"token": TEST_TOKEN, "ref": True}).encode())
        consumer = PersistenceKafkaConsumerStub(records_by_topic={topic: [bool_ref]})
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            result, _ = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=1))

        # THEN
        assert result.status == "FAIL"

    def test_ignores_tombstone_records_with_a_null_value(self) -> None:
        # GIVEN the topic contains a tombstone (null-value) record alongside the canary messages -
        # message.value.decode() would otherwise raise AttributeError on the None value.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        topic = f"validator_canary_{TEST_SCOPE_TOKEN}_{42:020d}"
        consumer = PersistenceKafkaConsumerStub(
            records_by_topic={
                topic: [ConsumerRecordStub(value=None), _canary_record(TEST_TOKEN, 1)],
            }
        )
        producer = KafkaProducerStub()

        with (
            patch("validators.kafka_client.validator.KafkaConsumer", return_value=consumer),
            patch("validators.kafka_client.validator.KafkaProducer", return_value=producer),
        ):
            # WHEN
            result, _ = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=1))

        # THEN the tombstone is ignored rather than raising, and the real canary message still passes
        assert result.status == "PASS"


class TestKafkaClientPersistenceValidatorCleanup:
    def test_deletes_all_discovered_canary_topics(self) -> None:
        # GIVEN two canary topics belonging to this validator instance, plus an unrelated topic
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        canary_a = f"validator_canary_{TEST_SCOPE_TOKEN}_{1:020d}"
        canary_b = f"validator_canary_{TEST_SCOPE_TOKEN}_{2:020d}"
        admin = PersistenceKafkaAdminClientStub(topics=[canary_a, canary_b, "unrelated-topic"])

        with patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin):
            # WHEN
            validator.cleanup()

        # THEN
        deleted = {name for batch in admin.deleted for name in batch}
        assert deleted == {canary_a, canary_b}
        assert "unrelated-topic" in admin.topics

    def test_an_already_gone_topic_does_not_mask_a_sibling_deletion_failure(self) -> None:
        # GIVEN two canary topics: one already gone (e.g. a concurrent cleanup), and one that
        # fails deletion for an unrelated reason. Regression test for: deleting both in a single
        # batch call would have let the first topic's UnknownTopicOrPartitionError swallow the
        # whole batch, silently leaving the second topic behind.
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        already_gone = f"validator_canary_{TEST_SCOPE_TOKEN}_{1:020d}"
        fails_to_delete = f"validator_canary_{TEST_SCOPE_TOKEN}_{2:020d}"
        admin = PersistenceKafkaAdminClientStub(
            topics=[already_gone, fails_to_delete],
            delete_errors_by_topic={already_gone: UnknownTopicOrPartitionError()},
        )

        with patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin):
            # WHEN
            validator.cleanup()

        # THEN the still-present topic was still attempted (and deleted), not masked by the
        # other topic's ignored error
        assert fails_to_delete not in admin.topics

    def test_a_genuine_deletion_error_still_propagates(self) -> None:
        # GIVEN a canary topic whose deletion fails for a reason other than "already gone"
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        canary = f"validator_canary_{TEST_SCOPE_TOKEN}_{1:020d}"
        admin = PersistenceKafkaAdminClientStub(
            topics=[canary], delete_errors_by_topic={canary: TopicAuthorizationFailedError()}
        )

        with patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin):
            # THEN the error is not silently swallowed
            with pytest.raises(TopicAuthorizationFailedError):
                validator.cleanup()

    def test_no_op_when_no_canary_topics_exist(self) -> None:
        # GIVEN no canary topics on the cluster
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        admin = PersistenceKafkaAdminClientStub(topics=["unrelated-topic"])

        with patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin):
            # WHEN
            validator.cleanup()

        # THEN
        assert admin.deleted == []

    def test_rejects_topics_that_only_share_the_prefix(self) -> None:
        # GIVEN a hand-created topic sharing the canary prefix but not its fixed-width shape
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        look_alike = f"validator_canary_{TEST_SCOPE_TOKEN}_backup"
        admin = PersistenceKafkaAdminClientStub(topics=[look_alike])

        with patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin):
            # WHEN
            validator.cleanup()

        # THEN the look-alike is not deleted
        assert admin.deleted == []
        assert look_alike in admin.topics

    def test_rejects_discovered_topics_with_an_out_of_range_identifier(self) -> None:
        # GIVEN a topic matching the fixed-width shape but with an identifier prepare() could
        # never have produced (i.e. bigger than _MAX_CANARY_IDENTIFIER)
        validator = _make_persistence_validator(PERSISTENCE_VALID_DATABAG)
        out_of_range_name = f"validator_canary_{TEST_SCOPE_TOKEN}_{_MAX_CANARY_IDENTIFIER + 1:020d}"
        admin = PersistenceKafkaAdminClientStub(topics=[out_of_range_name])

        with patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin):
            # WHEN
            validator.cleanup()

        # THEN
        assert admin.deleted == []
        assert out_of_range_name in admin.topics
