#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

set -euo pipefail
umask 077

version=4.48
state="${HOME}/.local/share/charmqa-s3"
project_root="$(cd "$(dirname "$0")/../.." && pwd)"
for command in curl tar sha256sum openssl jq ip systemd-run poetry; do
    command -v "${command}" >/dev/null || { echo "Missing prerequisite: ${command}" >&2; exit 1; }
done
case "$(uname -m)" in
    x86_64)
        arch=amd64
        checksum=4a7d108384d044d95212d1342cdda9533fa55842c1c9b41f606ca3c8a9561124
        binary_checksum=8c07a1ccc4ec058cd90989ac0533c30436832e41c63de2b26f37253d9f744c4d
        ;;
    aarch64)
        arch=arm64
        checksum=557e92c38c5c7d180748eb8e2b2b45e426f8e5282a4caca8e098a90d09f03a07
        binary_checksum=d74fca27004ae3d29e5d77ef53b915043652ebd5b4605ac3c08510c1fb7696b5
        ;;
    *) echo "Unsupported architecture" >&2; exit 1 ;;
esac
if [[ -L "${state}" || ( -e "${state}" && ( ! -d "${state}" || ! -O "${state}" ) ) ]]; then
    echo "S3 state must be an owned directory, not a symlink: ${state}" >&2
    exit 1
fi
mkdir -p "${state}"
chmod 700 "${state}"
for directory in "${state}/data" "${state}/bin"; do
    if [[ -L "${directory}" || ( -e "${directory}" && ( ! -d "${directory}" || ! -O "${directory}" ) ) ]]; then
        echo "S3 state path must be an owned directory, not a symlink: ${directory}" >&2
        exit 1
    fi
done
mkdir -p "${state}/data" "${state}/bin"
for file in "${state}/s3.json" "${state}/s3.conf" "${state}/s3.env"; do
    if [[ -e "${file}" || -L "${file}" ]]; then
        if [[ -L "${file}" || ! -f "${file}" || ! -O "${file}" ]]; then
            echo "S3 credential path must be an owned regular file, not a symlink: ${file}" >&2
            exit 1
        fi
        chmod 600 "${file}"
    fi
done
temporary_files=()
remove_temporary_files() {
    local file
    for file in "${temporary_files[@]}"; do
        rm -f -- "${file}"
    done
}
trap remove_temporary_files EXIT

if [[ ! -f "${state}/bin/weed" ]]; then
    archive="${state}/seaweedfs-${version}.tar.gz"
    curl --fail --silent --show-error --location \
        "https://github.com/seaweedfs/seaweedfs/releases/download/${version}/linux_${arch}.tar.gz" \
        --output "${archive}"
    printf '%s  %s\n' "${checksum}" "${archive}" | sha256sum --check --status
    tar -xzf "${archive}" -C "${state}/bin" weed
    rm "${archive}"
fi
if ! printf '%s  %s\n' "${binary_checksum}" "${state}/bin/weed" | sha256sum --check --status; then
    echo "Existing SeaweedFS binary checksum mismatch; inspect before replacing it" >&2
    exit 1
fi
if ! "${state}/bin/weed" version | grep -q " ${version} "; then
    echo "Existing SeaweedFS binary does not match ${version}; inspect before replacing it" >&2
    exit 1
fi
if [[ ! -f "${state}/s3.json" ]]; then
    export S3_ACCESS_KEY S3_SECRET_KEY S3_CLEANUP_ACCESS_KEY S3_CLEANUP_SECRET_KEY
    S3_ACCESS_KEY="$(openssl rand -hex 16)"
    S3_SECRET_KEY="$(openssl rand -hex 32)"
    S3_CLEANUP_ACCESS_KEY="$(openssl rand -hex 16)"
    S3_CLEANUP_SECRET_KEY="$(openssl rand -hex 32)"
    config_tmp="$(mktemp "${state}/s3.json.XXXXXX")"
    temporary_files+=("${config_tmp}")
    jq -n '{identities: [
        {name: "charmqa-cleanup", credentials: [{
            accessKey: env.S3_CLEANUP_ACCESS_KEY, secretKey: env.S3_CLEANUP_SECRET_KEY #gitleaks:allow
        }], actions: ["Admin"]},
        {name: "charmqa-tests", credentials: [{
            accessKey: env.S3_ACCESS_KEY, secretKey: env.S3_SECRET_KEY #gitleaks:allow
        }], actions: [
            "Read:charmqa-s3-*", "Write:charmqa-s3-*", "List:charmqa-s3-*", "Tagging:charmqa-s3-*"
        ]}
    ]}' > "${config_tmp}"
    mv "${config_tmp}" "${state}/s3.json"
