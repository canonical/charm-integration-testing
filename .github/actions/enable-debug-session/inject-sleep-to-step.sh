#!/bin/bash

WORKFLOW_FILE=$1
SCAN_DIRECTORY=$2
STEP_NAME=$3

TEMP_FILE=$(mktemp)
STEP_NAME=${STEP_NAME} yq '.jobs[].steps[] | select(.name == strenv(STEP_NAME)).run' \
    ${WORKFLOW_FILE} > ${TEMP_FILE}

INJECTED=false
while true; do

    for FILE in $(ls -1 ${SCAN_DIRECTORY}/*.sh); do
        if cmp -s ${FILE} ${TEMP_FILE}; then
            echo "Injecting sleep into ${FILE}"
            echo "sleep 3600" >> ${FILE}
            INJECTED=true
            break
        fi
    done

    if [ "$INJECTED" = true ]; then
        break
    fi

done

rm ${TEMP_FILE}