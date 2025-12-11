# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from pydantic.dataclasses import dataclass
from utils.normalization import (
    _normalize_minio_probe_urls,
    _normalize_numeric_sequences,
    _normalize_oci_image_digests,
    _normalize_pod_names,
    _normalize_temp_files,
    _normalize_uuids,
    normalize_string,
)


class TestNormalizeNumericSequences:
    @dataclass
    class Params:
        label: str
        input: str
        expected: str

    test_cases = [
        Params(
            label="single_number",
            input="test123",
            expected="testXXX",
        ),
        Params(
            label="multiple_numbers",
            input="test123abc456def789",
            expected="testXXXabcXXXdefXXX",
        ),
        Params(
            label="ip_address",
            input="192.168.1.1",
            expected="XXX.XXX.XXX.XXX",
        ),
        Params(
            label="timestamp",
            input="2024-12-11T10:30:45",
            expected="XXX-XXX-XXXTXXX:XXX:XXX",
        ),
        Params(
            label="model_name",
            input="model-19725395113-251127043917",
            expected="model-XXX-XXX",
        ),
        Params(
            label="no_numbers",
            input="hello world",
            expected="hello world",
        ),
        Params(
            label="only_numbers",
            input="123456789",
            expected="XXX",
        ),
        Params(
            label="port_number",
            input="localhost:8080",
            expected="localhost:XXX",
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test(self, params: Params):
        result = _normalize_numeric_sequences(params.input)
        assert result == params.expected


class TestNormalizePodNames:
    @dataclass
    class Params:
        label: str
        input: str
        expected: str

    test_cases = [
        Params(
            label="simple_pod_name",
            input="pod=grafana-k8s-0_model-abc-def(12345678-1234-1234-1234-123456789abc)",
            expected="pod=<POD>",
        ),
        Params(
            label="pod_with_dashes",
            input="pod=mysql-k8s-1_namespace-test(abcdef01-2345-6789-abcd-ef0123456789)",
            expected="pod=<POD>",
        ),
        Params(
            label="pod_with_dots",
            input="pod=app.service-0_model.test.ns(11111111-2222-3333-4444-555555555555)",
            expected="pod=<POD>",
        ),
        Params(
            label="multiple_pods",
            input="pod=app1-0_ns1(aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee) pod=app2-1_ns2(ffffffff-0000-1111-2222-333333333333)",
            expected="pod=<POD> pod=<POD>",
        ),
        Params(
            label="no_pod",
            input="hello world",
            expected="hello world",
        ),
        Params(
            label="incomplete_pod_missing_uuid",
            input="pod=app-0_namespace",
            expected="pod=app-0_namespace",
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test(self, params: Params):
        result = _normalize_pod_names(params.input)
        assert result == params.expected


class TestNormalizeUuids:
    @dataclass
    class Params:
        label: str
        input: str
        expected: str

    test_cases = [
        Params(
            label="uuid_lowercase",
            input="12345678-abcd-ef01-2345-6789abcdef01",
            expected="<UUID>",
        ),
        Params(
            label="uuid_in_text",
            input="error with id 12345678-1234-1234-1234-123456789abc occurred",
            expected="error with id <UUID> occurred",
        ),
        Params(
            label="multiple_uuids",
            input="id1: 11111111-2222-3333-4444-555555555555, id2: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            expected="id1: <UUID>, id2: <UUID>",
        ),
        Params(
            label="no_uuid",
            input="hello world",
            expected="hello world",
        ),
        Params(
            label="uuid_with_uppercase",
            input="12345678-ABCD-EF01-2345-6789ABCDEF01",
            expected="12345678-ABCD-EF01-2345-6789ABCDEF01",  # Not matched, only lowercase
        ),
        Params(
            label="almost_uuid_wrong_format",
            input="12345678-1234-1234-1234-12345678",  # Only 8 hex at end
            expected="12345678-1234-1234-1234-12345678",
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test(self, params: Params):
        result = _normalize_uuids(params.input)
        assert result == params.expected


class TestNormalizeTempFiles:
    @dataclass
    class Params:
        label: str
        input: str
        expected: str

    test_cases = [
        Params(
            label="simple_temp",
            input="/tmp5d7rg3qj",
            expected="/tmp<TEMP>",
        ),
        Params(
            label="temp_with_underscores",
            input="/tmp_test_abc123",
            expected="/tmp<TEMP>",
        ),
        Params(
            label="temp_in_full_path",
            input="/home/ubuntu/snap/juju/common/tmp5d7rg3qj",
            expected="/home/ubuntu/snap/juju/common/tmp<TEMP>",
        ),
        Params(
            label="temp_short_suffix",
            input="/tmpabc",
            expected="/tmp<TEMP>",
        ),
        Params(
            label="temp_without_slash",
            input="tmpabcd1234",  # Should not match
            expected="tmpabcd1234",
        ),
        Params(
            label="multiple_temps",
            input="/tmpfile1 and /tmpfile2",
            expected="/tmp<TEMP> and /tmp<TEMP>",
        ),
        Params(
            label="no_temp",
            input="hello world",
            expected="hello world",
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test(self, params: Params):
        result = _normalize_temp_files(params.input)
        assert result == params.expected


class TestNormalizeMinioProbeUrls:
    @dataclass
    class Params:
        label: str
        input: str
        expected: str

    test_cases = [
        Params(
            label="simple_probe",
            input="probe-bsign-abc123xyz",
            expected="probe-bsign-<NONCE>",
        ),
        Params(
            label="probe_in_url",
            input="http://10.1.1.1:9000/probe-bsign-rhudephrbcvyt",
            expected="http://10.1.1.1:9000/probe-bsign-<NONCE>",
        ),
        Params(
            label="probe_long_suffix",
            input="probe-bsign-wzmabcmsnfcknsridefyfuvghijlvd",
            expected="probe-bsign-<NONCE>",
        ),
        Params(
            label="multiple_probes",
            input="probe-bsign-abc123 and probe-bsign-xyz789",
            expected="probe-bsign-<NONCE> and probe-bsign-<NONCE>",
        ),
        Params(
            label="no_probe",
            input="hello world",
            expected="hello world",
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test(self, params: Params):
        result = _normalize_minio_probe_urls(params.input)
        assert result == params.expected


class TestNormalizeOciImageDigests:
    @dataclass
    class Params:
        label: str
        input: str
        expected: str

    test_cases = [
        Params(
            label="sha256_simple",
            input="image@sha256:abcdef0123456789",
            expected="image@sha256:<DIGEST>",
        ),
        Params(
            label="sha256_full_64_chars",
            input="registry.io/image@sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
            expected="registry.io/image@sha256:<DIGEST>",
        ),
        Params(
            label="sha512",
            input="image@sha512:0123456789abcdef",
            expected="image@sha512:<DIGEST>",
        ),
        Params(
            label="blake3",
            input="image@blake3:abc123def456",
            expected="image@blake3:<DIGEST>",
        ),
        Params(
            label="sha256_with_variant",
            input="image@sha256+variant:abcdef123456",
            expected="image@sha256+variant:<DIGEST>",
        ),
        Params(
            label="sha256_with_dot_separator",
            input="image@sha256.custom:xyz789",
            expected="image@sha256.custom:<DIGEST>",
        ),
        Params(
            label="sha256_with_underscore",
            input="image@sha256_algo:xyz789",
            expected="image@sha256_algo:<DIGEST>",
        ),
        Params(
            label="sha256_with_dash",
            input="image@sha256-custom:xyz789",
            expected="image@sha256-custom:<DIGEST>",
        ),
        Params(
            label="encoded_with_special_chars",
            input="image@sha256:aBc_-123=",
            expected="image@sha256:<DIGEST>",
        ),
        Params(
            label="in_error_message",
            input='Back-off pulling image "registry.io/app/oci-image@sha256:1234567890abcdef"',
            expected='Back-off pulling image "registry.io/app/oci-image@sha256:<DIGEST>"',
        ),
        Params(
            label="unsupported_algorithm",
            input="image@md5:abc123",  # md5 not in registered algorithms
            expected="image@md5:abc123",
        ),
        Params(
            label="no_digest",
            input="hello world",
            expected="hello world",
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test(self, params: Params):
        result = _normalize_oci_image_digests(params.input)
        assert result == params.expected


class TestNormalizeString:
    """Integration tests for the full normalize_string function."""

    @dataclass
    class Params:
        label: str
        input: str | bytes | object
        expected: str
        max_length: int = 150

    test_cases = [
        # Basic conversions
        Params(
            label="simple_string",
            input="hello world",
            expected="hello world",
        ),
        Params(
            label="bytes_to_string",
            input=b"hello world",
            expected="hello world",
        ),
        Params(
            label="bytes_with_invalid_utf8",
            input=b"hello\xff\xfeworld",
            expected="hello��world",
        ),
        Params(
            label="object_to_string",
            input=123,
            expected="XXX",
        ),
        Params(
            label="empty_string",
            input="",
            expected="",
        ),
        # Truncation
        Params(
            label="truncate_long_string",
            input="a" * 200,
            expected="a" * 147 + "...",
            max_length=150,
        ),
        Params(
            label="truncate_exact_length",
            input="a" * 150,
            expected="a" * 150,
            max_length=150,
        ),
        Params(
            label="truncate_custom_length",
            input="hello world test",
            expected="hello w...",
            max_length=10,
        ),
        # Complex real-world examples combining multiple normalizations
        Params(
            label="pod_crash_loop_backoff",
            input="crash loop backoff: back-off 5m0s restarting failed container=ml-pipeline-persistenceagent pod=target-dd123-jngfd_model-123-456(12345678-1234-1234-1234-123456789abc)",
            expected="crash loop backoff: back-off XXXmXXXs restarting failed container=ml-pipeline-persistenceagent pod=<POD>",
        ),
        Params(
            label="juju_add_secret_with_temp_file",
            input="Command ['juju', 'add-secret', '--model', 'model-19725395113-251127043917', 'vault-secret', '--file', '/home/ubuntu/snap/juju/common/tmp5d7rg3qj']",
            expected="Command ['juju', 'add-secret', '--model', 'model-XXX-XXX', 'vault-secret', '--file', '/home/ubuntu/snap/juju/common/tmp<TEMP>']",
        ),
        Params(
            label="minio_connection_error",
            input='mc: <ERROR> Get "http://10.152.183.123:9000/probe-bsign-rhudephrbcvytabcxipksqdefwjve"',
            expected='mc: <ERROR> Get "http://XXX.XXX.XXX.XXX:XXX/probe-bsign-<NONCE>"',
        ),
        Params(
            label="image_pull_with_oci_digest",
            input='Back-off pulling image "registry.jujucharms.com/kubeflow-charmers/katib-manager/oci-image@sha256:1ac2b3fd4c5e6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e"',
            expected='Back-off pulling image "registry.jujucharms.com/kubeflow-charmers/katib-manager/oci-image@shaXXX:<DIGEST>"',
        ),
        Params(
            label="nested_patterns",
            input="model-123/pod=app-0_ns-456(12345678-1234-1234-1234-123456789abc)/image@sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
            expected="model-XXX/pod=<POD>/image@shaXXX:<DIGEST>",
        ),
        Params(
            label="mixed_special_characters",
            input="!@#$%^&*()",
            expected="!@#$%^&*()",
        ),
        Params(
            label="line_breaks_preserved",
            input="line1\nline2\r\nline3",
            expected="lineXXX\nlineXXX\r\nlineXXX",
        ),
        Params(
            label="sample_add_secret_error",
            input="juju add-secret --model model-19725395113-251127043917 vault-secret-application-target-tokens --file /home/ubuntu/snap/juju/common/tmp5d7rg3qj",
            expected="juju add-secret --model model-XXX-XXX vault-secret-application-target-tokens --file /home/ubuntu/snap/juju/common/tmp<TEMP>",
        ),
        Params(
            label="sample_error_with_pod_name",
            input="crash loop backoff: back-off 5m0s restarting failed container=ml-pipeline-persistenceagent pod=target-dd5599494-jngfd_model-16804415299-250807122629(6c3c20e7-df7a-4101-a6e7-1d9e7f8d8f63)",
            expected="crash loop backoff: back-off XXXmXXXs restarting failed container=ml-pipeline-persistenceagent pod=<POD>",
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test(self, params: Params):
        # WHEN normalizing the input
        result = normalize_string(params.input, max_length=params.max_length)

        # THEN the result matches expectations
        assert result == params.expected
