# UV Island Overlay

Per-island **UV coloring in the 3D viewport** for Blender. Every
UV island of the active mesh is tinted with its own distinct color, drawn
translucently over the surface, so island boundaries are instantly visible
— no round-trip to the UV editor needed. Since v1.1.0 the overlay updates
**live while you mark seams**: islands are predicted
from seam topology, no Unwrap required. Since v1.2.0 the overlay is
**crack-free**: faces are drawn exactly on the surface (shader depth
bias) instead of being pushed apart along their normals, so no more
gaps between adjacent faces. Since v1.3.0 a second display mode
visualizes **texel density as a checkerboard** mapped through the mesh's
actual UVs — islands with mismatched density show visibly different
checker scales on the surface, optionally tinted by how far each island
deviates from the mesh's median density. Since v1.3.1 each mode has a
live **opacity slider** (the density checker is near-opaque by default,
so it reads like paint instead of a translucent film) and the overlay
draws with **handedness-aware face culling**: mirrored objects and
closed shells mirrored in Edit Mode keep painting the camera-facing side.
Since v1.4.0 a third mode — **Islands + Density**, the new default —
shows both at once: the texel-density checker multiplied by each
island's identity color, so **hue reads island membership while
checker scale reads texel density**.

Since v1.5.0 the sidebar also includes a **UV Health** analyzer. It reports and
selects collapsed UV faces, exact duplicate triangle mappings, faces outside
the main 0–1 tile, low-density islands, and islands too narrow to paint
reliably at a chosen texture resolution. Analysis never edits UVs; only an
explicit **Select** action changes face selection.

## Display modes

### Island Colors

![UV islands colored distinctly in the 3D viewport](../../media/screenshots/uv-island-overlay/Screenshot%202026-08-11%20015756.png)

### Texel Density

![Texel-density checkerboard shown on the model](../../media/screenshots/uv-island-overlay/Screenshot%202026-08-11%20015939.png)

### Islands + Density

![UV island colors and texel density shown together](../../media/screenshots/uv-island-overlay/Screenshot%202026-08-11%20015455.png)

## Install

