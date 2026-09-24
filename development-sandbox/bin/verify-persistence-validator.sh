#!/bin/bash
# Run deterministic persistence-validator verification gates inside the VM workspace.
#
# This script is VM-native and must be run from inside the sandbox VM where
# the project is mounted. The project root is auto-detected from this script's
# location or can be overridden via the PROJECT_ROOT environment variable.
#
# It is the persistence counterpart of verify-validator.sh: instead of checking
# that a functional validator detects a workload outage, it drives the
# prepare -> disrupt -> checkpoint -> cleanup lifecycle and asserts that canary
# data seeded before the disruption is still present afterwards.

set -euo pipefail

# Derive project root from this script's location: <project>/development-sandbox/bin/verify-persistence-validator.sh
PROJECT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"

MODEL=""
APP=""
PROVIDER=""
VALIDATOR=""
INTERFACE=""
PROVIDER_UNITS="auto"
OUTPUT_DIR=""
TEST_CMD=""
DOWN_CMD=""
RESTORE_CMD=""

usage() {
    cat <<'EOF'
Usage:
  development-sandbox/bin/verify-persistence-validator.sh \
    --model <model> --app <app> --provider <provider> --validator <validator-name> [options]

Required:
  --model <name>         Juju model to run the persistence lifecycle in
  --app <name>           Application whose units run the persistence validator
                         (the side the validator's role gating applies to)
  --provider <name>      Application disrupted between prepare and checkpoint
  --validator <name>     Validator package folder name (for wiring checks), e.g. postgresql_client

Optional:
  --interface <name>     Endpoint interface name (default: same as --validator)
  --provider-units <n>   Units to restore provider to (default: auto from juju status)
  --down-cmd <cmd>       Override the disruption command entirely (e.g. for k8s-only backends).
                         When set, --restore-cmd must also be provided.
  --restore-cmd <cmd>    Override the restore command (paired with --down-cmd).
  --output-dir <path>    Evidence output directory (default: /tmp/persistence-verification-<timestamp>)
  --test-cmd <cmd>       Override validator unit-test command
                         (default: poetry run pytest validators/<validator>/tests/unit -q)
  -h, --help             Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --model)
            MODEL="${2:-}"
            shift 2
            ;;
        --app)
            APP="${2:-}"
            shift 2
            ;;
        --provider)
            PROVIDER="${2:-}"
            shift 2
            ;;
        --validator)
            VALIDATOR="${2:-}"
            shift 2
            ;;
        --interface)
            INTERFACE="${2:-}"
            shift 2
            ;;
        --provider-units)
            PROVIDER_UNITS="${2:-}"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="${2:-}"
            shift 2
            ;;
        --test-cmd)
            TEST_CMD="${2:-}"
            shift 2
            ;;
        --down-cmd)
            DOWN_CMD="${2:-}"
            shift 2
            ;;
        --restore-cmd)
            RESTORE_CMD="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 1
            ;;
    esac
done

[ -n "$MODEL" ] || { echo "Missing required --model" >&2; exit 1; }
[ -n "$APP" ] || { echo "Missing required --app" >&2; exit 1; }
[ -n "$PROVIDER" ] || { echo "Missing required --provider" >&2; exit 1; }
[ -n "$VALIDATOR" ] || { echo "Missing required --validator" >&2; exit 1; }
INTERFACE="${INTERFACE:-$VALIDATOR}"

if [ -z "$OUTPUT_DIR" ]; then
    OUTPUT_DIR="/tmp/persistence-verification-$(date +%Y%m%d-%H%M%S)"
fi

if [ -z "$TEST_CMD" ]; then
    TEST_CMD="poetry run pytest validators/$VALIDATOR/tests/unit -q"
fi

# --down-cmd and --restore-cmd must be supplied together.
if [ -n "$DOWN_CMD" ] && [ -z "$RESTORE_CMD" ]; then
    echo "--down-cmd requires --restore-cmd to also be set" >&2
    exit 1
