# Calipers — Scale-Aware Voxel-Remesh Preview and Safety

Open work is tracked in [ROADMAP.md](ROADMAP.md).

A Blender add-on (prototype for [Proposal §5](../../PROPOSAL.md#5-scale-aware-remesh-preview-and-safety),
built on [research/scale-aware-remesh-safety.md](../../research/scale-aware-remesh-safety.md))
that puts a preflight in front of voxel remeshing: cost estimates and a
risk band before anything expensive runs, scale warnings when the
object-space voxel size would surprise you in world space, a pending
"safe add" for the Remesh modifier, a confirming wrapper for the
destructive operation, and a viewport guide that makes the cell size
visible against the mesh instead of an abstract number.

Requires Blender 4.2+; developed and probe-verified against
Blender 5.1.2.

![Scale-aware voxel-remesh estimates and viewport guide](../../media/screenshots/calipers/Screenshot%202026-08-11%20020208.png)

## The problem

Voxel remeshing launches an unexpectedly expensive operation whenever
the current voxel size is badly mismatched with the mesh:

- The native voxel size is an **object-space** distance (probed: a cube
  with unapplied 2x scale remeshes to the identical vertex count). With
  unapplied or non-uniform scale, the number you typed is not the cell
  size you see.
- The datablock default (`0.1`, read from RNA at runtime — never
  hardcoded here) knows nothing about your mesh. On a 100 m building
  scan it is ~10^9 bounding cells.
- **Adding the Remesh modifier evaluates it immediately**, at that
  default, before you have entered a sensible value. On a dense or
  badly-scaled mesh that first evaluation can stall Blender or exhaust
  memory.
- The number alone communicates neither resolution nor cost.

## Terminology: two entry points, two geometry sources

Blender's destructive **Voxel Remesh operation** and its non-destructive
**Remesh modifier** are separate entry points, and Calipers keeps them
strictly separate (they share arithmetic only through an interface that
names the coordinate space and geometry source):

| | Voxel Remesh **operation** | Remesh **modifier** (VOXEL) |
|---|---|---|
| Invocation | `bpy.ops.object.voxel_remesh`, Sculpt Mode `Ctrl-R` | modifier stack |
| Settings | `Mesh.remesh_voxel_size` (datablock) | `RemeshModifier.voxel_size` |
| Geometry source | **Original mesh data** — probed: ignores modifiers, ignores shape keys (uses basis, then destroys the keys) | whatever the stack produced **before** the modifier |
| Estimate confidence | **exact** (original mesh is readable) | exact only when the input is knowable (see below) |

Modifier-input confidence rules (each case probed on 5.1.2):

- modifier **first** in the stack → input is the base mesh → **exact**;
- modifier **last and disabled** (the safe-add pending state) →
  `evaluated_get()` output *is* the input geometry (probed: 98 subsurf
  verts with a disabled trailing remesh) → **exact**;
- anything else → Python cannot read mid-stack geometry; base-mesh
  stats stand in and the estimate is labeled **approximate** in the UI.

## Install

1. Zip the `calipers` directory (or use it in place from a scripts
   path).
2. Blender → Edit → Preferences → Add-ons → Install… → pick the zip.
3. Enable "Calipers". The panel appears in the 3D Viewport sidebar
   (`N`) under the **Calipers** tab.

`F3` → search also finds the operators ("Calipers: …" entries live in
the Object menu).

## The panel

For the active mesh object, two context boxes plus the guide:

### Voxel Remesh (Destructive Operation)

- **Voxel Size** — the native `Mesh.remesh_voxel_size`, verbatim, in
  object space. Never silently reinterpreted; world-space derivations
  are labeled as such.
- The estimate (see [Scores](#scores-and-risk-bands)): risk band with
  icon, longest-axis cells, bounding-cell score, relative surface
  score, world-space cell sizes per axis, scale warnings, geometry
  source + confidence.
- **Set from World Target** — type the world-space cell size you
  actually want; the helper writes the object-space value through the
  documented conversion: *divide by the largest effective axis scale*
  (a singular value of the world matrix — correct under shear), so no
  transformed axis comes out coarser than the target.
- **Voxel Remesh (Preflight)** — the confirming wrapper: a dialog
  shows the estimate (with an extra warning block in the RED band) and
  runs the native operator on OK. Warnings never hard-block. The
  native operator and its `Ctrl-R` keymap are untouched.

### Remesh Modifier (Voxel Mode)

- **Add Remesh Modifier (Safe)** — adds the modifier **pending**:
  `show_viewport` *and* `show_render` off (probed: disabled in the same
  operator execution, the remesh never evaluates — a render must not
  stall on a modifier whose cost was never confirmed), with an initial
  voxel size derived from the *evaluated* bounds (longest axis /
  *Initial Cells*, default 64) instead of the scale-blind datablock
  default.
- With a VOXEL Remesh modifier present: its voxel size (verbatim), the
  estimate with the confidence label above, Set from World Target, and
  — while pending — an **Enable Remesh Modifier** button. Enabling is
  itself the expensive event, so the estimate is on screen (panel and
  confirm dialog) *before* the toggle.

### Visual Guide

A GPU overlay in the viewport, drawn in the object's local space and
world-transformed — so unapplied scale is *visible* (the guide
stretches exactly like the remesh result would appear):

- the object-space bounding box;
- **eight sample cells**: one inward-facing wireframe cube of edge `v`
  at every bounds corner, colored by the risk band so the guide remains
  visible from any direction without camera-dependent logic;
- three representative **grid slices** (one mid-plane per axis) ruled
  at spacing `v`, capped at 129 lines per direction. A slice over the
  cap is dropped entirely — a partial grid would lie about the density
  — so at extreme density the guide falls back to box + corner cells +
  annotation. It never draws every voxel.
- a text annotation: longest-axis cells + risk band (+ "grid capped").
- an optional **Bounding Box Dimensions** annotation showing the drawn
  box's transformed X/Y/Z edge lengths. This remains honest under
  unapplied/non-uniform scale and shear.

The **Source** selector picks which entry point the guide reads:
*Auto* (modifier when present, else mesh), *Mesh*, or *Modifier*.

### Risk Thresholds

Bands compare `log10(bounding_cells)` against two configurable
exponents: **Yellow At** (default 7 ≈ 215 cells/axis) and **Red At**
(default 9 ≈ 1000 cells/axis). These are honest ballpark defaults, not
calibrated measurements (calibration across hardware is future work,
Implementation sequence §5 of the research doc).

## Scores and risk bands

For object-space bounds `d` and voxel size `v` (all in the SAME
coordinate space — mixing world dimensions with the object-space voxel
size is exactly the bug this add-on prevents):

- `longest_axis_cells = ceil(max(d) / v)` — the intuitive headline.
- `bounding_cells = Π (ceil(d_i / v) + 1)` — saturating integer
  arithmetic (capped at 2^63−1, flagged "saturated"). A conservative
  **domain risk indicator, not an OpenVDB memory prediction** (VDB is
  sparse); the +1 padding per axis is documented honesty about narrow-
  band slop, not physics.
- `surface_cells = area / v²` — a **relative** output-complexity
  score. Never presented as a face count or megabytes: the faces-per-
  cell factor is uncalibrated and version-dependent.
- Scale analysis: warnings for unapplied, non-uniform, and negative
  scale, plus shear — detected and measured via the **singular values**
  of the world matrix's 3×3 block (pure-python Jacobi; under shear the
  three scale properties/column norms lie, singular values do not).

Invalid input (`v ≤ 0`, non-finite bounds) is the *only* thing the
estimator rejects — zero extents are valid (probed: a flat plane voxel-
remeshes fine), and RED is a warning you may override, never a block.

## What an add-on cannot cover

Per the research doc, a wrapper cannot guarantee safety at the native
entry points:

- the **native Add Modifier menu** still evaluates the Remesh modifier
  immediately on add, at the datablock default;
- **direct operator calls** — Sculpt Mode `Ctrl-R`, F3 search on the
  native operator, scripts calling `bpy.ops.object.voxel_remesh` —
  bypass the preflight entirely (Calipers deliberately does not touch
  the native keymap);
- accurate OpenVDB allocation info is not exposed to Python, so all
  scores stay relative.

Changing the add-modifier default (a pending state or first-run sizing
prompt) and annotating the native Sculpt popover / `R` overlay are
Blender-core work — this add-on is the interaction prototype for that
proposal.

## Headless test suite

```
tests/run_tests.sh    # runs all four files, prints ALL_TESTS_PASSED
```

House pattern: Blender always exits 0 even after a traceback, so each
file prints a unique sentinel (`CALIPERS_*_TESTS_PASSED`) and the
runner greps for it.

- `test_estimate.py` — the pure estimator (also runs under bare
  `python3`): tiny/huge objects, zero extents, empty mesh, unapplied /
  non-uniform / negative scale, shear (exact golden-ratio singular
  values), overflow saturation, world-target conversion, band
  thresholds, invalid-input rejection.
- `test_core.py` — stats extraction, the confidence rules, RNA default
  reads, the cache, debounce plumbing (fake clock).
- `test_register.py` — registration surface, safe-add leaves the
  modifier pending with a bounds-derived size and *nothing evaluated*,
  enable-confirm, set-from-world-target math against the pure helper,
  the confirming wrapper (including clean `v = 0` rejection), overlay
  toggle round-trip, clean unregister.
- `test_overlay.py` — pure guide-geometry builders (counts, caps,
  fallback), shader create-info descriptor, the draw error latch
  (headless GPU boundary), AST audit that every `gpu.state.*_set` sits
  inside the restore guard.

GPU objects cannot be created in `--background` (SystemError — probed),
which is why all shader/batch work is lazy at draw time behind a latch
and enabling the overlay headless is a harmless no-op.

## Probe-verified 5.1.2 findings this add-on is built on

See `PROGRESS.md` for the full list, including where the (written-
blind) research doc needed correction: native C operators are absent
from `bpy.types` (probe with `get_rna_type()`), `Modifier.show_viewport`
RNA default disagrees with factory behavior, `remesh_voxel_size = 0.0`
is assignable and makes the native op raise, evaluated `bound_box` is
not tight to evaluated geometry (vertex scans are), and the destructive
operation ignores modifiers and shape keys.

## Files

- `__init__.py` — bl_info, settings, operators, panel, handlers/timer.
- `estimate.py` — pure estimator (no bpy; runs under bare python3).
- `core.py` — geometry stats, cache, entry-point/confidence resolution.
- `live.py` — pure debounce (fake-clock testable).
- `overlay.py` — GPU viewport guide + blf annotation.
- `probes/` — one-shot API probe scripts (not part of the suite).
- `tests/` — the headless suite.
