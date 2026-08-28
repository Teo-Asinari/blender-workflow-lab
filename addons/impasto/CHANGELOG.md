# Impasto changelog

This file records shipped user-visible changes. Detailed historical engineering
notes remain available in
[docs/archive/PROGRESS_LEGACY.md](docs/archive/PROGRESS_LEGACY.md).

## 0.15.31

- Draw scale handles as sharp, single-color squares that rotate with the
  stencil. Drop the beveled two-tone rims on both scale and rotate knobs.

## 0.15.30

- Recolor Planar Viewport stencil handles to a dark strong green for scale
  and a dark strong cyan for rotate.

## 0.15.29

- Double Planar Viewport stencil handle size and draw the knobs with
  anti-aliased signed-distance fills instead of aliased polygons.
- Recolor scale handles dusty rose and the rotate knob olive, replacing the
  previous amber/cyan pairing. Function arrows are thick filled strokes.
- Hit targets grow with the knobs.

## 0.15.28

- Enlarge Planar Viewport stencil handles and draw filled amber scale boxes
  versus a filled cyan rotate knob, each with a dark outline.
- Add semi-transparent grey function hints: double arrows on scale handles
  and circular arrows around the rotate knob.
- Press `R` during GPU painting to reset Planar Viewport position, scale,
  and rotation to defaults.

## 0.15.27

- Add viewport-plane scale and rotate handles for Planar Viewport image
  stencils during GPU painting. Corner and edge boxes change Viewport Scale;
  the top knob rotates. Shift preserves aspect. Numeric fields stay in sync.
  Esc or right-click during a handle drag restores the start transform.
  Brush Footprint stencils are unchanged.

## 0.15.26

- Show the SSS Caliper colored Scale×Radius rings whenever Show SSS Caliper
  is enabled and the cursor is over the mesh, not only during GPU painting.
- Keep the white screen-space brush ring GPU-paint-only, and skip the idle
  overlay while a GPU paint session is already drawing the caliper.

## 0.15.25

- Extend Flatten / Export into a one-command glTF preparation workflow: save
  flattened channel PNGs, build a separate exporter-recognizable Principled
  material, and optionally assign it without modifying the editable stack.
- Force flattened pixel buffers to contiguous float32 at Blender's image API
  boundary, fixing real stacks whose NumPy composition promoted to float64.
- Do not export the default-white emission color when flattened emission
  strength is zero; this previously made otherwise non-emissive assets glow
  white in glTF viewers and game engines.
- Validate direct base-color, scalar, tangent-normal, and zero-emission material
  wiring with focused regression coverage and an end-to-end production asset.

## 0.15.24

- Enumerate sparse Undo tile geometry once for all painted channels instead of
  rebuilding and deduplicating fragmented-UV tile lists per channel.
- Preflight each unique tile once while retaining atomic multichannel budget
  rejection and deterministic capture order.
- A synthetic five-channel fragmented 4K workload reduced Undo tile
  bookkeeping from about 868 ms to 70 ms (12.5x). GPU snapshot time is separate,
  so total interactive improvement depends on how many new tiles are captured.

## 0.15.23

- Compute the exact screen-space triangle hit set once per GPU flush and reuse
  it for dirty UV bounds, sparse Undo coverage, and seam transport. This avoids
  repeating the same full-mesh intersection work for each subsystem while
  preserving inclusive edge hits and deterministic seam-record order.
- Reuse the paint UV soup for the common Base Normal case where it uses the
  active UV map. Distinct named Base Normal UV maps remain independently
  extracted.
- Add `screen_exact_hits` to stroke telemetry for production qualification.

## 0.15.22

- Cache backend capability-probe results for the current Blender process,
  keyed by a versioned backend/vendor/renderer identity. Repeated sessions no
  longer rerun the complete readback/framebuffer probe suite.
- Add bounded startup summaries for mesh preparation and first-draw GPU phases:
  probes, shaders/UBOs, IBL, gutters, paint textures, batches, stack baselines,
  and remaining setup.
- Add bounded shutdown timing for handlers, hover logging, Undo-history disposal,
  GPU reference release, modal timer removal, redraw, and operator completion.
- Drop GPU Undo snapshot ownership directly during teardown rather than walking
  a no-op release callback for every tile. Required Image synchronization is
  unchanged.

## 0.15.21

- Defer per-triangle screen projection and near-plane clipping until the first
  paint flush after navigation. Orbit/zoom still updates the GPU depth texture,
  but no longer projects 150K+ CPU triangle bounds on every changed view.
- Report the deferred one-time work as `projection_bounds_ms` in stroke
  telemetry.

## 0.15.20

- Remove the synchronous one-pixel framebuffer read from every navigation
  depth prepass. GPU command ordering already guarantees that later consumers
  observe the completed pass, so viewport orbit/zoom no longer forces Blender's
  CPU to wait for the entire mesh-depth raster on each changed view.
- Guard the navigation path against future accidental depth readback with a
  focused regression assertion.

## 0.15.19

- Stop sparse UV-rectangle generation and tile enumeration immediately after
  a stroke exceeds the atomic Undo budget. Painting, dirty synchronization,
  conservative seams, and gutters continue normally.
