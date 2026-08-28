# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless regression tests for mirrored-overlay culling policy.

Prints CULLING_TESTS_PASSED on success.
"""

import os
import sys
import traceback

import bmesh
import bpy
from mathutils import Matrix

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADDONS_ROOT = os.path.dirname(_ADDON_DIR)
if _ADDONS_ROOT not in sys.path:
    sys.path.insert(0, _ADDONS_ROOT)

from uv_island_overlay import overlay  # noqa: E402


FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def mode(matrix, mesh_orientation=1):
    return overlay._face_culling_mode(matrix, mesh_orientation)


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    identity = Matrix.Identity(4)
    mirror_x = Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0))
    mirror_xy = Matrix.Diagonal((-1.0, -1.0, 1.0, 1.0))
    rotated_mirror = Matrix.Rotation(0.73, 4, 'Z') @ mirror_x
    singular = Matrix.Diagonal((0.0, 1.0, 1.0, 1.0))

    check("ordinary transform culls back faces",
          mode(identity) == 'BACK')
    check("negative object X scale reverses culling",
          mode(mirror_x) == 'FRONT')
    check("two negative object scales preserve culling",
          mode(mirror_xy) == 'BACK')
    check("rotation does not mask a negative determinant",
          mode(rotated_mirror) == 'FRONT')
    check("singular transform uses two-sided drawing",
          mode(singular) == 'NONE')

    # Ctrl+M in Edit Mode reverses a closed shell in mesh space without
    # changing matrix_world.  Its cached orientation is therefore -1.
    check("Ctrl+M-equivalent reversed closed shell reverses culling",
          mode(identity, -1) == 'FRONT')
    check("object mirror plus reversed mesh compose to normal winding",
          mode(mirror_x, -1) == 'BACK')

    # Open/non-manifold or mixed-winding geometry has no trustworthy global
    # polarity.  Two-sided drawing keeps the overlay usable in that case.
    check("ambiguous or open mesh uses two-sided drawing",
          mode(identity, 0) == 'NONE')
    check("ambiguous fallback is independent of object determinant",
          mode(mirror_x, 0) == 'NONE')

    # Exercise the rebuild-time orientation producer on actual Blender
    # meshes.  Mirroring vertex coordinates is the geometry equivalent of
    # Edit Mode Ctrl+M and deliberately leaves matrix_world unchanged.
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    cube = bpy.context.active_object
    check("normal closed cube has positive mesh orientation",
          overlay._mesh_orientation(cube) == 1)
    for vertex in cube.data.vertices:
        vertex.co.x = -vertex.co.x
    cube.data.update()
    check("Ctrl+M-equivalent closed cube has negative mesh orientation",
          overlay._mesh_orientation(cube) == -1)

    bpy.ops.mesh.primitive_grid_add(x_subdivisions=2, y_subdivisions=2,
                                    size=2.0)
    plane = bpy.context.active_object
    check("open plane has ambiguous mesh orientation",
          overlay._mesh_orientation(plane) == 0)

    bpy.ops.mesh.primitive_cube_add(size=2.0)
    mixed = bpy.context.active_object
    bm = bmesh.new()
    bm.from_mesh(mixed.data)
    bm.faces.ensure_lookup_table()
    bm.faces[0].normal_flip()
    bm.to_mesh(mixed.data)
    bm.free()
    mixed.data.update()
    check("locally inconsistent winding has ambiguous orientation",
          overlay._mesh_orientation(mixed) == 0)

    if FAILURES:
        raise AssertionError("%d culling test(s) failed: %s"
                             % (len(FAILURES), ", ".join(FAILURES)))
    print("CULLING_TESTS_PASSED")


try:
    main()
except Exception:
    traceback.print_exc()
    sys.exit(1)