fi
if ! jq -e '
    (.identities | length == 2)
    and (.identities[0].name == "charmqa-cleanup")
    and (.identities[1].name == "charmqa-tests")
    and (.identities[0].credentials[0].accessKey | type == "string" and length > 0)
    and (.identities[0].credentials[0].secretKey | type == "string" and length > 0)
    and (.identities[1].credentials[0].accessKey | type == "string" and length > 0)
    and (.identities[1].credentials[0].secretKey | type == "string" and length > 0)
' "${state}/s3.json" >/dev/null; then
    echo "Existing S3 credentials use an unsupported format; inspect ${state}/s3.json before replacing it" >&2
    exit 1
fi
address="$(ip -4 route get 1.1.1.1 | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
[[ -n "${address}" ]] || { echo "Cannot determine a VM address reachable from units" >&2; exit 1; }
export S3_ENDPOINT="http://${address}:8333"
export S3_ACCESS_KEY S3_SECRET_KEY S3_CLEANUP_ACCESS_KEY S3_CLEANUP_SECRET_KEY
S3_CLEANUP_ACCESS_KEY="$(jq -r '.identities[0].credentials[0].accessKey' "${state}/s3.json")"
S3_CLEANUP_SECRET_KEY="$(jq -r '.identities[0].credentials[0].secretKey' "${state}/s3.json")"
S3_ACCESS_KEY="$(jq -r '.identities[1].credentials[0].accessKey' "${state}/s3.json")"
S3_SECRET_KEY="$(jq -r '.identities[1].credentials[0].secretKey' "${state}/s3.json")"
if ! sudo -n systemctl is-active --quiet charmqa-s3.service; then
    if sudo -n systemctl cat charmqa-s3.service >/dev/null 2>&1; then
        echo "Existing charmqa-s3 service is not active; inspect journalctl before restarting" >&2
        exit 1
    fi
    sudo -n systemd-run --unit=charmqa-s3 \
        --property="User=$(id -un)" --property="WorkingDirectory=${state}" \
        "${state}/bin/weed" server -ip=127.0.0.1 -ip.bind=127.0.0.1 -dir="${state}/data" \
        -volume.port=18080 -volume.max=0 -master.volumeSizeLimitMB=64 -filer.port=18888 \
        -s3 -s3.ip.bind="${address}" -s3.port=8333 -s3.port.iceberg=0 -s3.port.lance=0 \
        -s3.config="${state}/s3.json"
fi
ready=0
for attempt in $(seq 1 30); do
    if status="$(curl --silent --connect-timeout 1 --max-time 2 --output /dev/null \
        --write-out '%{http_code}' "${S3_ENDPOINT}")" && [[ "${status}" == 200 || "${status}" == 403 ]]; then
        ready=1
        break
    fi
    sleep 1
done
if (( ! ready )); then
    echo "SeaweedFS is not ready at ${S3_ENDPOINT}; credentials were not written" >&2
    echo "Inspect journalctl -u charmqa-s3 and the service bind address; setup does not rebind an existing service." >&2
    exit 1
fi
cd "${project_root}"
poetry run python - <<'PY'
import os
import sys
from collections.abc import Callable
from functools import partial
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def create_client(access_key: str, secret_key: str):
    return boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT"],
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, request_checksum_calculation="when_required"),
    )


client = create_client(os.environ["S3_ACCESS_KEY"], os.environ["S3_SECRET_KEY"])
cleanup_client = create_client(
    os.environ["S3_CLEANUP_ACCESS_KEY"], os.environ["S3_CLEANUP_SECRET_KEY"]
)


def remove_probe_bucket(name: str) -> None:
    try:
        cleanup_client.delete_object(Bucket=name, Key="probe")
    except ClientError as exc:
        error = exc.response.get("Error", {})
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if error.get("Code") not in {"NoSuchKey", "NoSuchBucket"} and status != 404:
            raise
    finally:
        try:
            cleanup_client.delete_bucket(Bucket=name)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if error.get("Code") != "NoSuchBucket" and status != 404:
                raise


