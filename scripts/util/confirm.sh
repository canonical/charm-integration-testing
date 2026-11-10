#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Source this file to define confirm.

# Ask a y/N question on the terminal; succeeds only if the user answers yes.
confirm() {
    local answer=""
    if [[ ! -t 0 ]]; then
        echo "No terminal to ask for consent; treating as no." >&2
        return 1
    fi
    read -r -p "$1 [y/N] " answer
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}