- Clip camera-crossing triangles in homogeneous screen space. Fully hidden or
  offscreen triangles no longer enter every dab's dirty/Undo selection merely
  because one vertex crossed the camera plane; invalid non-finite geometry
  retains the conservative fallback.
- Emit one aggregated `GPU_PAINT_SPIKE_HOVER` line when a resident session
  stops. It separates passive preview, depth-prepass, stencil, reticle,
  caliper, text-overlay, and total callback timing and reports triangle and
  unprojectable counts without per-frame log spam.

## 0.15.18

- Capture Paint/Erase Undo tiles from each screen-hit triangle's UV region
  rather than the bounding rectangle spanning every touched island. Final
  Image synchronization retains its conservative broad bound.
- Deduplicate overlapping sparse tile requests before atomic memory-budget
  preflight and GPU capture.
- Keep experimental gutter destinations as separate regions through pen-up,
  ensuring the gutter pass cannot modify atlas-gap tiles omitted by sparse
  Undo.
- Known performance regression: highly fragmented UV atlases can spend
  substantial CPU time rebuilding sparse tile requests after a stroke has
  already exceeded the atomic Undo budget. Painting remains correct; an early
  exit and adaptive sparse/broad policy are tracked in the roadmap.

## 0.15.17

- Replace repeated Python scans of every conservative seam record with an
  exact, cached NumPy owner lookup. Record order and inclusive intersection
  behavior remain unchanged.
- Preflight multichannel GPU undo storage as one atomic request. If a stroke
  cannot fit the configured history budget, skip its otherwise-pointless tile
  copies while preserving the painted, non-undoable result produced before.

## 0.15.16

- Remove unused per-dab triangle/UV work from ordinary GPU Paint and Erase.
  Soften and Smear retain the detailed rectangles required for neighborhood
  sampling.
- Add bounded stroke profiling for input duration, flush time, UV bounds,
  seam selection, and GPU undo capture/commit. These measurements identify
  the next bottleneck without changing painting output.

## 0.15.15

- Add a separate default-off Conservative UV Seam Paint experiment for Paint
  and Erase. It extends only touched seam boundaries by less than one texel,
  evaluates brush coverage at the corresponding mesh edge, includes endpoint
  caps, and protects rasterized UV-island interiors.
- Limit conservative seam batches, undo capture, and dirty synchronization to
  seam faces intersecting the current stroke. Hard-disable the ineffective
  0.15.14 texel-center transport path.
- Known limitation: exterior gutter strips still lack ownership where islands
  are packed within roughly one texel. Tangent Normal, Soften, and Smear are
  not included in this experiment.
- User validation on a complex 4K production mesh confirmed that conservative
  boundary painting removes the persistent white, staircase-like gaps along UV
  island seams that the earlier padding and cross-island transport attempts did
  not resolve.

## 0.15.12

- Add default-off Experimental Seam Padding for resident GPU painting. After
  each stroke, exact UV-edge ownership extends complete channel texels eight
  pixels into island gutters, reducing white/filtering seams without changing
  UV-interior pixels.
- Apply seam padding only to targeted channel dirty regions, include expanded
  pixels in Undo/Redo and flush/save synchronization, and reuse the existing
  scratch texture rather than retaining another per-channel 4K canvas.

## 0.15.11

- Refresh brush mode, channel targets, brush parameters, pressure/stamp state,
  and stencil state between strokes without restarting GPU painting.
- Make stencil visibility and placement overlays follow live stencil changes
  during a resident painting session.

## 0.15.10

- Add a neutral-by-default, preview-only Roughness Readability light control
  for distinguishing low and medium roughness without changing painted data.

## 0.15.9

- Simplified canvas sizing to one persistent stack-wide selector so every
  channel created within a Paint layer remains GPU-session compatible.
- Removed per-channel resolution overrides.

## 0.15.8

- Added persistent stack-level canvas resolution selection for newly created
  Paint images at 1K, 2K, 4K, or experimental 8K.
- Added optional per-channel resolution overrides and made their
  non-destructive, new-images-only behavior explicit in the main panel.

## 0.15.7

- Added exact upper-layer mask composition for one visible same-UV image mask
  per affine non-normal layer, including opacity, inversion, and per-channel
  participation.
- Added exact named-UV reprojection for arbitrary ordered unmasked affine
  upper Paint layers while keeping active-layer-only writes.
- Kept nonlinear, independently mapped/multiple-mask, lower mixed-UV, and
  exact dynamic upper-RNM cases on authoritative Material Preview fallback.

## 0.15.6

- Replaced the two-upper-Base special case with arbitrary-depth ordered upper
  composition for compatible non-normal layers.
- Precompose every supported upper sequence into one GPU affine-transform
  texture per channel, keeping sampler use and live-preview draw cost fixed
  as layer count grows.

## 0.15.5

- Kept Lit PBR resident for an active material layer beneath ordered
  `Base + Emission` and `Base + Metallic + Roughness` Paint layers.
- Added a second ordered upper Base Color image path while preserving
  active-layer-only brush writes and the other sparse upper channels.

## 0.15.4