bucket = "charmqa-s3-local-probe-" + uuid4().hex
outside_bucket = "charmqa-outside-probe-" + uuid4().hex
bucket_creation_attempted = False
outside_bucket_creation_attempted = False
probe_error: Exception | None = None
try:
    bucket_creation_attempted = True
    cleanup_client.create_bucket(Bucket=bucket)
    client.put_object(Bucket=bucket, Key="probe", Body=b"local-s3")
    result = client.get_object(Bucket=bucket, Key="probe")
    try:
        if result["Body"].read() != b"local-s3":
            raise RuntimeError("Local S3 canary content mismatch")
    finally:
        result["Body"].close()
    outside_bucket_creation_attempted = True
    try:
        client.create_bucket(Bucket=outside_bucket)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if error.get("Code") not in {"AccessDenied", "Forbidden"} and status != 403:
            raise
    else:
        raise RuntimeError("The test S3 identity can create buckets outside its harness scope")
    cleanup_client.create_bucket(Bucket=outside_bucket)
    cleanup_client.put_object(Bucket=outside_bucket, Key="probe", Body=b"outside-scope")
    cleanup_client.put_bucket_tagging(Bucket=outside_bucket, Tagging={"TagSet": [{"Key": "probe", "Value": "scope"}]})
    for operation, arguments in (
        ("get_object", {"Key": "probe"}),
        ("put_object", {"Key": "probe", "Body": b"not-allowed"}),
        ("delete_object", {"Key": "probe"}),
        ("list_objects_v2", {}),
        ("get_bucket_tagging", {}),
        ("put_bucket_tagging", {"Tagging": {"TagSet": [{"Key": "probe", "Value": "not-allowed"}]}}),
    ):
        try:
            response = getattr(client, operation)(Bucket=outside_bucket, **arguments)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if error.get("Code") not in {"AccessDenied", "Forbidden"} and status != 403:
                raise
        else:
            if operation == "get_object":
                response["Body"].close()
            raise RuntimeError(f"The test S3 identity can {operation} outside its harness scope")
except Exception as exc:
    probe_error = exc
    raise
finally:
    cleanup_actions: list[Callable[[], None]] = []
    if bucket_creation_attempted:
        cleanup_actions.append(partial(remove_probe_bucket, bucket))
    if outside_bucket_creation_attempted:
        cleanup_actions.append(partial(remove_probe_bucket, outside_bucket))
    cleanup_actions.extend((client.close, cleanup_client.close))
    cleanup_error: Exception | None = None
    for cleanup in cleanup_actions:
        try:
            cleanup()
        except Exception as exc:
            print(f"Local S3 probe cleanup failed: {exc}", file=sys.stderr)
            if cleanup_error is None:
                cleanup_error = exc
    if cleanup_error is not None:
        if probe_error is not None:
            raise probe_error from cleanup_error
        raise cleanup_error
print("Local S3 read/write passed; out-of-scope bucket, object, listing and tagging operations denied")
PY
config_tmp="$(mktemp "${state}/s3.conf.XXXXXX")"
temporary_files+=("${config_tmp}")
printf 'S3_ENDPOINT=%s\nS3_ACCESS_KEY=%s\nS3_SECRET_KEY=%s\n' \
    "${S3_ENDPOINT}" "${S3_ACCESS_KEY}" "${S3_SECRET_KEY}" > "${config_tmp}"
printf 'S3_CLEANUP_ACCESS_KEY=%s\nS3_CLEANUP_SECRET_KEY=%s\nS3_BUCKET_NAMESPACE=local\n' \
    "${S3_CLEANUP_ACCESS_KEY}" "${S3_CLEANUP_SECRET_KEY}" >> "${config_tmp}"
mv "${config_tmp}" "${state}/s3.conf"
env_tmp="$(mktemp "${state}/s3.env.XXXXXX")"
temporary_files+=("${env_tmp}")
printf 'export S3_CONFIG_localhost=%q\n' "${state}/s3.conf" > "${env_tmp}"
mv "${env_tmp}" "${state}/s3.env"
echo "Load the localhost cloud's S3 config path with: source ${state}/s3.env"
echo "This service is for this disposable VM only; do not expose it publicly."
