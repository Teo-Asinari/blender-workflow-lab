# Impasto roadmap

## UV seam-safe paint padding

Development branch: `feature/impasto-uv-gutter-padding`. Experimental seam
padding and topology-aware seam continuation are implemented behind a
default-off Advanced toggle and await
interactive validation on production 4K meshes before merging to `master`.

- The pure ownership foundation lives in `gpu/uv_gutters.py`: triangles are
  grouped by mesh-edge and UV continuity, never by paint alpha.
- The resident compact GPU path propagates exact deterministic local source
  offsets through an `RG16F` map using eight one-pixel relaxation passes.
  UV interiors are rasterized into the immutable map once per UV map and
  resolution. The retained map costs 16 MiB at 2K, 64 MiB at 4K, or 256 MiB
  at 8K; construction temporarily doubles that GPU allocation and uses a
  float32 CPU seed allocation twice the retained-map size.
- UV/resolution cache keys, direct GPU seed-raster shaders, and warnings for
  out-of-range, very small, and
  exactly duplicated UV triangles. These diagnostics do not yet detect every
  partial overlap, and the small-triangle warning is heuristic.
- Implemented: pen-up processes only per-channel dirty rectangles expanded by
  the padding radius and copies complete resident texels through the existing
  scratch texture. UV interiors are preserved; expanded Undo/Redo and
  flush/save bounds include the gutters.
- Implemented experimentally: manifold UV seams are paired by their shared
  mesh edge rather than atlas proximity. Paint/Erase record one shared `R16F`
  coverage mask and copy only source-touched, destination-missed samples
  through bidirectional two-pixel seam strips. The mask costs 8 MiB at 2K or
  32 MiB at 4K and exists only for sessions with eligible seams. Tangent Normal,
  Soften, and Smear are not yet transported across this path.

Remaining: stress-test hand-authored and Smart UV atlases at 2K/4K, measure
pen-up latency, and keep 8K experimental because the retained compact map alone
costs 256 MiB. Partial UV overlaps and islands too small to cover a texel center
remain limitations requiring diagnostics rather than silent claims of repair.

Conservative boundary rasterization is available behind its own default-on
toggle. It processes only seam faces intersecting the current stroke,
adds endpoint caps, protects rasterized island interiors, and supports Paint and
Erase for literal color/scalar channels. **User-validated:** it removed the
white staircase-like UV seam gaps on the complex 4K production mesh that
reproduced the original failure. Remaining qualification is exterior-gutter
collision ownership for islands packed within about one texel, broader GPU
coverage, and eventual Tangent Normal/Soften/Smear support.

Observed after close-range seam validation: white seams can reappear abruptly
past a viewport zoom-out threshold and disappear after one zoom-in increment.
Treat this as a separate minification/filtering investigation, not a regression
of the corrected boundary rasterization. Determine whether Lit PBR sampling is
selecting or approximating a coarser footprint that blends painted boundary
texels with unpainted atlas gutters; qualify explicit gutter ownership and
filter behavior at multiple zoom levels before declaring distant-view seams
resolved.

This is the authoritative list of open work for Impasto 0.15.40. Shipped work
belongs in [CHANGELOG.md](CHANGELOG.md), not here.

## Near-term

- Fix orphaned GPU-paint sessions when the owning Paint layer or stack is
  deleted. Confirmed failure: the modal timer calls
  `_refresh_stroke_settings()`, raises `PaintTargetError("The active paint
  layer disappeared")`, and exits without `_finish()`, leaving resident GPU
  resources/status overlays alive while pointer events return to Blender.
  Until fixed, recover from Blender's Python Console with
  `from impasto import gpu_engine; gpu_engine.stop_session()`. The final fix
  must catch missing-target failures on every modal refresh path, always
  remove timers/draw handlers/resources, and either prevent deletion during a
  resident session or stop/flush it explicitly before deletion. Add regression
  coverage for layer deletion, whole-stack removal, and left-handed input.
- Improve roughness readability beyond the current supplemental studio light.
  Add an optional, clearly identified diagnostic view or stronger
  preview-only contrast control while keeping the neutral preview unchanged
  and never modifying painted roughness data.
- Interactively benchmark Paint, Erase, Soften, and Smear at 4K with 1, 4, and
  8 channels. Treat 8K as experimental until latency, synchronization, undo,
  and memory behavior have been measured. See
  [high-resolution estimates](docs/HIGH_RESOLUTION_PERFORMANCE.md).
  Version 0.15.16 removed the unused per-dab triangle scan from Paint/Erase
  and added timings for flushing, UV bounds, seam selection, and undo. Version
  0.15.17 vectorizes conservative seam selection and rejects impossible atomic
  undo records before GPU copying. Version 0.15.18 captures Paint/Erase Undo
  from sparse hit-island tiles rather than their atlas-wide union. Re-measure
  production strokes to identify the next dominant 4K cost.
  Version 0.15.19 stops all sparse-rect/tile work once the 256 MiB transaction
  is abandoned and replaces the behind-camera always-dirty projection fallback
  with conservative homogeneous clipping. Re-test the fragmented Smart UV and
  zoomed hand-unwrapped cases. If either remains costly, qualify an adaptive
  sparse/broad threshold using island fragmentation and unique request counts.
  Version 0.15.20 also removes the synchronous navigation-prepass read that
  measured 126.7 ms per changed view on the 173,063-triangle production mesh.
  Version 0.15.21 defers the remaining 100–110 ms CPU triangle projection until
  painting resumes. Re-measure orbit/zoom using the session-end hover summary
  and inspect `projection_bounds_ms` on the first subsequent stroke.
  Version 0.15.22 caches process-stable capability probes and divides startup
  and shutdown into named phases. Use production logs to decide whether paint
  texture upload/seeding, shader creation, stack baselines, or GPU reference
  release is the next viable target. Do not hide required Image sync latency
  inside teardown measurements.