- Kept every visible active-layer channel in Lit PBR even when its brush
  target is disabled; brush targeting continues to control writes only.
- Made the complete preview mesh write depth during its draw so front
  triangles reject nearly coincident rear geometry.

## 0.15.3

- Fixed a misplaced initialization that suspended the Lit PBR draw callback
  when starting GPU painting after the preview UBO migration.

## 0.15.2

- Migrated every Lit PBR preview parameter from oversized push constants to
  one std140 uniform buffer for portable GPU-backend behavior.
- Kept live preview updates allocation-free and added layout, lifecycle, and
  real-GPU regression coverage.

## 0.15.1

- Added a persistent add-on preference for the default stencil directory and
  remember the folder of each successfully loaded stencil.
- Replaced the generic image opener with an image-filtered stencil browser that
  opens in thumbnail view and assigns the selected image directly.

## 0.15.0

- Added production layer-mask controls: add/remove/select, native grayscale
  painting, visibility, inversion, opacity, and per-channel participation.
- Mask canvases match their layer resolution, feed generated materials and
  flattened exports, and remain available as Blender Images after removal.
- Added persistent brush-material presets with capture/apply/remove controls,
  spherical color swatches, and full channel-value tooltips.
- Applying a preset changes material values without changing brush channel
  targets or active-layer ownership.

## 0.14.6

- Added live same-UV post-composition for affine upper Fill layers and one
  upper Paint image per active non-normal channel.
- Intermediate GPU painting now stays in Lit PBR and displays resident strokes
  immediately while preserving active-layer-only writes.
- Kept complex upper sequences, masks, nonlinear blends, and upper normals on
  the explicit authoritative-inspection fallback.

## 0.14.5

- Fixed intermediate sparse layers—such as emission-only Paint layers—so
  unrelated visible channels above and below remain composed in Lit PBR.
- Kept brush ownership isolated to channels on the active layer.
- Unsupported mixed-UV, masked, or same-channel upper compositions now retain
  authoritative Blender material inspection instead of showing active-only
  preview data as though it were the complete stack.

## 0.14.4

- Soften and Smear now copy only conservative, padded per-dab UV regions and
  render them back through a GPU scissor instead of copying whole textures.
- Reused one persistent scratch framebuffer and removed per-dab texture swaps
  and framebuffer reconstruction.
- Added exact 4K memory/copy estimates, a stable 1/4/8-channel benchmark
  matrix, and brush mode/target count in stroke telemetry.

## 0.14.3

- Removed the redundant Lit PBR depth-texture comparison that could reject thin
  surface strips and expose Blender's underlying material.
- Lit PBR now relies on Blender's framebuffer depth, a small clip-depth bias,
  smooth corner normals, and back-face culling for continuous, occluded preview.
- Verified the revised Lit PBR depth handling on a production mesh.

## 0.14.2

- Removed the obsolete `active_normal_blend` preview uniform after RNM made
  normal-layer color blend modes irrelevant, restoring GPU-paint startup.

## 0.14.1

- Rebuild now discovers a loose material-level `Kiln Bake Target` image and
  imports or refreshes it as the bottom RNM normal layer.

## 0.14.0

- Added bottom-up RNM composition for Kiln and Impasto tangent-normal layers.
- Kept generated Blender nodes, resident Lit PBR preview, and flattened Normal
  exports on the same alpha/mask-aware normal-composition semantics.
- Existing stacks upgrade in place through Rebuild Stack without replacing
  layers or painted images.

## 0.13.4

- Paint, Soften, Smear, and Erase independently remember their selected layer
  channels.
- Every brush mode provides All and None target shortcuts.
- Resident painting and stroke undo affect only the selected channels.

## 0.13.3

- Fixed GPU painting startup after Blender optimized the unused
  `resolved_stack` shader uniform away.

## 0.13.2

- Added All and None shortcuts to the Erase channel grid.

## 0.13.1

- Made the top-layer Lit PBR overlay continuous across the visible surface.
- Collapsed Emission and Subsurface brush-value sections by default.

## 0.13.0

- Added layer-aware targeted erasing, GPU Smear, and non-destructive
  Flatten/Export to combined per-channel Blender Images.
- Hardened preview startup, state restoration, and fallback behavior.

## 0.12

- Made stencil Paint Coverage and Normal Relief independent, allowing both in
  one stroke.
- Added persistent recent-color swatches and custom brush-mode icons.

## 0.11

- Added GPU Soften and Erase, combined stencil material/normal painting,
  per-channel image dimension readouts, and clearer brush-mode controls.

## 0.10

- Made grayscale stencil Normal Relief resolution-independent and split major
  UI, operator, and GPU responsibilities into focused modules.

## 0.9

- Added Emission and Subsurface painting, categorized stencil controls,
  configurable preview lighting, pressure opacity, Base Normal Map preview,
  Kiln-normal integration, improved occlusion, and the SSS Caliper.

## 0.7 and earlier

- Established GPU-resident multi-channel painting, atomic GPU undo, deferred
  image synchronization, diagnostic previews, PBR lighting, per-channel
  canvases, and the non-destructive Principled layer stack.
