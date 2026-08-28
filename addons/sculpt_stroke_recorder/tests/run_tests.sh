#!/usr/bin/env bash
set -u

BLENDER="${1:-/mnt/c/Program Files/Blender Foundation/Blender 5.1/blender.exe}"
TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -e "$BLENDER" ]; then
    echo "ERROR: Blender binary not found at: $BLENDER" >&2
    exit 2
fi

out="$("$BLENDER" --background --factory-startup \
        --python "$(wslpath -w "$TESTS_DIR/test_register.py")" 2>&1)"
echo "$out" | sed -n '/^  /p;/TESTS_/p;/Traceback/,+15p'
if echo "$out" | grep -q "SCULPT_STROKE_RECORDER_TESTS_PASSED"; then
    echo "ALL_TESTS_PASSED"
    exit 0
fi
echo "$out"
echo "TESTS_FAILED"
exit 1
