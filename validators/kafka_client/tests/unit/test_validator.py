# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import os
from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import patch

import ops
import pytest
from kafka import TopicPartition  # type: ignore[import-untyped]
from kafka.errors import UnknownTopicOrPartitionError  # type: ignore[import-untyped]
from pydantic import ValidationError

from validators.base import PersistenceNotApplicable, PersistenceState
from validators.kafka_client.validator import KafkaClientPersistenceValidator, KafkaClientValidator
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

    def create_topics(self, new_topics: list[Any]) -> dict[str, Any]:
        if self.create_error:
            raise self.create_error
        return {}

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
    deleted: list[list[str]] = field(default_factory=list, init=False, repr=False)

    def create_topics(self, new_topics: list[Any]) -> dict[str, Any]:
        if self.create_error:
            raise self.create_error
        for new_topic in new_topics:
            if new_topic.name not in self.topics:
                self.topics.append(new_topic.name)
        return {}

    def list_topics(self) -> list[str]:
        if self.list_error:
            raise self.list_error
        return list(self.topics)

    def delete_topics(self, topics: list[str]) -> dict[str, Any]:
        if self.delete_error:
            raise self.delete_error
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
        # A new message is still produced to continue the chain
        assert len(producer.sent) == 1
        _, _, value = producer.sent[0]
        assert json.loads(value.decode()) == {"token": TEST_TOKEN, "ref": 3}  # type: ignore[union-attr]

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
        out_of_range_name = f"validator_canary_{TEST_SCOPE_TOKEN}_{99999999999999999999:020d}"
        admin = PersistenceKafkaAdminClientStub(topics=[out_of_range_name])

        with patch("validators.kafka_client.validator.KafkaAdminClient", return_value=admin):
            # WHEN
            validator.cleanup()

        # THEN
        assert admin.deleted == []
        assert out_of_range_name in admin.topics
