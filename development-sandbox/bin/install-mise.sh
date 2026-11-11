#!/usr/bin/env bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
set -euo pipefail

if ! snap list mise >/dev/null 2>&1; then
    sudo snap install mise --classic --channel=stable
fi

/snap/bin/mise --version

for startup_file in "$HOME/.bashrc" "$HOME/.profile"; do
    if ! grep -Fq '# mise sandbox environment' "$startup_file" 2>/dev/null; then
        cat >> "$startup_file" <<'EOF'

# mise sandbox environment
eval "$(/snap/bin/mise activate bash)"
EOF
    fi
done