Download `uv_island_overlay-1.5.2.zip` from
[GitHub Releases](https://github.com/Teo-Asinari/blender-workflow-lab/releases)
and install it with **Edit > Preferences > Add-ons > Install from Disk**,
then enable **UV Island Overlay**. Works on Blender 4.2+ / 5.x. Copying or
symlinking the `uv_island_overlay` folder into `scripts/addons/` remains a
developer option.

## Usage

1. Select a mesh object (Object or Edit Mode).
2. Open the 3D viewport **sidebar** (press `N`) and pick the
   **UV Islands** tab — it has the **UV Island Overlay** checkbox, a
   refresh button, a **Mode** dropdown (*Island Colors* / *Texel
   Density* / *Islands + Density*, the default) and a live status
   readout (island count; in the checker modes also the mesh's median
   density).
3. The same controls also live at the bottom of the viewport header
   **Overlays popover** (the two-overlapping-circles icon), where
   overlay toggles conventionally go.
4. The toggle is also a menu entry — **View > Toggle UV Island Overlay**
   in any mode, and **UV > Toggle UV Island Overlay** in Edit Mode
   (right next to Unwrap) — so F3 menu search finds it; or bind
   `uv.island_overlay_toggle` to a key.

Colors are assigned by golden-ratio hue stepping with stable ordering:
recomputing the same mesh keeps the same colors, and adding islands never
reshuffles existing ones.

## UV Health

Choose the intended texture resolution and press **Analyze UV Health**. The
report distinguishes:

- **Collapsed UV faces** — zero UV area or degenerate 3D area.
- **Duplicate mappings** — distinct triangles using the same three UV
  coordinates. They may be intentionally stacked, but painting cannot
  distinguish them.
- **Low-density islands** — below the selected fraction of the mesh median;
  the default threshold is 50%.
- **Tiny islands** — the shorter UV-bounds dimension is below the selected
  pixel span. This catches thin charts that average density can miss.
- **Outside 0–1** — potentially valid for UDIM or deliberate tiling, so this
  is diagnostic rather than automatically erroneous.

Each row has a **Select** button that enters face Edit Mode and selects the
affected faces. Re-run the analysis after changing the unwrap.

## Island sources: Seams (predicted) vs UVs (actual)

In **Island Colors** and **Islands + Density** modes both panels have
a **Source** dropdown choosing how the island *colors* are defined
(the density mode always uses actual UVs and hides this dropdown; the
combined mode's *checker* always samples actual UVs regardless of the
source — see *Combined mode* below):

- **Seams (predicted)** — the default. Islands are connected face regions
  bounded by seam edges (boundary edges bound; non-manifold edges connect
  all their faces unless seamed). No UV data is read at all, so this
  works on meshes that were never unwrapped and **updates live as you
  mark/clear seams** with any tool. Because Blender's Unwrap splits
  charts exactly at seams, this mode *predicts the post-unwrap islands*:
  it shows the same partition the next Unwrap will produce (the test
  suite asserts SEAM-partition == UV-partition-after-unwrap on several
  meshes). Paint seams, watch
  the islands split, unwrap once you like the layout.
- **UVs (actual)** — true UV-space connectivity of the *current* unwrap:
  two faces are in the same island iff their shared edge's loop UVs
  coincide on both sides. Follows the actual UV data — Smart UV Project
  charts, lightmap packs and hand-split UVs are detected correctly even
  with zero seam flags — but only changes when the UVs change (i.e.
  after unwrapping). This was the only mode before v1.1.0.

The island-count label shows which source produced it: *"6 islands
(predicted)"* vs *"(actual)"*. Switching the source invalidates and
rebuilds immediately.

Default rationale: the primary workflow this overlay serves is
interactive seam marking, where SEAM is the mode that does something
UV mode cannot (live feedback without paying for an unwrap); it is also
~5x cheaper to compute on large meshes. If you unwrap without seams
(Smart UV Project etc.), switch to **UVs (actual)**.

### Refreshing

- Islands are computed when you toggle the overlay on.
- **SEAM source — live updates:** marking or clearing seams (or any other
  geometry edit) recomputes the overlay automatically, ~0.3 s after the
  edit burst goes quiet. The pipeline is deliberately cheap:
  1. the `depsgraph_update_post` hook does O(1) work per tick (records a
     timestamp — safe even during a transform drag);
  2. a debounced `bpy.app.timers` callback waits for 0.3 s of quiet, then
     computes a **checksum** of seam flags + vertex positions + element
     counts (~0.1 s on a 300k-vert mesh; edit-mode state is snapshotted
     with `bm.to_mesh()` into a private datablock because
     `update_from_editmode()` would tag the depsgraph and re-trigger the
     hook — probed on 5.1.2);
  3. only if the checksum actually changed does the rebuild run, on a
     numpy-vectorized path (~0.65 s at 300k verts vs ~3.1 s for the old
     per-face Python loop). No-op events — mode switches, re-marking an
     existing seam, selection changes — never trigger a rebuild.

  Measured on a 302,500-face / 303,601-vert grid (Blender 5.1.2,
  headless): seam+position checksum 0.10 s; vectorized island computation
  0.06 s (pure-Python union-find: 0.57 s); full geometry rebuild 0.64 s
  (bmesh path: 3.08 s). Rebuild cost scales linearly with face count, so
  a burst of seam edits on a 300k-vert mesh costs about one second after
  you pause — no per-edit cost, hence no face-count cutoff or "Live"
  sub-toggle was needed.
- **UV source:** mesh edits and mode switches on the overlaid object are
  picked up automatically (the depsgraph hook marks the overlay dirty;
  the recompute happens once at the next viewport draw — never per
  frame). UV edits made purely in the UV editor may not flag a geometry
  update — hit **Refresh** after re-unwrapping if in doubt.
- The **refresh button** (or `uv.island_overlay_refresh`) is always
  available as the escape hatch in either mode.

### Troubleshooting

Draw-time failures are **never silent**: if anything goes wrong inside
the viewport draw callback, the overlay suspends itself, prints the full
traceback **once** to the system console (`Window > Toggle System
Console` on Windows), and both panels show a *"Draw failed — see system
console"* error row. Hitting **Refresh** clears the error and retries.

## Texel-density checker mode (v1.3.0)

Set **Mode** to **Texel Density** to replace the per-island colors with
a checkerboard mapped through the mesh's **actual UVs**. Because the
checker lives in UV space, its on-surface scale is a direct readout of
texel density: islands that were unwrapped larger or smaller than their
neighbors show bigger or smaller checkers, and a density change *within*
an island (stretching) shows as a checker gradient. Islands in this mode
always come from true UV connectivity (there are no UVs to measure on a
seam prediction), so the **Source** dropdown applies to Island Colors
mode only.

**Units convention.** Texel density is the linear ratio
`sqrt(UV area / 3D area)` — UV units per world unit, so an island whose
UVs are scaled 2x reports exactly 2x the density. The panel multiplies
it by the **Texture Size** property (assumed square texture edge,
default 1024 px) to show the familiar **px/unit** figure. Per island the
ratio is computed over *summed* areas (area-weighted, not a mean of
per-face ratios); the panel shows the **median** across islands.

Properties (in both panels; shown in DENSITY mode — since v1.4.0 the
first three also appear in COMBINED mode, which shares them):

- **Checker Size** — checkers per UV unit (default 32: on a 1024 px
  texture each checker covers 32 px). It is a shader **push constant**
  (probed on 5.1.2: `FLOAT` push constants work with
  `GPUShaderCreateInfo`), so dragging it updates live — no geometry or
  batch rebuild ever happens for this property.
- **Checker Opacity** (v1.3.1) — opacity of the checker, default
  **0.9**: a texel checker should read like near-opaque paint on the
  surface, not a translucent film (the 0.35/0.85 checker grays sit off
  the extremes, so at 0.9 the parity stays obvious in both viewport
  themes while the residual 10% show-through plus the deviation tint
  keep the underlying shading readable). Same push-constant mechanism
  as Checker Size: dragging is live, zero rebuild.
- **Texture Size** — only converts the median readout to px/unit;
  changing it recomputes nothing.
- **Deviation Tint** (default on) — multiplies the checker with a subtle
  per-island tint by **log2 deviation from the median density**: blue
  below the median, neutral at it, red above, saturating at ±2 octaves
  (¼x / 4x). The checker tells you *where and in which direction* the
  density jumps; the tint tells you *how bad* at a glance. The tint is
  baked into the same per-vertex color attribute Island Colors mode
  uses, so toggling it rebuilds once.

**UV requirement.** Density needs a UV layer. Without one the panel
shows *"Mesh has no UVs"* and nothing is drawn — that is a state, not an
error (the *"Draw failed"* row is reserved for real failures).
Degenerate faces are excluded from the statistics: a face with zero UV
area or zero 3D area contributes to neither area sum, and an island with
no valid face at all has *undefined* density — it is skipped by the
median and rendered with the neutral tint (checker as-is, which for
zero-area UVs degenerates to a flat shade).

**Refreshing.** DENSITY mode uses the classic dirty → rebuild-at-next-
draw path (the SEAM live debounce machinery is Island Colors-only).
Probed on 5.1.2: UV edits *do* fire `is_updated_geometry` — direct
`foreach_set` writes in Object Mode, edit-bmesh UV writes, and
`uv.unwrap` alike — so re-unwrapping is picked up automatically; the
**Refresh** button remains the escape hatch.

**Performance** (302,500-face grid, Blender 5.1.2 headless): full
DENSITY rebuild 2.4 s in Object Mode / 4.5 s in Edit Mode. Breakdown:
UV-connectivity island computation 1.75 s (both modes); soup + UV
extraction 0.35 s on the Object-Mode numpy `foreach_get` fast path vs
2.6 s on the Edit-Mode bmesh loop-iteration fallback (the fallback is
*required* in Edit Mode: while the edit bmesh owns the mesh, the Mesh
`uv_layers` data arrays are empty — probed on 5.1.2); area + density
statistics 0.10 s.

## Combined mode (v1.4.0, the default)

Set **Mode** to **Islands + Density** (the default for new sessions)
to see both visualizations at once: the texel-density checkerboard
**multiplied by each island's identity color**. Hue reads island
membership, checker scale reads texel density — one glance answers
"where are my islands *and* is their density consistent".

**How it works (no new shader).** The density fragment stage already
multiplies the per-vertex attribute color by the checker parity;
combined mode simply bakes the ISLANDS-mode per-island palette into
that attribute instead of the deviation tints. Same soup layout
(positions + UVs), same shader, same push constants — the colors are
bit-identical to Island Colors mode for the same mesh and source (the
test suite asserts this).

Design decisions, deliberate and pinned by tests:

- **Deviation Tint is Texel Density-only.** Island hue x checker x
  deviation tint would be unreadable; the combined checker's tint IS
  the island color. The *Deviation Tint* checkbox is hidden outside
  Texel Density mode and toggling the underlying property while in
  combined mode is a no-op (no rebuild).
- **Opacity comes from Checker Opacity** (default 0.9): the combined
  overlay is checker-like paint, so it shares the density mode's
  property and near-opaque default. *Tint Opacity* remains Island
  Colors-only. As always, both are live push-constant uniforms.
- **Needs a UV layer.** The checker has nothing to sample without UVs,
  so — exactly like Texel Density mode — the panel shows *"Mesh has no
  UVs"* and **nothing is drawn**. It deliberately does *not* degrade
  to an islands-only overlay: silently changing what the mode shows
  would be surprising; switch to Island Colors (which falls back to
  seam prediction) if the mesh has no UVs yet.
- **The Source dropdown applies to the island COLORS exactly as in
  Island Colors mode**; the checker always samples the actual UVs.
  With **Seams (predicted)** and *stale* UVs this means the hues are
  predictions of the next unwrap while the checkers still show the
  *old* layout — expected behavior, documented plainly: the hue
  partition and the checker layout are two different readouts and only
  agree after you re-unwrap. Switch the source to **UVs (actual)** to
  make both reflect the current unwrap.
- The panel shows the island count with its *(predicted)* / *(actual)*
  source tag plus the median-density readout (in Seams source the
  median is grouped by the *predicted* islands over the actual UVs —
  coherent with what is drawn).

**Refreshing / invalidation.** Combined+**Seams** participates in the
live seam-marking refresh exactly like Island Colors+Seams: hues
update ~0.3 s after a seam-edit burst goes quiet. Its debounce
checksum additionally covers the **UV layer bytes**, so UV edits and
re-unwraps ride the *same* debounced pipeline — both invalidation
concerns converge on **one** rebuild (the draw path never rebuilds
this mode/source, so a draw-path rebuild can never race a debounced
one; the suite drives both edit kinds with a fake clock and asserts
exactly one rebuild each). Combined+**UVs** uses the classic dirty →
rebuild-at-next-draw path, exactly like Texel Density mode.

**Default change, no migration.** The Mode setting is a
`WindowManager` property: runtime-only, never restored from `.blend`
files — every session starts from the property default, so flipping
the default to combined required no migration for existing users. The
suite probes this empirically (saves a file with mode Island Colors,
changes the session value, reopens the file: the saved value does not
come back).

## Opacity and handedness-aware culling (v1.3.1, updated v1.5.2)

All modes have an **opacity slider** (in both panels): **Tint
Opacity** in Island Colors mode (default 0.4 — the classic translucent
color wash, unchanged) and **Checker Opacity** in Texel Density mode
(default 0.9 — near-opaque paint; since v1.4.0 the combined mode
shares this property, because its overlay is checker-like paint too).
They are deliberately **two separate
properties** rather than one shared slider: the right default differs
by an order of intent (a color wash must let the shading through; a
texel checker should cover it), and separate properties mean switching
modes never drags one mode's setting into the other. Both feed a
`FLOAT overlay_opacity` **push constant** read at draw time (the same
probed-on-5.1.2 mechanism as Checker Size), so dragging either slider
updates live — no geometry or batch rebuild, ever. The fragment stages
take alpha from this uniform and ignore the baked per-vertex alpha.

The overlay uses **handedness-aware face culling** inside the GPU-state
restore guard. A normal closed shell uses `BACK`; an odd negative object
scale or a globally reversed closed mesh swaps the polarity to `FRONT`, and
both reversals together cancel. Open, non-manifold, disconnected, mixed-
winding, degenerate, and singularly transformed meshes use `NONE`, because
one global culling polarity cannot represent them reliably. Previously
culling was off, and because the overlay depth-passes its own surface
by a tiny viewer-ward bias, the *back* faces of open or thin geometry
showed through the front — which made density mode look translucent
even at high opacity, and gave the island tint a confusing
through-shell look on open meshes. The soup's triangle winding follows
the mesh's loop order, which is consistent with the face normals, so on
a normal-consistent mesh exactly the front side of every face is drawn.

The mesh topology and signed-volume orientation are computed only during a
geometry rebuild and cached; clean draw frames only evaluate the object's
3×3 transform determinant. This covers unapplied Scale X = -1 and a closed
shell mirrored with Ctrl+M without adding a per-frame topology walk.

## How it draws (v1.2.0: crack-free depth-biased overlay)

The overlay is a triangle soup whose vertex positions are **bit-identical
to the mesh's own vertex coordinates** — no geometric offset at all — so
adjacent faces share edge vertices exactly and the colored shell is
perfectly connected. Earlier versions pushed each triangle slightly along
its *face* normal to avoid z-fighting; that displaced the two copies of a
shared vertex in different directions wherever faces meet at an angle,
visibly cracking the overlay apart at every non-flat edge.

Z-fighting is instead resolved in a custom shader
(`gpu.types.GPUShaderCreateInfo` + `gpu.shader.create_from_info` — the
legacy raw-GLSL `gpu.types.GPUShader(vert, frag)` constructor cannot be
instantiated on 5.1.2): the vertex stage transforms by the
ModelViewProjectionMatrix and then pulls the result toward the viewer in
*clip space*:

```glsl
gl_Position = ModelViewProjectionMatrix * vec4(pos, 1.0);
gl_Position.z -= 0.0001 * gl_Position.w;
```

Scaling the bias by `w` makes the post-perspective-divide depth offset a
constant fraction (`CLIP_DEPTH_BIAS = 1e-4`) of the NDC depth range at
any distance, so it is robust across zoom levels: a few hundred steps of
a 24-bit depth buffer — comfortably above z-fighting noise, far too small
to make the overlay bleed through foreground geometry around silhouette
edges. Face color still arrives as a plain per-vertex attribute (all
three corners of a triangle carry the same RGBA), and alpha blending,
draw-error latching and GPU-state restoration are unchanged. The GLSL
lives in module-level constants (`overlay.VERT_SHADER_SRC` /
`FRAG_SHADER_SRC`) and compilation stays lazy at draw time, so a shader
error surfaces through the loud *"Draw failed"* row, never silently.

The COMBINED mode (v1.4.0) adds **no shader at all**: it draws the
density soup through the density shader below, with the island
identity palette baked into the per-vertex `color` attribute (the
fragment stage's `finalColor.rgb * shade` is exactly "island color x
checker").

The DENSITY mode (v1.3.0) is a second shader built the same way
(`overlay.DENSITY_VERT_SHADER_SRC` / `DENSITY_FRAG_SHADER_SRC`,
compiled lazily behind the same latch, same depth-bias term): it adds a
per-loop `vec2 uv` vertex attribute and a `float checker_res` push
constant, and the fragment stage derives checker parity from
`floor(uv * checker_res)`, mixing two mid-tone gray shades (0.35 /
0.85 — off the extremes, so they stay distinguishable over both light
and dark viewport themes even at the near-opaque v1.3.1 default
opacity) multiplied by the per-island deviation tint. Since v1.3.1 both
fragment stages take alpha from the `overlay_opacity` push constant
(see *Opacity and back-face culling*). The Island Colors shader's
attributes are otherwise untouched — the test suite pins that
structurally.

## How islands are detected

`islands.py` implements both sources, pure and bpy-free:

- `compute_islands` — **true UV-space connectivity**: two faces sharing a
  mesh edge belong to the same island iff that edge's loop UVs coincide
  on both sides (within epsilon). Follows the *actual unwrap result*
  even when **no edge is flagged as a seam** (the test suite includes a
  smart-projected cube: 6 islands, zero seam flags).
- `compute_islands_by_seam` — **seam-flag connected components** (the
  SEAM source; also the fallback for UV mode on meshes with no UV
  layer). `compute_islands_by_seam_arrays` is its numpy-vectorized twin
  (min-label propagation with pointer jumping over flat `foreach_get`
  arrays) — identical partition and island ordering, ~10x faster at
  300k faces; the test suite asserts equivalence on several meshes
  including non-manifold ones.

## Module layout

- `islands.py` — pure island computation and color assignment (above).
- `density.py` (v1.3.0) — pure texel-density math: triangle areas,
  per-island `sqrt(UV area / 3D area)` densities with degenerate-face
  exclusions, median, and the log2-deviation tint ramp. Like
  `islands.py` it imports neither `bpy` nor `gpu` (the suite pins the
  purity of both), so all of it is testable headless and reusable.
- `live.py` — pure debounce state machine for the SEAM live refresh.
- `overlay.py` — the bpy/gpu glue: geometry extraction, state, shaders,
  draw callback.

## Limitations

- Overlays **one object at a time** (the active mesh when toggled on).
- The tint sits exactly *on* the surface and wins the depth test by a
  tiny viewer-ward bias (see *How it draws*); the bias is far too small
  for the overlay to show through *other* geometry in front of it.
  Closed, consistently wound shells paint only their camera-facing side.
  Ambiguous/open geometry draws two-sided so the preview stays complete.
- UV-editor-only edits may not trigger the auto-refresh hook; use the
  manual **Refresh** button after re-unwrapping if in doubt.
- SEAM source can only see seams: charts split by Smart UV Project or
  manual UV edits (no seam flags) show as one predicted island — switch
  the source to **UVs (actual)** for those. In COMBINED mode with SEAM
  source and stale UVs, the hues are predictions while the checkers
  show the old unwrap (see *Combined mode*).
- While enabled in SEAM mode on an edit-mode mesh, a private snapshot
  mesh datablock (`.uv_island_overlay.snapshot`) holds a copy of the
  mesh for cheap array reads; it is removed when the overlay is
  disabled.
- The overlay draws the mesh *without* modifier results in Object Mode
  (it reads the base mesh, matching what unwrapping operates on).
- In Edit Mode, Blender's own edit-cage overlays (wireframe, seam and
  selection highlights) draw in their own overlay pass and are expected
  to stay readable over the tint. This is a GUI-only behavior — the
  headless suite cannot exercise real drawing — so check it visually
  after installing (see the checklist in the tests section notes).
- Colors are distinct but unlabeled; with hundreds of islands, adjacent
  hues can get close (value is cycled to compensate).
- **DENSITY and COMBINED modes**: need a UV layer (panel hints, draws
  nothing without one — COMBINED deliberately does not fall back to
  islands-only). Hidden faces are excluded from the drawn soup *and*
  from the density statistics, matching what you see. Overlapping UVs
  count their area once per face (overlaps are not detected), and the
  density is measured against the base mesh in object space —
  modifiers and object scale are not applied (matching what unwrapping
  operates on; apply scale for world-true px/unit numbers). The
  deviation tint compares islands *within* the active mesh only —
  cross-object density matching needs the same texture-size convention
  applied manually. Density readouts assume square textures.

## Tests

Headless suite against the real binary (WSL → Windows Blender):

```bash
addons/uv_island_overlay/tests/run_tests.sh
# optionally: run_tests.sh "/path/to/blender.exe"
```

Blender exits 0 even when a `--python` script raises, so each test prints
a sentinel (`ISLANDS_TESTS_PASSED`, `DENSITY_TESTS_PASSED`,
`COMBINED_TESTS_PASSED`, `REGISTER_TESTS_PASSED`) and the
wrapper greps for them, printing `ALL_TESTS_PASSED` / `TESTS_FAILED` and
setting the exit code. GPU shader/batch creation is impossible in
`--background` (`gpu.shader.create_from_info` raises `SystemError` on
5.1.2; building the `GPUShaderCreateInfo` *descriptor* works headless —
probed, for both shaders), so all gpu work is deferred to draw time and
exception-guarded; the register test verifies the draw callback no-ops
gracefully headlessly, checks the GLSL constants structurally (MVP
transform, w-scaled depth-bias term, color passthrough; for the density
shader also the UV attribute, the `floor(uv * checker_res)` checker
parity and the `FLOAT` push constant), asserts the soup positions are
bit-identical to the mesh's vertex coordinates (the crack-free
guarantee), and pins that the Island Colors shader is untouched by the
density mode. `test_density.py` covers the density math on constructed
meshes with known ratios (a 2x-scaled-UV island must report *exactly*
2x density), degenerate-face exclusions, the deviation tint's
sign/clamp/neutral cases, no-UV behavior, and Object-Mode-numpy vs
Edit-Mode-bmesh extraction equivalence; the register test additionally
covers mode-switch invalidation, DENSITY depsgraph routing, and that a
checker-size change never rebuilds. For v1.3.1 it additionally pins the
two opacity properties (0..1 factors, defaults 0.4/0.9), the
`overlay_opacity` push constant in both create-infos and fragment
stages, that an opacity change is uniform-only (no dirty flag, no
rebuild — same assertion style as the checker-size test), and — via the
AST guard audit plus a targeted span check — that the dynamic culling call
sits inside `_gpu_state_restored`, whose finally-clause restores `'NONE'`.
For v1.4.0, `test_combined.py` covers the COMBINED mode end to end:
the enum gained COMBINED and its default flipped (plus an empirical
save/reopen probe that WM property values are never restored from
`.blend` files — no migration needed); no third shader variant exists
(structural checks that the draw path routes COMBINED through the
density shader/batch/uniforms and reads Checker Opacity); COMBINED
geometry equals the DENSITY soup (positions + UVs) while its colors
are bit-identical to the ISLANDS-mode colors for the same mesh and
source (both SEAM and UV); the deviation-tint property is a no-op in
COMBINED; opacity/checker-size changes are uniform-only;
mode-switching between all three modes invalidates and rebuilds; the
no-UV contract (hint, draw nothing, no error latch, both sources);
and — with a fake clock — that COMBINED+SEAM routes both seam edits
AND UV edits through the debounced pipeline (the checksum covers UV
bytes) for **exactly one** rebuild each, while COMBINED+UV takes the
classic dirty → draw path with the live tick refusing to run.
The shaders' actual compile/draw can only be confirmed in the GUI.