fi
if [ -z "$DOWN_CMD" ] && [ -n "$RESTORE_CMD" ]; then
    echo "--restore-cmd requires --down-cmd to also be set" >&2
    exit 1
fi

if [ ! -d "$PROJECT" ]; then
    echo "This script must run inside the sandbox VM where the project is mounted." >&2
    exit 1
fi

cd "$PROJECT"
mkdir -p "$OUTPUT_DIR"
summary="$OUTPUT_DIR/summary.txt"
report="$OUTPUT_DIR/report.json"
state_file="$OUTPUT_DIR/persistence-state.json"

printf "Persistence validator verification summary\n" > "$summary"
printf "model=%s app=%s provider=%s validator=%s interface=%s\n\n" \
    "$MODEL" "$APP" "$PROVIDER" "$VALIDATOR" "$INTERFACE" >> "$summary"

declare -A STEP_RC

run_step() {
    local key="$1"
    shift
    local cmd="$*"
    local out="$OUTPUT_DIR/${key}.out"

    printf "[%s] %s\n" "$key" "$cmd" >> "$summary"
    set +e
    # Pipe through tee so bash's stdout is a pipe (not a plain file redirect).
    # Some tools (e.g. markdownlint-cli2) check for a TTY/pipe on stdout and
    # behave incorrectly when stdout is a regular file.
    bash -lc "$cmd" 2>&1 | tee "$out" > /dev/null
    local rc=${PIPESTATUS[0]}
    set -e
    STEP_RC["$key"]=$rc
    printf "  rc=%s output=%s\n\n" "$rc" "$out" >> "$summary"
}

# A persistence op is only a success if it ran (rc=0) AND every result it
# produced is PASS. Unlike the functional validator, there is no SKIPPED
# result channel here: a relation the validator does not apply to is simply
# absent from the results (PersistenceNotApplicable), so an empty result set
# is not a pass - it means nothing was actually verified.
persistence_op_check() {
    local file="$1"
    local mode="$2"
    python3 - "$file" "$INTERFACE" "$mode" <<"PY"
import json
import sys

file_path, interface, mode = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(file_path, "r", encoding="utf-8", errors="replace").read()

marker = "--- JSON ---"
if marker not in text:
    sys.exit(2)

payload = text.split(marker, 1)[1].strip()
try:
    data = json.loads(payload)
except Exception:
    sys.exit(3)

statuses = []
for unit_results in data.values():
    for result in unit_results:
        if result.get("interface") == interface:
            statuses.append(result.get("status"))

if mode == "checkpoint":
    # The whole point of the run: canary data seeded by prepare() must still be
    # present after the disruption, so every result must be PASS and there must
    # be at least one (an empty set means the validator never ran).
    sys.exit(0 if statuses and all(s == "PASS" for s in statuses) else 1)

if mode == "prepare":
    # prepare() makes no assertions, so it should produce no results at all;
    # any FAIL/ERROR means seeding itself broke.
    sys.exit(0 if not any(s in ("FAIL", "ERROR") for s in statuses) else 1)

sys.exit(5)
PY
}

run_step pre_status "juju status -m $MODEL --relations"
run_step format "./scripts/format.sh"
run_step lint "./scripts/lint.sh"
run_step unit_tests "$TEST_CMD"

# Poetry normalises underscores to hyphens in distribution names, so derive
# the expected package name from the validator directory name.
VALIDATOR_PKG="${VALIDATOR//_/-}"
run_step wiring_runner "grep -q '\"validators-$VALIDATOR_PKG\"' validators/runner/pyproject.toml"
run_step wiring_root "grep -q '^validators-$VALIDATOR_PKG = { path = \"./validators/$VALIDATOR\"' pyproject.toml"
run_step entrypoint "cd '$PROJECT' && poetry run python3 -c \"from importlib.metadata import entry_points; import sys; names=[e.name for e in entry_points(group='endpoint_persistence_validators')]; sys.exit(0 if '$INTERFACE' in names else 1)\""

