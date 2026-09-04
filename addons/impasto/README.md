# Impasto

Impasto 0.16.2 is a Blender 5.1 add-on for non-destructive, multi-channel PBR
painting. It stores material work as ordered Paint and Fill layers, compiles
the stack into a Principled BSDF material, and provides a GPU-resident painting
session with immediate material feedback.

Impasto is under active development. The GPU workflow is the primary painting
path. **Blender Brush Replay is an embryonic, fundamentally non-performant
prototype and is not intended for serious work.**

At 4K, ordinary GPU Paint and Erase strokes skip detailed per-dab UV work that
is only needed by Soften and Smear. Stroke logs now split input time, flushing,
UV bounds, seam selection, and undo costs so further performance work can be
driven by measured bottlenecks.
Conservative seam selection uses a cached vectorized owner lookup instead of
rescanning every seam record in Python for each live flush. Undo requests
already known to exceed the atomic history budget are rejected before GPU tile
copies; gradually expanding strokes stop recording once they cross that limit.
Painting is preserved, matching the previous non-undoable outcome.
On the production 4K mesh that motivated this work, seam selection became
approximately 29× faster and total live flush work became 3.65× faster. See
the [performance history](docs/PERFORMANCE_HISTORY.md) for the exact logs,
comparison method, and remaining costs.
Paint and Erase Undo capture is also sparse: distant UV islands contribute
their own 128-pixel tiles without forcing capture of the empty atlas space
between them. Exact gains depend strongly on UV layout and stroke coverage.
Version 0.15.19 stops sparse request work immediately after an Undo transaction
exceeds its memory budget and clips camera-crossing triangles instead of
treating all behind-camera geometry as touched. This directly addresses the
0.15.18 fragmented-atlas regression and extreme zoom slowdown; production
timings still require interactive revalidation.
Version 0.15.20 removes a forced GPU-completion read from the navigation depth
prepass. In the motivating 173,063-triangle session that synchronous pass
averaged 126.7 ms on each changed view, while Lit PBR submission averaged only
0.22 ms. GPU ordering preserves depth correctness without blocking the CPU.
Version 0.15.21 also defers CPU projection/clipping of every triangle until a
stroke actually needs dirty and Undo bounds. A post-0.15.20 trace still spent
111.7 ms per changed view projecting 151,112 triangles; navigation no longer
performs that work. The first paint flush after moving the view reports the
one-time cost as `projection_bounds_ms`.
Version 0.15.24 introduced cached GPU capability probes across sessions in the same
Blender process and reports detailed startup/shutdown phase summaries. Object,
image, and stack GPU resources remain session-local for correctness. Undo
history teardown now drops the owned snapshot graph directly; explicit
GPU-to-Image synchronization remains mandatory and separately timed.

## Current feature set

- Ordered Paint and Fill layers with visibility, opacity, blend mode, and
  per-channel influence.
- One image canvas per painted channel. A persistent stack-wide selector
  creates new Paint layers at 1K, 2K, 4K, or experimental 8K; channels added
  later inherit their layer's resolution.
- The expanded Layer Channels view reports each bound image's actual pixel
  dimensions and warns when channel sizes differ, including imported or
  migrated images.
- Channels for Base Color, Metallic, Roughness, Tangent Normal, Height, Alpha,
  Emission Color/Strength, and Subsurface Weight/Radius/Scale.
- Post-creation channel expansion without replacing existing canvases.
- GPU multi-channel strokes with tablet-pressure control for size and opacity.
- A live stencil sampling assistant compares stencil pixels with the local UV
  paint-texel density under the cursor and warns before fine source detail is
  squeezed into too few destination texels.
- A default-off **Experimental Seam Padding** option under Paint → Advanced
  combines eight-pixel UV-island gutters with topology-aware seam continuation.
  For Paint and Erase, Impasto pairs UV edges through their shared mesh edge
  even when their islands are distant, rotated, or differently scaled. A
  temporary stroke-coverage mask fills only missed peer-edge texels instead of
  overwriting paint that already reached both sides. It participates in
  Undo/Redo and saving and remains experimental pending production validation.
  Literal transport supports color and scalar channels; Tangent Normal is
  excluded until its value can be converted between the islands' tangent bases.
  Exactly duplicated UV triangles disable the feature; partial overlaps and
  extremely subpixel islands remain diagnostic limitations.
