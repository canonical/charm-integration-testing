#!/usr/bin/env bash

marker="${XDG_CONFIG_HOME:-$HOME/.config}/charm-integration-testing/development-sandbox"
if [[ ! -f "$marker" ]]; then
    echo "ERROR: This task must run inside the development sandbox VM." >&2
    echo "Start it with scripts/sandbox.sh up, then run the task from the VM." >&2
    exit 1
fi
