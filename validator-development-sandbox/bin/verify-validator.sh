#!/bin/bash
# Run deterministic validator verification gates inside the VM workspace.
#
# This script is VM-native and must be run from inside the validator VM where
# /project is mounted.

set -euo pipefail

MODEL=""
APP=""
PROVIDER=""
VALIDATOR=""
INTERFACE=""
LEVEL="simple"
DOWN_MODE="scale"
PROVIDER_UNITS="auto"
OUTPUT_DIR=""
TEST_CMD=""
DOWN_CMD=""
RESTORE_CMD=""

usage() {
    cat <<'EOF'
Usage:
  validator-development-sandbox/bin/verify-validator.sh \
    --model <model> --app <requirer-app> --provider <provider-app> --validator <validator-name> [options]

Required:
  --model <name>         Juju model to validate
  --app <name>           Requirer application to run dev-validate against
  --provider <name>      Provider application used for workload-down checks
  --validator <name>     Validator package folder name (for wiring checks), e.g. s3

Optional:
  --interface <name>     Endpoint interface name (default: same as --validator)
  --level <level>        Validation level for dev-validate (default: simple)
  --down-mode <mode>     Workload-down method: scale (default)
  --provider-units <n>   Units to restore provider to (default: auto from juju status)
  --down-cmd <cmd>       Override the provider-down command entirely (e.g. for k8s-only backends).
                         When set, --restore-cmd must also be provided.
  --restore-cmd <cmd>    Override the provider-restore command (paired with --down-cmd).
  --output-dir <path>    Evidence output directory (default: /tmp/validator-verification-<timestamp>)
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
        --level)
            LEVEL="${2:-}"
            shift 2
            ;;
        --down-mode)
            DOWN_MODE="${2:-}"
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
    OUTPUT_DIR="/tmp/validator-verification-$(date +%Y%m%d-%H%M%S)"
fi

if [ -z "$TEST_CMD" ]; then
    TEST_CMD="poetry run pytest validators/$VALIDATOR/tests/unit -q"
fi

if [ "$DOWN_MODE" != "scale" ]; then
    echo "Unsupported --down-mode '$DOWN_MODE'. Supported: scale" >&2
    exit 1
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

if [ ! -d /project ]; then
    echo "This script must run inside the validator VM where /project is mounted." >&2
    exit 1
fi

cd /project
mkdir -p "$OUTPUT_DIR"
summary="$OUTPUT_DIR/summary.txt"
report="$OUTPUT_DIR/report.json"

printf "Validator verification summary\n" > "$summary"
printf "model=%s app=%s provider=%s validator=%s interface=%s level=%s\n\n" \
    "$MODEL" "$APP" "$PROVIDER" "$VALIDATOR" "$INTERFACE" "$LEVEL" >> "$summary"

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

