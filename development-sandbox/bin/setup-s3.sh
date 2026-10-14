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
mkdir -p "${state}/data" "${state}/bin"
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
    export S3_ACCESS_KEY S3_SECRET_KEY
    S3_ACCESS_KEY="$(openssl rand -hex 16)"
    S3_SECRET_KEY="$(openssl rand -hex 32)"
    jq -n '{identities: [{name: "charmqa", credentials: [{
        accessKey: env.S3_ACCESS_KEY, secretKey: env.S3_SECRET_KEY #gitleaks:allow
    }], actions: ["Admin", "Read", "Write", "List", "Tagging"]}]}' > "${state}/s3.json"
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
    sudo -n systemd-run --unit=charmqa-s3 \
        --property="User=$(id -un)" --property="WorkingDirectory=${state}" \
        "${state}/bin/weed" server -ip=127.0.0.1 -ip.bind=127.0.0.1 -dir="${state}/data" \
        -volume.port=18080 -volume.max=0 -master.volumeSizeLimitMB=64 -filer.port=18888 \
        -s3 -s3.ip.bind="${address}" -s3.port=8333 -s3.port.iceberg=0 -s3.port.lance=0 \
        -s3.config="${state}/s3.json"
fi
ready=0
for attempt in $(seq 1 30); do
    if curl --silent --connect-timeout 1 --max-time 2 --output /dev/null "${S3_ENDPOINT}"; then
        ready=1
        break
    fi
    sleep 1
done
if (( ! ready )); then
    echo "SeaweedFS is not reachable at ${S3_ENDPOINT}; credentials were not written" >&2
    exit 1
fi
cd "${project_root}"
poetry run python - <<'PY'
import os
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

client = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT"],
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
    region_name="us-east-1",
    config=Config(s3={"addressing_style": "path"}, request_checksum_calculation="when_required"),
)
bucket = "charmqa-local-probe-" + uuid4().hex
bucket_creation_attempted = False
try:
    bucket_creation_attempted = True
    client.create_bucket(Bucket=bucket)
    client.put_object(Bucket=bucket, Key="probe", Body=b"local-s3")
    result = client.get_object(Bucket=bucket, Key="probe")
    try:
        assert result["Body"].read() == b"local-s3"
    finally:
        result["Body"].close()
finally:
    try:
        if bucket_creation_attempted:
            try:
                client.delete_object(Bucket=bucket, Key="probe")
            except ClientError as exc:
                error = exc.response.get("Error", {})
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if error.get("Code") not in {"NoSuchKey", "NoSuchBucket"} and status != 404:
                    raise
            finally:
                try:
                    client.delete_bucket(Bucket=bucket)
                except ClientError as exc:
                    error = exc.response.get("Error", {})
                    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    if error.get("Code") != "NoSuchBucket" and status != 404:
                        raise
    finally:
        client.close()
print("Local S3 write/read/delete passed")
PY
printf 'export S3_ENDPOINT=%q\nexport S3_ACCESS_KEY=%q\nexport S3_SECRET_KEY=%q\nexport S3_BUCKET_NAMESPACE=local\n' \
    "${S3_ENDPOINT}" "${S3_ACCESS_KEY}" "${S3_SECRET_KEY}" > "${state}/s3.env"
echo "Load credentials with: source ${state}/s3.env"
echo "This service is for this disposable VM only; do not expose it publicly."