### GUI checklist (drawing — not coverable headless)

v1.2.0 (Island Colors mode):

1. Enable the overlay on a curved mesh (e.g. a UV sphere or Suzanne with
   a few seams): the colored faces must be **seamlessly connected** — no
   hairline gaps at edges between faces, at any zoom level.
2. Orbit and zoom in/out: the tint must stay stable — **no z-fighting
   shimmer** against the surface, near or far.
3. Look along a silhouette edge: the overlay must **not bleed** around
   the mesh's outline onto the background or geometry behind it.
4. View an open (single-sided) mesh from both sides: its ambiguous global
   orientation uses two-sided drawing, so the tint remains complete.
5. In Edit Mode, wireframe/seam/selection cage overlays must stay
   readable on top of the tint.
6. If the shader ever failed to compile, both panels would show the
   *"Draw failed — see system console"* row instead of drawing nothing
   silently.

v1.3.0 (Texel Density mode):

7. On an unwrapped mesh, switch **Mode** to **Texel Density**: a
   checkerboard must appear on the surface, **uniform in scale within
   each island** (on an undistorted unwrap) and continuous across faces
   of the same island.
8. Scale one island's UVs up 2x in the UV editor (then hit Refresh if
   it does not auto-update): its checkers must shrink to half size on
   the surface — a visible scale jump against neighboring islands —
   and, with **Deviation Tint** on, the island must warm toward red
   (denser than median) while the others cool toward blue; toggling the
   tint off must return all islands to the plain gray checker.