interface_status_check() {
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

if not statuses:
    sys.exit(4)

if mode == "up":
    has_bad = any(s in ("FAIL", "ERROR") for s in statuses)
    has_pass = any(s == "PASS" for s in statuses)
    sys.exit(0 if has_pass and not has_bad else 1)

if mode == "down":
    has_down_signal = any(s in ("FAIL", "ERROR") for s in statuses)
    sys.exit(0 if has_down_signal else 1)

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
run_step wiring_root "grep -q '^validators-$VALIDATOR_PKG = { path = \"./validators/$VALIDATOR\", develop = true' pyproject.toml"
run_step entrypoint "cd /project && poetry run python3 -c \"from importlib.metadata import entry_points; import sys; names=[e.name for e in entry_points(group='endpoint_validators')]; sys.exit(0 if '$INTERFACE' in names else 1)\""

run_step validate_up "/project/validator-development-sandbox/bin/dev-validate.py --model $MODEL --app $APP --level $LEVEL --reinstall"
run_step status_up "juju status -m $MODEL --relations"

if [ -n "$DOWN_CMD" ]; then
    # Custom down/restore commands override the default Juju scale behavior.
    run_step provider_down "$DOWN_CMD"
    run_step status_down "juju status -m $MODEL --relations"
    run_step validate_down "/project/validator-development-sandbox/bin/dev-validate.py --model $MODEL --app $APP --level $LEVEL --reinstall"
    run_step provider_restore "$RESTORE_CMD"
    run_step status_restored "juju status -m $MODEL --relations"
else
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

    run_step provider_down "juju scale-application -m $MODEL $PROVIDER 0"
    run_step status_down "juju status -m $MODEL --relations"
    run_step validate_down "/project/validator-development-sandbox/bin/dev-validate.py --model $MODEL --app $APP --level $LEVEL --reinstall"
    run_step provider_restore "juju scale-application -m $MODEL $PROVIDER $orig_units"
    run_step status_restored "juju status -m $MODEL --relations"
fi

up_pass=false
down_detected=false
quality_pass=false
wiring_pass=false

if [ "${STEP_RC[validate_up]:-1}" -eq 0 ] && interface_status_check "$OUTPUT_DIR/validate_up.out" up; then
    up_pass=true
fi
if [ "${STEP_RC[validate_down]:-0}" -ne 0 ] && interface_status_check "$OUTPUT_DIR/validate_down.out" down; then
    down_detected=true
fi
if [ "${STEP_RC[format]:-1}" -eq 0 ] && [ "${STEP_RC[lint]:-1}" -eq 0 ] && [ "${STEP_RC[unit_tests]:-1}" -eq 0 ]; then
    quality_pass=true
fi
if [ "${STEP_RC[wiring_runner]:-1}" -eq 0 ] && [ "${STEP_RC[wiring_root]:-1}" -eq 0 ] && [ "${STEP_RC[entrypoint]:-1}" -eq 0 ]; then
    wiring_pass=true
fi

overall=1
if [ "$quality_pass" = "true" ] && [ "$wiring_pass" = "true" ] && [ "$up_pass" = "true" ] && [ "$down_detected" = "true" ]; then
    overall=0
fi

cat > "$report" <<EOF
{
  "model": "$MODEL",
  "app": "$APP",
  "provider": "$PROVIDER",
  "validator": "$VALIDATOR",
  "interface": "$INTERFACE",
  "level": "$LEVEL",
  "output_dir": "$OUTPUT_DIR",
  "steps": {
    "pre_status": ${STEP_RC[pre_status]:-1},
    "format": ${STEP_RC[format]:-1},
    "lint": ${STEP_RC[lint]:-1},
    "unit_tests": ${STEP_RC[unit_tests]:-1},
    "wiring_runner": ${STEP_RC[wiring_runner]:-1},
    "wiring_root": ${STEP_RC[wiring_root]:-1},
    "entrypoint": ${STEP_RC[entrypoint]:-1},
    "validate_up": ${STEP_RC[validate_up]:-1},
    "status_up": ${STEP_RC[status_up]:-1},
    "provider_down": ${STEP_RC[provider_down]:-1},
    "status_down": ${STEP_RC[status_down]:-1},
    "validate_down": ${STEP_RC[validate_down]:-1},
    "provider_restore": ${STEP_RC[provider_restore]:-1},
    "status_restored": ${STEP_RC[status_restored]:-1}
  },
  "checks": {
    "quality_pass": $quality_pass,
    "wiring_pass": $wiring_pass,
    "workload_up_pass": $up_pass,
    "workload_down_detected": $down_detected
  },
  "overall_exit_code": $overall
}
EOF

printf "Final checks:\n" >> "$summary"
printf "  quality_pass=%s\n" "$quality_pass" >> "$summary"
printf "  wiring_pass=%s\n" "$wiring_pass" >> "$summary"
printf "  workload_up_pass=%s\n" "$up_pass" >> "$summary"
printf "  workload_down_detected=%s\n" "$down_detected" >> "$summary"
printf "  report=%s\n" "$report" >> "$summary"
printf "  overall_exit_code=%s\n" "$overall" >> "$summary"

cat "$summary"
echo "OUTPUT_DIR=$OUTPUT_DIR"
exit "$overall"
