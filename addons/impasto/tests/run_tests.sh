#!/usr/bin/env bash
# Real-Blender test runner. Blender may exit zero after a Python traceback,
# so success is determined by explicit sentinels.

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
    out="$("$BLENDER" --background --factory-startup --python "$(wslpath -w "$script")" 2>&1)"
    echo "$out" | sed -n '/^  /p;/IMPASTO_/p;/Traceback/,+15p'
    if echo "$out" | grep -q "$sentinel"; then
        echo "--- $(basename "$script"): PASS"
        return 0
    fi
    echo "--- $(basename "$script"): FAIL (sentinel '$sentinel' not found)"
    echo "$out"
    return 1
}

status=0
bash "$TESTS_DIR/test_source_hygiene.sh" || status=1
run_one "$TESTS_DIR/test_package_contract.py" "IMPASTO_PACKAGE_CONTRACT_PASSED" || status=1
run_one "$TESTS_DIR/test_model.py" "MODEL_TESTS_PASSED" || status=1
run_one "$TESTS_DIR/test_integration.py" "IMPASTO_INTEGRATION_PASSED" || status=1
run_one "$TESTS_DIR/test_native_paint.py" "IMPASTO_NATIVE_PAINT_PASSED" || status=1
run_one "$TESTS_DIR/test_multichannel_paint.py" "IMPASTO_MULTICHANNEL_PASSED" || status=1
run_one "$TESTS_DIR/test_gpu_paint.py" "IMPASTO_GPU_PAINT_PASSED" || status=1
run_one "$TESTS_DIR/test_sss_caliper_overlay.py" "IMPASTO_SSS_CALIPER_OVERLAY_PASSED" || status=1
run_one "$TESTS_DIR/test_channel_paint.py" "IMPASTO_CHANNEL_PAINT_PASSED" || status=1
run_one "$TESTS_DIR/test_channel_expansion.py" "IMPASTO_CHANNEL_EXPANSION_PASSED" || status=1
run_one "$TESTS_DIR/test_stencil.py" "IMPASTO_STENCIL_PASSED" || status=1
run_one "$TESTS_DIR/test_preview_modes.py" "IMPASTO_PREVIEW_MODES_PASSED" || status=1
run_one "$TESTS_DIR/test_gpu_preview_contract.py" "IMPASTO_GPU_PREVIEW_CONTRACT_PASSED" || status=1
run_one "$TESTS_DIR/test_ibl_preview.py" "IMPASTO_IBL_PREVIEW_PASSED" || status=1
run_one "$TESTS_DIR/test_preview_stack.py" "IMPASTO_PREVIEW_STACK_PASSED" || status=1
run_one "$TESTS_DIR/test_preview_hardening.py" "preview hardening tests PASSED" || status=1
run_one "$TESTS_DIR/test_brush_undo.py" "IMPASTO_BRUSH_UNDO_PASSED" || status=1
run_one "$TESTS_DIR/test_performance.py" "IMPASTO_PERFORMANCE_ESTIMATES_PASSED" || status=1
run_one "$TESTS_DIR/test_visibility.py" "IMPASTO_VISIBILITY_PASSED" || status=1
run_one "$TESTS_DIR/test_scalar_channels.py" "IMPASTO_SCALAR_CHANNELS_PASSED" || status=1
run_one "$TESTS_DIR/test_rendered_semantics.py" "IMPASTO_RENDERED_SEMANTICS_PASSED" || status=1
run_one "$TESTS_DIR/test_pbr_canvas_semantics.py" "IMPASTO_PBR_CANVAS_PASSED" || status=1
run_one "$TESTS_DIR/test_normal_paint.py" "IMPASTO_NORMAL_PAINT_PASSED" || status=1
run_one "$TESTS_DIR/test_rnm_rebuild.py" "IMPASTO_RNM_REBUILD_PASSED" || status=1
run_one "$TESTS_DIR/test_persistence.py" "IMPASTO_PERSISTENCE_PASSED" || status=1
run_one "$TESTS_DIR/test_flatten_export.py" "flatten export tests passed" || status=1
run_one "$TESTS_DIR/test_restore.py" "IMPASTO_RESTORE_PASSED" || status=1
run_one "$TESTS_DIR/test_undo.py" "IMPASTO_UNDO_PASSED" || status=1
if [ "$status" -eq 0 ]; then
    echo "ALL_TESTS_PASSED"
else
    echo "TESTS_FAILED"
fi
exit "$status"