- Continue performance hardening in this order. Version 0.15.24 completed the
  shared sparse Undo tile-enumeration item; validate its production effect with
  `undo_touch_ms`, which also includes GPU snapshot capture.
  1. Replace repeated linear gutter/seam-rectangle membership checks with an
     exact indexed or hashed lookup, preserving deterministic ownership and
     coverage.
  2. Cache unchanged brush channel keys and target plans between settings
     refreshes. This is safe but expected to be a smaller gain.
  3. Investigate dirty-region-only GPU-to-Image synchronization at explicit
     flush/exit. Full 4K multichannel readback remains a material delay, but
     partial synchronization requires careful Blender Image, save, Undo, and
     color-management qualification.
  4. Revisit screen-space triangle indexing only with a vectorized/native build
     or a safely persistent cache. The rejected Python uniform-grid prototype
     accelerated synthetic queries by roughly 15x but took about 2.9 seconds
     to construct for 173K triangles, causing an unacceptable first-stroke
     regression.

## Workflow and UX

- Develop the shared keyboard-first interaction layer described in
  [`../../docs/KEYBOARD_FIRST_UX.md`](../../docs/KEYBOARD_FIRST_UX.md). In
  particular, make Impasto GPU recording independent of Blender Texture Paint
  mode and its redundant brush toolbar, and move frequent Impasto operations
  out of the sidebar into mnemonic commands with clear HUD feedback.
- Shipped in 0.15.26: SSS Caliper colored rings are available outside an
  active GPU painting session. The white brush ring remains GPU-paint-only.
- Shipped in 0.15.27: Planar Viewport stencil scale and rotate handles.
  Translate/Position handles remain numeric.
- Improve Smear across rotated UV islands and seams.

## Architecture and compatibility

- Add Substance material interoperability in two stages:
  1. Implement a Substance texture-set importer for exported PNG, TIFF, and
     EXR maps. Recognize common Painter/Sampler filename conventions, map
     Base Color, Roughness, Metallic, Normal, Height, Emission, AO, and other
     compatible outputs onto Impasto channels, assign correct color spaces,
     and create a Fill or pass-through layer without requiring Adobe software.
  2. Add an optional `.sbsar` bridge that discovers a user-installed,
     separately licensed Substance Engine/SDK or command-line renderer,
     exposes graph presets and parameters, renders outputs into a managed
     cache, and feeds them through the same texture-set importer. Do not bundle
     Adobe's runtime without an explicit redistribution-license review.
  Native evaluation of editable `.sbs` graphs is out of initial scope: the XML
  is readable, but reproducing Designer's complete and versioned node runtime
  would be a separate material-engine project.
- Investigate format-optimized resident paint targets: keep color and normal
  channels in `RGBA16F`, but store scalar channels such as Metallic,
  Roughness, Subsurface Weight, and Emission Strength in `R16F`. For seven
  representative channels this would reduce one resident copy from about
  896 MiB to 512 MiB at 4K, or 224 MiB to 128 MiB at 2K. Because the current
  OpenGL probe supports both formats separately but not mixed-format MRT,
  implementation requires separate RGB/scalar draw batches plus readback,
  preview, compositing, undo, and backend qualification. Treat this as later
  performance work, after UV seam-safe padding correctness.
- Expand live upper-layer post-composition beyond arbitrary ordered affine
  non-normal layers, one same-UV mask per upper layer, and named-UV
  reprojection for unmasked upper Paint layers. Remaining boundaries are
  multiple or independently mapped masks, nonlinear blends, exact dynamic
  upper RNM normals, and mixed-UV lower/static-only channels.
- Continue decomposing `gpu_engine.py` and `ops.py` compatibility facades into
  focused, regression-guarded modules.
- Continue qualification across supported GPU backends and drivers.

## Explicitly not open

- Brush mode, channel targets, brush parameters, and stencil settings now
  refresh live between strokes in a resident GPU painting session.
- Embedding Eevee inside the resident GPU painting overlay is not planned.
  Instead, improve Lit PBR parity with Blender and provide diagnostic channel
  views; Eevee remains the authoritative post-flush material preview.
- Flattening the stack to combined per-channel images is implemented.
- Paint, Soften, Smear, and Erase already have independent per-channel target
  toggles with All/None shortcuts.
- The preview-only Base Normal Map picker is implemented and has been
  user-validated as a useful, reliable manual fallback. Automatic Kiln
  discovery and true layered-normal composition remain open.
- Stencil Paint Coverage and Normal Relief can be enabled together.
- Kiln and Impasto normal layers use bottom-up RNM composition in the
  generated material, resident preview, and flattened Normal export.
- Rebuild automatically imports or refreshes a loose material-level
  `Kiln Bake Target` as the bottom normal layer.
- Paintable layer masks and persistent brush-material presets are implemented.
- The stencil browser has a persistent add-on-level default directory and
  opens in thumbnail view.