# Seed canary data while the workload is up. --reinstall pushes the latest
# validator code onto the units.
run_step prepare "$PROJECT/development-sandbox/bin/dev-persistence.py --model $MODEL --app $APP --op prepare --state-file $state_file --reinstall"
run_step status_prepared "juju status -m $MODEL --relations"

# Disrupt the provider, then restore it and wait for the model to settle before
# checkpointing. Checkpointing while the disruption is still in flight would
# test the wrong thing (and can produce ERROR/skipped results rather than a
# genuine data-survival verdict).
if [ "$PROVIDER_UNITS" = "auto" ]; then
    # juju snap cannot redirect output to files directly, so capture via pipe.
    _status_file=$(mktemp /tmp/juju-status-XXXXXX.json)
    juju status -m "$MODEL" --format=json | cat > "$_status_file"
    orig_units=$(python3 - "$PROVIDER" "$_status_file" <<'PY'
import json, sys
provider, status_file = sys.argv[1], sys.argv[2]
with open(status_file) as f:
    data = json.load(f)
app = data.get("applications", {}).get(provider, {})
scale = app.get("scale")
if isinstance(scale, int):
    print(scale)
else:
    units = app.get("units", {})
    print(len(units) if isinstance(units, dict) else 1)
PY
    )
    rm -f "$_status_file"
else
    orig_units="$PROVIDER_UNITS"
fi

if [ -z "$orig_units" ] || [ "$orig_units" -lt 1 ]; then
    orig_units=1
fi

if [ -n "$DOWN_CMD" ]; then
    run_step provider_down "$DOWN_CMD"
    run_step status_down "juju status -m $MODEL --relations"
    run_step provider_restore "$RESTORE_CMD"
else
    run_step provider_down "juju scale-application -m $MODEL $PROVIDER 0"
    run_step status_down "juju status -m $MODEL --relations"
    run_step provider_restore "juju scale-application -m $MODEL $PROVIDER $orig_units"
fi

run_step status_restored "juju status -m $MODEL --relations"
# Wait for the provider and validator units to actually exist, be active, and be idle.
# Juju can report an application as active while replacing a unit, and `wait-for unit` may then
# finish when the original unit disappears. Polling status avoids checkpointing during that gap.
read -r -d '' wait_cmd <<EOF || true
for attempt in \$(seq 1 180); do
    if juju status -m "$MODEL" --format=json | cat | python3 -c 'import json, sys; d=json.load(sys.stdin); apps=d.get("applications", {}); provider=apps.get(sys.argv[1], {}); app=apps.get(sys.argv[2], {}); expected=int(sys.argv[3]); provider_units=provider.get("units", {}); app_units=app.get("units", {}); good_provider=provider.get("application-status", {}).get("current") == "active" and len(provider_units) >= expected and all(u.get("juju-status", {}).get("current") == "idle" and u.get("workload-status", {}).get("current") == "active" for u in provider_units.values()); good_app=app.get("application-status", {}).get("current") == "active" and bool(app_units) and all(u.get("juju-status", {}).get("current") == "idle" and u.get("workload-status", {}).get("current") == "active" for u in app_units.values()); sys.exit(0 if good_provider and good_app else 1)' "$PROVIDER" "$APP" "$orig_units"; then
        exit 0
    fi
    sleep 5
done
exit 1
EOF
run_step wait_settled "$wait_cmd"

# Verify the canary data survived the disruption.
run_step checkpoint "$PROJECT/development-sandbox/bin/dev-persistence.py --model $MODEL --app $APP --op checkpoint --state-file $state_file"

# Drop the canary data.
run_step cleanup "$PROJECT/development-sandbox/bin/dev-persistence.py --model $MODEL --app $APP --op cleanup --state-file $state_file"

