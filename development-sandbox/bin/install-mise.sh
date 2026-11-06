#!/usr/bin/env bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MISE_VERSION="v$(<"$PROJECT_ROOT/.mise-version")"
MISE_BIN="$HOME/.local/bin/mise"

installed_version=""
if [[ -x "$MISE_BIN" ]]; then
    installed_version="$("$MISE_BIN" --version 2>/dev/null | awk 'NR == 1 {print $1}')" || installed_version=""
fi
if [[ "$installed_version" != "${MISE_VERSION#v}" ]]; then
    curl --fail --silent --show-error --location \
        "https://github.com/jdx/mise/releases/download/$MISE_VERSION/install.sh" \
        | MISE_VERSION="$MISE_VERSION" MISE_INSTALL_PATH="$MISE_BIN" sh
fi

for startup_file in "$HOME/.bashrc" "$HOME/.profile"; do
    if [[ -f "$startup_file" ]]; then
        sed -i \
            -e '/^export MISE_ENV=\(sandbox\|development\|development,provisioning,sandbox\)$/d' \
            -e '\|^export MISE_OVERRIDE_CONFIG_FILENAMES=mise.toml:development-sandbox/mise.toml$|d' \
            "$startup_file"
    fi
    if ! grep -Fq '# mise sandbox environment' "$startup_file" 2>/dev/null; then
        cat >> "$startup_file" <<'EOF'

# mise sandbox environment
eval "$(~/.local/bin/mise activate bash)"
EOF
    fi
done

"$MISE_BIN" --version