9. Drag **Checker Size**: the checker must rescale live while dragging
   (it is a uniform — no rebuild hitch), and the panel median readout
   must NOT change (checker size does not affect density).
10. On a mesh with no UV layer, DENSITY mode must draw nothing and show
    *"Mesh has no UVs"* in the panel — no error row.
11. Switch back to **Island Colors**: same colors, same crack-free
    shell as before v1.3.0 (from the front; the back side is culled
    since v1.3.1).
12. Check both a light and a dark viewport theme: the two checker
    shades must stay clearly distinguishable in each.

v1.3.1 (opacity + culling):

13. In **Texel Density** mode at the default Checker Opacity (0.9) the
    checker must read like **near-opaque paint** on the surface — no
    see-through, ghosted look — while the surface shading still reads
    through the residual transparency and the deviation tint.
14. Drag **Checker Opacity** (density mode) and **Tint Opacity**
    (islands mode): both must update **live while dragging** with no
    rebuild hitch (they are push-constant uniforms, like Checker Size),
    from fully invisible at 0 to fully opaque at 1.
15. Set a closed cube's object Scale X to -1: its overlay must remain on
    the same visible shell side. Repeat with Ctrl+M, X in Edit Mode; the
    result must also preview correctly. Applying both mirrors must compose
    back to ordinary BACK culling.