quality_pass=false
wiring_pass=false
prepare_pass=false
checkpoint_pass=false
cleanup_pass=false

if [ "${STEP_RC[format]:-1}" -eq 0 ] && [ "${STEP_RC[lint]:-1}" -eq 0 ] && [ "${STEP_RC[unit_tests]:-1}" -eq 0 ]; then
    quality_pass=true
fi
if [ "${STEP_RC[wiring_runner]:-1}" -eq 0 ] && [ "${STEP_RC[wiring_root]:-1}" -eq 0 ] && [ "${STEP_RC[entrypoint]:-1}" -eq 0 ]; then
    wiring_pass=true
fi
if [ "${STEP_RC[prepare]:-1}" -eq 0 ] && persistence_op_check "$OUTPUT_DIR/prepare.out" prepare; then
    prepare_pass=true
fi
if [ "${STEP_RC[checkpoint]:-1}" -eq 0 ] && persistence_op_check "$OUTPUT_DIR/checkpoint.out" checkpoint; then
    checkpoint_pass=true
fi
if [ "${STEP_RC[cleanup]:-1}" -eq 0 ]; then
    cleanup_pass=true
fi

overall=1
if [ "$quality_pass" = "true" ] && [ "$wiring_pass" = "true" ] && [ "$prepare_pass" = "true" ] \
    && [ "$checkpoint_pass" = "true" ] && [ "$cleanup_pass" = "true" ]; then
    overall=0
fi

cat > "$report" <<EOF
{
  "model": "$MODEL",
  "app": "$APP",
  "provider": "$PROVIDER",
  "validator": "$VALIDATOR",
  "interface": "$INTERFACE",
  "output_dir": "$OUTPUT_DIR",
  "steps": {
    "pre_status": ${STEP_RC[pre_status]:-1},
    "format": ${STEP_RC[format]:-1},
    "lint": ${STEP_RC[lint]:-1},
    "unit_tests": ${STEP_RC[unit_tests]:-1},
    "wiring_runner": ${STEP_RC[wiring_runner]:-1},
    "wiring_root": ${STEP_RC[wiring_root]:-1},
    "entrypoint": ${STEP_RC[entrypoint]:-1},
    "prepare": ${STEP_RC[prepare]:-1},
    "status_prepared": ${STEP_RC[status_prepared]:-1},
    "provider_down": ${STEP_RC[provider_down]:-1},
    "status_down": ${STEP_RC[status_down]:-1},
    "provider_restore": ${STEP_RC[provider_restore]:-1},
    "status_restored": ${STEP_RC[status_restored]:-1},
    "wait_settled": ${STEP_RC[wait_settled]:-1},
    "checkpoint": ${STEP_RC[checkpoint]:-1},
    "cleanup": ${STEP_RC[cleanup]:-1}
  },
  "checks": {
    "quality_pass": $quality_pass,
    "wiring_pass": $wiring_pass,
    "prepare_pass": $prepare_pass,
    "checkpoint_pass": $checkpoint_pass,
    "cleanup_pass": $cleanup_pass
  },
  "overall_exit_code": $overall
}
EOF

printf "Final checks:\n" >> "$summary"
printf "  quality_pass=%s\n" "$quality_pass" >> "$summary"
printf "  wiring_pass=%s\n" "$wiring_pass" >> "$summary"
printf "  prepare_pass=%s\n" "$prepare_pass" >> "$summary"
printf "  checkpoint_pass=%s\n" "$checkpoint_pass" >> "$summary"
printf "  cleanup_pass=%s\n" "$cleanup_pass" >> "$summary"
printf "  report=%s\n" "$report" >> "$summary"
printf "  overall_exit_code=%s\n" "$overall" >> "$summary"

cat "$summary"
echo "OUTPUT_DIR=$OUTPUT_DIR"
exit "$overall"
