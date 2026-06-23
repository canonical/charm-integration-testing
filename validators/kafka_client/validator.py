# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import tempfile
import time
import uuid
from typing import Any

from kafka import KafkaConsumer, KafkaProducer  # type: ignore[import-untyped]
from kafka.admin import KafkaAdminClient, NewTopic  # type: ignore[import-untyped]
from kafka.errors import TopicAlreadyExistsError  # type: ignore[import-untyped]

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_CLIENT_TIMEOUT_MS = 5000
_CLIENT_IDLE_MS = 10000
_SIMPLE_LATENCY_TARGET_S = 0.5
_DEEP_LATENCY_TARGET_S = 10.0
_CONSUME_TIMEOUT_S = 5.0


class KafkaClientValidator(BaseValidator):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ca_file_path: str | None = None

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
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
        creds = self._resolve_credentials()

        # --- 3. Schema check ---
        schema_check = self.validate_schema(
            ["endpoints", "topic", "consumer-group-prefix", "username", "password"], creds
        )
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="simple", checks=checks)

        data = self.databag | creds

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
        creds = self._resolve_credentials()

        # --- 3. Schema check ---
        schema_check = self.validate_schema(
            ["endpoints", "topic", "consumer-group-prefix", "username", "password"], creds
        )
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="deep", checks=checks)

        data = self.databag | creds

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

    def _resolve_credentials(self) -> dict[str, str]:
        """Resolve credentials from relation databag or Juju secrets."""
        return {
            **self.resolve_secret("secret-user", "username", "password"),
            **self.resolve_secret("secret-tls", "tls", "tls-ca"),
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

    def _build_producer(self, data: dict[str, str]) -> KafkaProducer:
        """Build a KafkaProducer with appropriate security settings."""
        kwargs = self._build_kafka_client_kwargs(data)
        return KafkaProducer(**kwargs)

    def _ensure_topic_exists(self, data: dict[str, str], topic: str) -> None:
        """Create the topic if it does not already exist.

        kafka-k8s sets auto.create.topics.enable=false, so the topic must be
        created explicitly. Errors are swallowed — if creation fails the produce
        step will surface a meaningful error instead.
        """
        admin: KafkaAdminClient | None = None
        try:
            admin = KafkaAdminClient(**self._build_kafka_client_kwargs(data))
            admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
        except TopicAlreadyExistsError:
            pass
        except Exception:  # nosec B110 - best-effort; produce step will catch real failures
            pass
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