- A separate default-on **Conservative UV Seam Paint** mode addresses
  texel-center misses directly. Paint and Erase conservatively extend only
  touched UV seam edges by less than one texel, evaluate the brush at the
  corresponding face edge, protect existing island interiors, and include
  endpoint caps. This removed the previously persistent white, staircase-like
  seam gaps in user validation on a complex 4K production mesh. Tangent Normal,
  Soften, and Smear are excluded. Exterior gutters of islands packed within
  roughly one texel can still collide; disable the mode under Advanced if that
  edge case or a GPU-backend incompatibility is encountered.
  A separate distance-dependent issue remains: seams may reappear abruptly
  when the viewport is zoomed out and vanish one zoom increment closer. This
  is consistent with texture-minification/filter-footprint contamination from
  unpainted atlas gutters, but the cause is not yet confirmed.
- Emission and Subsurface brush-value sections are collapsed by default and
  retain their disclosure state per Paint layer.
- A collapsed **Recent Colors** menu remembers up to eight colors actually
  used in each material stack. Base and Emission histories are separate,
  near-identical colors are consolidated, and the history persists in the
  `.blend`. Expand the menu and click the arrow beside a swatch to reuse it.
- A persistent, collapsible **Material Presets** palette captures and restores
  the complete brush material without changing channel targets. Sphere-like
  color swatches and tooltips summarize Base, Metallic, Roughness, Normal,
  Height, Emission, and Subsurface values.
- Reusable material mask assets with one grayscale image and UV definition can
  be linked to Paint and Fill layers. Every link has independent visibility,
  inversion, opacity, and channel scope. Painting the asset updates all linked
  layers; unlinking retains both the asset and source image.
- A GPU-resident **Soften** brush that blurs all enabled active-layer channel
  canvases together; brush strength, falloff, and optional pressure control the
  effect without synchronizing images back to the CPU.
- A GPU-resident **Smear** brush that transports enabled active-layer channel
  pixels along the stroke. This first version maps screen direction onto
  texture axes; rotated UV islands and seams remain a refinement target.
- Layer-aware GPU erasing that removes active-layer coverage to reveal the
  layers below instead of painting black or neutral channel values.
- Paint, Soften, Smear, and Erase each remember independent per-channel target
  selections and provide **All** and **None** shortcuts.
- GPU-resident per-stroke undo and deferred synchronization to Blender Images.
- Lit PBR and diagnostic live previews.
- Image stencils as a viewport projection or brush-following alpha, with
  view-plane scale and rotate handles on Planar Viewport stencils (`R`
  resets placement).
- A thumbnail-first stencil file browser whose default folder is configured in
  Blender Preferences > Add-ons > Impasto and remembers the last loaded folder.
- Grayscale-stencil normal relief.
- Configurable preview lighting and a preview-only Base Normal Map fallback.
- Kiln baked-normal import/repair.
- A literal-scale SSS Caliper while hovering the mesh.
- Completed GPU strokes can be recorded and replayed by **Stroke Recorder**
  (the `sculpt_stroke_recorder` add-on) while a GPU painting session is
  active. Replay uses the current Impasto brush and the same viewport.

Subsurface IOR and Anisotropy can be registered for material control, but are
not GPU paint-canvas channels.

## Install

