#!/usr/bin/env bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
set -euo pipefail

if ! snap list mise >/dev/null 2>&1; then
    sudo snap install mise --classic --channel=stable
fi

/snap/bin/mise --version
# Existing shims point to the previous installer-managed binary.
/snap/bin/mise reshim --force
rm -f -- "$HOME/.local/bin/mise"

for startup_file in "$HOME/.bashrc" "$HOME/.profile"; do
    if [[ -f "$startup_file" ]]; then
        sed -i \
            -e '/^export MISE_ENV=\(sandbox\|development\|development,provisioning,sandbox\)$/d' \
            -e '\|^export MISE_OVERRIDE_CONFIG_FILENAMES=mise.toml:development-sandbox/mise.toml$|d' \
            -e 's|^eval "\$(~/\.local/bin/mise activate bash)"$|eval "$(/snap/bin/mise activate bash)"|' \
            "$startup_file"
    fi
    if ! grep -Fq '# mise sandbox environment' "$startup_file" 2>/dev/null; then
        cat >> "$startup_file" <<'EOF'

# mise sandbox environment
eval "$(/snap/bin/mise activate bash)"
EOF
    fi
done
