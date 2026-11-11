#!/bin/bash
#MISE description="Start a local S3 service in the sandbox"
#MISE raw_args=true
#MISE alias="sandbox:setup-s3"
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

set -euo pipefail
umask 077

state="${HOME}/.local/share/charmqa-s3"
project_root="$(cd "$(dirname "$0")/../.." && pwd)"
for command in jq uv; do
    command -v "${command}" >/dev/null || {
        echo "Missing ${command}; run: mise run setup -- --group local-s3" >&2
        exit 1
    }
done
weed="$(mise which weed 2>/dev/null)" || {
    echo "Missing weed; run: mise run setup -- --group local-s3" >&2
    exit 1
}
for command in curl openssl ip sudo systemctl systemd-run; do
    command -v "${command}" >/dev/null || { echo "Missing prerequisite: ${command}" >&2; exit 1; }
done
if ! sudo -n true; then
    echo "Local S3 setup requires noninteractive sudo; authorize it before running setup" >&2
    exit 1
fi
if [[ -L "${state}" || ( -e "${state}" && ( ! -d "${state}" || ! -O "${state}" ) ) ]]; then
    echo "S3 state must be an owned directory, not a symlink: ${state}" >&2
    exit 1
fi
mkdir -p "${state}"
chmod 700 "${state}"
if [[ -L "${state}/data" || ( -e "${state}/data" && ( ! -d "${state}/data" || ! -O "${state}/data" ) ) ]]; then
    echo "S3 state path must be an owned directory, not a symlink: ${state}/data" >&2
    exit 1
fi
mkdir -p "${state}/data"
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

if [[ ! -f "${state}/s3.json" ]]; then
    export S3_ACCESS_KEY S3_SECRET_KEY
    S3_ACCESS_KEY="$(openssl rand -hex 16)"
    S3_SECRET_KEY="$(openssl rand -hex 32)"
    config_tmp="$(mktemp "${state}/s3.json.XXXXXX")"
    temporary_files+=("${config_tmp}")
    jq -n '{identities: [
        {name: "charmqa-tests", credentials: [{
            accessKey: env.S3_ACCESS_KEY, secretKey: env.S3_SECRET_KEY #gitleaks:allow
        }], actions: [
            "Admin:charmqa-s3-*", "List:charmqa-s3-*"
        ]}
    ]}' > "${config_tmp}"
    mv "${config_tmp}" "${state}/s3.json"
fi
if ! jq -e '
    (.identities | length == 1)
    and (.identities[0].name == "charmqa-tests")
    and (.identities[0].actions == ["Admin:charmqa-s3-*", "List:charmqa-s3-*"])
    and (.identities[0].credentials[0].accessKey | type == "string" and length > 0)
    and (.identities[0].credentials[0].secretKey | type == "string" and length > 0)
' "${state}/s3.json" >/dev/null; then
    echo "Existing S3 credentials use an unsupported format; inspect ${state}/s3.json before replacing it" >&2
    exit 1
fi
address="$(ip -4 route get 1.1.1.1 | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
[[ -n "${address}" ]] || { echo "Cannot determine a VM address reachable from units" >&2; exit 1; }
export S3_ENDPOINT="http://${address}:8333"
export S3_ACCESS_KEY S3_SECRET_KEY
S3_ACCESS_KEY="$(jq -r '.identities[0].credentials[0].accessKey' "${state}/s3.json")"
S3_SECRET_KEY="$(jq -r '.identities[0].credentials[0].secretKey' "${state}/s3.json")"
if ! sudo -n systemctl is-active --quiet charmqa-s3.service; then
    if sudo -n systemctl cat charmqa-s3.service >/dev/null 2>&1; then
        echo "Existing charmqa-s3 service is not active; inspect journalctl before restarting" >&2
        exit 1
    fi
    sudo -n systemd-run --no-block --unit=charmqa-s3 \
        --property="User=$(id -un)" --property="WorkingDirectory=${state}" \
        "${weed}" server -ip=127.0.0.1 -ip.bind=127.0.0.1 -dir="${state}/data" \
        -volume.port=18080 -volume.max=0 -master.volumeSizeLimitMB=64 -filer.port=18888 \
        -s3 -s3.ip.bind="${address}" -s3.port=8333 -s3.port.iceberg=0 -s3.port.lance=0 \
        -s3.config="${state}/s3.json"
fi
ready=0
for _ in $(seq 1 30); do
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
uv run --no-sync --all-packages python3 - <<'PY'
import os
from contextlib import ExitStack
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from s3_storage import S3Credentials, StaticS3Provider


credentials = S3Credentials(os.environ["S3_ACCESS_KEY"], os.environ["S3_SECRET_KEY"])
provider = StaticS3Provider(os.environ["S3_ENDPOINT"], "local", credentials)
bucket = provider.new_bucket()
with ExitStack() as cleanup:
    cleanup.callback(bucket.cleanup)
    name, _ = bucket.provision()
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT"],
        aws_access_key_id=credentials.access_key,
        aws_secret_access_key=credentials.secret_key,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, request_checksum_calculation="when_required"),
    )
    cleanup.callback(client.close)
    client.put_object(Bucket=name, Key="probe", Body=b"local-s3")
    result = client.get_object(Bucket=name, Key="probe")
    try:
        if result["Body"].read() != b"local-s3":
            raise RuntimeError("Local S3 canary content mismatch")
    finally:
        result["Body"].close()
    outside_bucket = "charmqa-outside-probe-" + uuid4().hex
    try:
        client.create_bucket(Bucket=outside_bucket)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if error.get("Code") not in {"AccessDenied", "Forbidden"} and status != 403:
            raise
    else:
        cleanup.callback(client.delete_bucket, Bucket=outside_bucket)
        raise RuntimeError("The S3 identity can create buckets outside its harness scope")
print("Local S3 bucket lifecycle and read/write passed; out-of-scope bucket creation denied")
PY
config_tmp="$(mktemp "${state}/s3.conf.XXXXXX")"
temporary_files+=("${config_tmp}")
printf 'S3_ENDPOINT=%s\nS3_ACCESS_KEY=%s\nS3_SECRET_KEY=%s\n' \
    "${S3_ENDPOINT}" "${S3_ACCESS_KEY}" "${S3_SECRET_KEY}" > "${config_tmp}"
printf 'S3_BUCKET_NAMESPACE=local\n' >> "${config_tmp}"
mv "${config_tmp}" "${state}/s3.conf"
env_tmp="$(mktemp "${state}/s3.env.XXXXXX")"
temporary_files+=("${env_tmp}")
printf 'export S3_CONFIG_localhost=%q\n' "${state}/s3.conf" > "${env_tmp}"
mv "${env_tmp}" "${state}/s3.env"
echo "Load the localhost cloud's S3 config path with: source ${state}/s3.env"
echo "This service is for this disposable VM only; do not expose it publicly."