Impasto currently targets Blender 5.1 and is experimental. Download
`impasto-0.16.2.zip` from the
[v2026.08.28 release](https://github.com/Teo-Asinari/blender-workflow-lab/releases/tag/v2026.08.28)
and install it with **Edit > Preferences > Add-ons > Install from Disk**,
then enable **Impasto**. Copying `addons/impasto/` into `scripts/addons/`
remains a developer option.

Select a UV-unwrapped mesh with a node-based material, then open
`3D Viewport > N sidebar > Impasto`.

## Recommended workflow

1. Create an Impasto layer stack.
2. Add or select a Paint layer.
3. Expand **Layer Channels** and add the channels that layer should own.
4. Under **Brush Controls**, select **GPU Multi-Channel**.
5. Choose Paint, Soften, Smear, or Erase, then set Brush Radius, Brush Hardness, Brush
   Opacity, pressure behavior, and any channel values used by Paint mode.
   Every GPU brush mode exposes its own compact channel grid. Paint, Soften,
   Smear, and Erase remember independent target selections, with **All** and
   **None** shortcuts.
6. Start GPU Painting.

Enable **Focused Painting UI** above the start button to temporarily hide the
invoking viewport's left toolbar, tool header, and large brush asset shelf.
Impasto restores their previous visibility when the GPU painting session ends;
the sidebar and every other viewport remain untouched.
7. Use LMB to paint. RMB or Esc flushes the resident canvases and exits.

During a session:

- `P` pauses/resumes dab capture so sidebar controls can be edited safely.
- `V` flushes current changes and temporarily shows Blender's authoritative
  material; use it again to resume the Impasto preview.
- Ctrl-Z / Ctrl-Shift-Z operate Impasto's atomic multi-channel stroke history.
- **Flush for Save / Export** synchronizes resident canvases to Blender Images.

Ordinary GPU strokes remain resident at pen-up. Synchronization is explicit or
performed when the session exits; 4K is viable, but uses substantially more
VRAM and makes synchronization slower.

When a session stops, `GPU_PAINT_SPIKE_HOVER` reports aggregated passive-frame
timings for preview, depth prepass, stencil, reticle, caliper, and overlays,
plus mesh triangle and invalid-projection counts. It is intended to distinguish
cursor/viewport cost from actual dab processing without logging every frame.
After 0.15.20, prepass timing measures CPU preparation/submission rather than
forcing a GPU-completion measurement.

Lifecycle diagnostics use three bounded lines:

- `GPU_PAINT_SPIKE_START_PHASES` for mesh/UV/seam preparation.
- `GPU_PAINT_STARTUP` for first-draw GPU initialization.
- `GPU_PAINT_SPIKE_STOP` for teardown after required synchronization.

## Flatten / Prepare glTF

The **Flatten / Export** box creates one new Blender Image for every enabled
stack channel without changing or deleting the source layers. Choose 1K, 2K,
or 4K; generated datablocks are named `Impasto Export <material> <channel>`
and can be packed safely into the `.blend`. Repeating the operation updates
images with matching names and dimensions.

Color channels are tagged sRGB; scalar, normal, height, and vector channels
are Non-Color. Tangent normals remain encoded RGB, height remains in its
stored data representation, and flattened outputs are opaque because the
channel's material default supplies a complete surface below all layers.
Source images are bilinearly resampled to the chosen size. Flush a resident
GPU session first. UDIM images and stacks using multiple UV maps are rejected
rather than producing a misleading result.

With **Prepare glTF Material** enabled, the same command also saves the
flattened channels as PNG files (by default beside the blend in `textures/`),
creates a simple `<source> glTF` Principled material using direct Image Texture
connections recognized by Blender's glTF exporter, and optionally assigns it
to the active object. The editable Impasto material and layer stack remain in
the file unchanged. Reassign the source material to resume painting.

## Painting engines

### GPU Multi-Channel

This is the intended engine. One stroke writes simultaneously to every enabled
paint binding on the selected layer. Base Color, Emission Color, and encoded
tangent normals use RGB values; scalar channels use grayscale values; Height
uses signed additive deposition.

The active Blender Draw brush contributes the basic stamp size, spacing,
strength, and supported pressure behavior. Impasto does not yet reproduce the
full behavior of every Blender brush asset. Clone, Smear, Soften, Fill,
Gradient, Mask, arbitrary brush textures, and custom falloff parity remain
incomplete.

### Blender Brush Replay (Prototype)

This path records a stroke and replays Blender image painting separately into
each target canvas after pen-up. It is slow, visually delayed, and cannot
provide the resident multi-channel behavior of the GPU engine. It remains only
as a compatibility experiment and should not be treated as a production
painting workflow.

Single-channel native image painting is still available from individual
channel rows when direct editing of one canvas is useful.

## Live preview

The resident preview offers:

- **Lit PBR** for approximate material evaluation.
- **Raw Tangent Normal** for encoded normal-map inspection.
- **Neutral Normal Lighting** for isolating Normal and Height response.
- **Height Grayscale** for inspecting the height canvas.

Lit PBR uses Impasto's own configurable studio lighting. It is not Blender
Material Preview. Same-UV visible layers remain composed around intermediate
active layers; affine Fill values and one upper Paint image per non-normal
channel update live after the active canvas. Brushes still write only to the
active layer. Mixed UV layouts, masks, nonlinear blends, upper normals, and
more complex upper-image sequences retain authoritative Blender inspection.

The preview-only **Base Normal Map** picker can display an existing tangent
normal image while painting. This manual fallback has been user-validated and
works well. It does not alter the node graph, stack, render, export, or source
image. Kiln normal data can also be imported or repaired as a baseline layer.
Multiple normal layers are composed bottom-up with Reoriented Normal Mapping
(RNM), so upper detail augments the normals below instead of replacing them.
The same RNM semantics are used by the material, Lit PBR preview, and flattened
Normal export; layer alpha, opacity, and masks attenuate detail toward neutral.
**Rebuild** automatically discovers a loose material-level `Kiln Bake Target`
image node and imports or refreshes it as the bottom **Kiln Baked Normal**
layer. The top-level material continues to show one Impasto group connection;
the RNM graph is generated inside that group.

## Image stencils

The stencil image selector includes a cached thumbnail of the selected image.
The Preview Lighting popover includes Blender's spherical preview for the
active material; because resident strokes deliberately avoid routine
readback, that sphere represents the last synchronized material. Use Inspect
Material or finish the painting session to synchronize it.

An Image Stencil has three independent choices:

- **Placement:** fixed Viewport Stencil or brush-following footprint.
- **Image interpretation:** Alpha Channel or Grayscale.
- **Stencil Effects:** Paint Coverage and Normal Relief are independent toggles
  and can be enabled together. Coverage masks every enabled painted channel;
  relief derives tangent-normal detail from the same image.

Normal Relief derives tangent-space normal direction from grayscale gradients;
it does not interpret grayscale directly as normal-map RGB. See
[STENCIL_WORKFLOW.md](docs/STENCIL_WORKFLOW.md) for the detailed transform and
sampling contract. **Alpha Channel** only produces relief when the image has
varying transparency. For an opaque grayscale height image, select
**Grayscale** so relief is derived from its visible brightness. Normal Relief
can be used in the same stroke as Base Color, Metallic, Roughness, and other
enabled material channels: the derived gradient supplies Normal while the
stencil intensity remains the shared paint-coverage mask for those channels.

## Emission and subsurface painting

Emission Color and Emission Strength are independent channels. Strength is an
unclipped HDR scalar.

Principled subsurface color comes from Base Color. The paintable SSS controls
are:

- **Weight:** how much subsurface scattering contributes.
- **Scale:** the overall scene-space travel distance.
- **Radius RGB:** relative red, green, and blue travel distances.

The optional **Show SSS Caliper** overlay is visible whenever the toggle is on
and the cursor is over the mesh. GPU painting is not required. Its colored
rings show the literal projected distances `Scale × Radius R/G/B`; the white
circle is the screen-sized GPU-paint brush radius and appears only during a
GPU paint session. There is no visual magnification. Extremely small distances
produce a warning relative to the mesh bounding-box diagonal.

## Storage and material ownership

Each Paint binding owns a dedicated Blender Image at the layer's resolution.
Display-color channels use sRGB storage; scalar, Height, and normal data use
Non-Color storage. Older single-canvas layer data migrates to the current
per-binding schema without replacing its images.

Impasto owns its generated root and per-layer node groups. Treat those node
graphs as build artifacts and edit the material stack through Impasto.
Removing a stack restores displaced pre-existing Principled links where they
were recorded.

## Important limitations

- The live preview is an Impasto approximation, not Blender Material Preview.
- GPU painting currently requires UV-mapped image canvases.
- Generated materials, flattened export, and the resident baseline support
  bottom-up RNM normal composition. Exact dynamic resident preview of arbitrary
  upper normal sequences remains limited.
- Resident GPU preview supports one visible same-UV image mask per affine
  upper layer. Multiple, independently mapped, lower, or active masks enter
  authoritative Blender material inspection.
- The SSS Caliper's white brush ring is GPU-paint-only; colored Scale×Radius
  rings remain available outside a GPU session whenever the toggle is on.
- GPU canvases consume real VRAM. One 4K RGBA16F channel is approximately
  128 MB before preview, depth, and undo resources.
- Resident painting also keeps one full-size RGBA16F scratch texture regardless
  of channel count. The minimum active-canvas allocation is therefore roughly
  `(channels + 1) × 128 MiB` at 4K and `(channels + 1) × 512 MiB` at 8K,
  before baseline textures, viewport depth, Blender's own image textures, and
  up to 256 MiB of tile undo snapshots.
- 8K creation is exposed as an experimental option. Soften and Smear use
  conservative dirty-region copies, but 8K remains unqualified and may have
  prohibitive VRAM, synchronization, undo, or teardown costs.

See [High-resolution painting estimates](docs/HIGH_RESOLUTION_PERFORMANCE.md)
for formulas, VRAM/RAM tables, expected responsiveness, and qualification
requirements. See [GPU painting performance history](docs/PERFORMANCE_HISTORY.md)
for measured optimization results.

## Tests and development notes

Runtime code is separated by responsibility: focused GPU helpers live under
`gpu/`, paint-panel rendering lives in `ui_paint.py`, channel menus in
`ui_channels.py`, and reusable operator mechanics in `operator_support.py`.
Historical design notes live under `docs/`. The original `gpu_engine`, `ops`,
and `ui` module paths remain compatibility facades while further safe
decomposition continues.

Run the Blender regression suite with:

```bash
addons/impasto/tests/run_tests.sh
```

The runner checks explicit success sentinels because Blender may exit with a
zero status after a Python exception. See the
[documentation index](docs/README.md), [roadmap](ROADMAP.md), and
[changelog](CHANGELOG.md). Architectural background is in
[`../../research/layer-stack-design.md`](../../research/layer-stack-design.md).

## Support

Report issues at
[github.com/Teo-Asinari/blender-workflow-lab/issues](https://github.com/Teo-Asinari/blender-workflow-lab/issues).

## License

GPL-3.0-or-later. Brush icons in `assets/icons/` are CC0.
