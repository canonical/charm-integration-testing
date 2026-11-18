#!/usr/bin/env bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

export CHARM_QA_HOME="$HOME/.local/share/charm-qa"
export CHARM_QA_ROOT="$CHARM_QA_HOME/current"
export PROJECT_ROOT="$CHARM_QA_ROOT"
export UV_PROJECT_ENVIRONMENT="$CHARM_QA_ROOT/.venv"
export MISE_DATA_DIR="$CHARM_QA_HOME/mise"
export MISE_CACHE_DIR="$CHARM_QA_HOME/cache/mise"
export MISE_CONFIG_DIR="$CHARM_QA_HOME/config/mise"
export UV_CACHE_DIR="$CHARM_QA_HOME/cache/uv"
export PATH="$PATH:/snap/bin"
