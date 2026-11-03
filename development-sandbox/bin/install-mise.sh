#!/usr/bin/env bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MISE_VERSION="v$(<"$PROJECT_ROOT/.mise-version")"

curl --fail --silent --show-error --location \
    "https://github.com/jdx/mise/releases/download/$MISE_VERSION/install.sh" \
    | MISE_VERSION="$MISE_VERSION" \
      MISE_INSTALL_PATH="$HOME/.local/bin/mise" \
      MISE_INSTALL_SKIP_IF_EXISTS=1 sh

for startup_file in "$HOME/.bashrc" "$HOME/.profile"; do
    if [[ -f "$startup_file" ]]; then
        sed -i 's/^export MISE_ENV=sandbox$/export MISE_ENV=development/' "$startup_file"
    fi
    if ! grep -Fq '# mise sandbox environment' "$startup_file" 2>/dev/null; then
        cat >> "$startup_file" <<'EOF'

# mise sandbox environment
export MISE_ENV=development
eval "$(~/.local/bin/mise activate bash)"
EOF
    fi
done

"$HOME/.local/bin/mise" --version
