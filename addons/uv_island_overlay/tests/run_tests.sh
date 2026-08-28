#!/usr/bin/env bash
# Run the uv_island_overlay headless test suite against the Windows Blender
# binary from WSL. Blender always exits 0 even when a --python script
# raises, so each test prints a sentinel and we grep for it.
#
# Usage: ./run_tests.sh  [path-to-blender.exe]

set -u

BLENDER="${1:-/mnt/c/Program Files/Blender Foundation/Blender 5.1/blender.exe}"
TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -e "$BLENDER" ]; then
    echo "ERROR: Blender binary not found at: $BLENDER" >&2
    exit 2
fi

run_one() {
    local script="$1"
    local sentinel="$2"
    local out

    echo "=== Running $(basename "$script") ==="
    out="$("$BLENDER" --background --factory-startup \
            --python "$(wslpath -w "$script")" 2>&1)"
    echo "$out" | sed -n '/^  /p;/TESTS_/p;/Traceback/,+15p'

    if echo "$out" | grep -q "$sentinel"; then
        echo "--- $(basename "$script"): PASS"
        return 0
    else
        echo "--- $(basename "$script"): FAIL (sentinel '$sentinel' not found)"
        echo "$out"
        return 1
    fi
}

status=0
run_one "$TESTS_DIR/test_islands.py" "ISLANDS_TESTS_PASSED" || status=1
run_one "$TESTS_DIR/test_density.py" "DENSITY_TESTS_PASSED" || status=1
run_one "$TESTS_DIR/test_health.py" "HEALTH_TESTS_PASSED" || status=1
run_one "$TESTS_DIR/test_culling.py" "CULLING_TESTS_PASSED" || status=1
run_one "$TESTS_DIR/test_combined.py" "COMBINED_TESTS_PASSED" || status=1
run_one "$TESTS_DIR/test_register.py" "REGISTER_TESTS_PASSED" || status=1

echo
if [ "$status" -eq 0 ]; then
    echo "ALL_TESTS_PASSED"
else
    echo "TESTS_FAILED"
fi
exit "$status"