16. Open/non-manifold or locally mixed-winding geometry must remain fully
    visible via the two-sided fallback.
17. Islands mode at the default Tint Opacity (0.4) must look exactly
    like v1.3.0 from the front.

v1.4.0 (Islands + Density):

18. Fresh session, enable the overlay on an unwrapped multi-island
    mesh: the default mode is **Islands + Density** and every island
    must show a checkerboard in its own hue — **hued checkers per
    island**, continuous within each island.
19. The checker scale must still read texel density: an island
    unwrapped 2x larger shows half-size checkers, exactly as in Texel
    Density mode (only the tinting differs).
20. With **Source: Seams (predicted)**, mark a seam across an island
    in Edit Mode: ~0.3 s after you stop, the **hues must re-split
    live** (like Island Colors mode) while the checker layout stays
    put until you re-unwrap; after Unwrap the checkers follow. Then
    scale some UVs in the UV editor: the checkers must update
    automatically too (the combined live checksum covers UVs).
21. On a mesh with no UV layer, combined mode must draw **nothing**
    and show *"Mesh has no UVs"* — no islands-only fallback, no error
    row.
22. Cycle Mode through all three entries: Island Colors shows the
    plain tints (Source + Tint Opacity rows), Texel Density the
    gray/deviation checker (Checker rows + Deviation Tint), Islands +
    Density the hued checker (Source + Checker rows, **no** Deviation
    Tint row) — each switch updates immediately.
23. Drag **Checker Opacity** in combined mode: live, no hitch, and it
    is the same value Texel Density mode uses; **Tint Opacity** must
    have no effect in combined mode.

## Credits

Inspired by island-visualization and texel-density-checker workflows
found in specialized 3D tools such as 3DCoat (e.g. its retopo/UV
rooms). This project is independent and is not affiliated with or
endorsed by Pilgway.
