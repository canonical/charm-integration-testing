#!/usr/bin/env bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MISE_VERSION="$(<"$PROJECT_ROOT/.mise-version")"

if [[ -x "$HOME/.local/bin/mise" ]] \
    && [[ "$("$HOME/.local/bin/mise" --version | awk '{print $1}')" == "$MISE_VERSION" ]]; then
    echo "mise $MISE_VERSION is already installed."
else
    case "$(uname -m)" in
        x86_64|amd64) MISE_ARCH=x64 ;;
        aarch64|arm64) MISE_ARCH=arm64 ;;
        *)
            echo "Unsupported architecture for mise: $(uname -m)" >&2
            exit 1
            ;;
    esac

    MISE_ARCHIVE="mise-v${MISE_VERSION}-linux-${MISE_ARCH}.tar.gz"
    MISE_URL="https://github.com/jdx/mise/releases/download/v${MISE_VERSION}"
    TMP_DIR="$(mktemp -d)"
    trap 'rm -rf -- "$TMP_DIR"' EXIT

    curl --fail --location --silent --show-error "${MISE_URL}/SHASUMS256.txt" \
        --output "$TMP_DIR/SHASUMS256.txt"
    curl --fail --location --silent --show-error "${MISE_URL}/${MISE_ARCHIVE}" \
        --output "$TMP_DIR/$MISE_ARCHIVE"
    grep -F " ${MISE_ARCHIVE}" "$TMP_DIR/SHASUMS256.txt" > "$TMP_DIR/checksum.txt"
    (
        cd "$TMP_DIR"
        sha256sum --check checksum.txt
        tar -xzf "$MISE_ARCHIVE" mise
    )

    install -d "$HOME/.local/bin"
    install -m 0755 "$TMP_DIR/mise" "$HOME/.local/bin/mise"
fi

for startup_file in "$HOME/.bashrc" "$HOME/.profile"; do
    if ! grep -Fq '# mise sandbox environment' "$startup_file" 2>/dev/null; then
        cat >> "$startup_file" <<'EOF'

# mise sandbox environment
export MISE_ENV=sandbox
eval "$(~/.local/bin/mise activate bash)"
EOF
    fi
done

"$HOME/.local/bin/mise" --version
