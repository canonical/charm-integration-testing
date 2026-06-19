# Copyright (C) 2026 Canonical Ltd

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

from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import patch

import ops
import pytest

from validators.kafka_client.validator import KafkaClientValidator
from validators.test_utils.helpers import make_charm_from_relation
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
) -> KafkaClientValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: databag})
    charm = cast(ops.CharmBase, make_charm_from_relation(relation, interface_name="kafka_client", role=role))
    return KafkaClientValidator(charm, cast(ops.Relation, relation))


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

    def send(self, topic: str, key: bytes | None = None, value: bytes | None = None) -> FutureStub:
        if self.send_error:
            raise self.send_error
        return self.future

    def flush(self, timeout: float | None = None) -> None:
        if self.flush_error:
            raise self.flush_error

    def close(self, timeout: float | None = None) -> None:
        pass


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
            (RelationRoleStub.provides, True),
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
        assert "consumer-group-prefix" in schema_check.message
        assert "username" in schema_check.message
        assert "password" in schema_check.message

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
            def send(  # type: ignore[override]
                self, topic: str, key: bytes | None = None, value: bytes | None = None
            ) -> FutureStub:
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
            def send(  # type: ignore[override]
                self, topic: str, key: bytes | None = None, value: bytes | None = None
            ) -> FutureStub:
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
            def send(  # type: ignore[override]
                self, topic: str, key: bytes | None = None, value: bytes | None = None
            ) -> FutureStub:
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
