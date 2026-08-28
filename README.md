# Blender Workflow Lab

Practical tools and technical experiments for better Blender sculpting,
texture painting, UV, baking, and UX workflows. Ideas are shipped as add-ons
where Python can carry the feature and researched toward upstream
contributions where it cannot.

> This is an independent project. It is not affiliated with, endorsed by, or derived from 3DCoat (Pilgway), Substance Painter (Adobe), or any other product mentioned; product names are used only to describe comparable workflows. All code here is original, written against Blender's public APIs.

## Add-ons

All add-ons are tested against **Blender 5.1.2** and ship with test suites that
run against a real Blender binary. See each add-on's README for usage,
limitations, and interactive acceptance checks.

## Install

**Install from Disk** is the supported user path:

1. Download a versioned ZIP from
   [GitHub Releases](https://github.com/Teo-Asinari/blender-workflow-lab/releases)
   (for example `impasto-0.15.31.zip`).
2. In Blender: **Edit > Preferences > Add-ons > Install from Disk**.
3. Enable the add-on.

Copying `addons/<name>/` (minus `tests/`) into `scripts/addons/` remains a
developer option.

## In use

### [Sculpt Stroke Recorder](addons/sculpt_stroke_recorder/) — v0.1.0

Records completed native Sculpt Mode strokes—including 3D locations, pressure,
radius, tilt, and timing—without replacing Blender's sculpt interaction. Takes
are stored in the `.blend` and can be replayed with the current brush, providing
a deterministic workflow tool and the structured demonstrations needed for a
future imitation-learning sculpt assistant.

### UV Island Overlay

![UV islands and texel density shown together](media/screenshots/uv-island-overlay/Screenshot%202026-08-11%20015455.png)

### Kiln

| High-poly source and plain low-poly target | High-poly source and baked low-poly result |
| --- | --- |
| ![High-poly source beside the unbaked low-poly target](media/screenshots/kiln/Screenshot%202026-08-11%20020629.png) | ![High-poly source beside the baked low-poly result](media/screenshots/kiln/Screenshot%202026-08-11%20020803.png) |

### Calipers

![Scale-aware voxel-remesh estimates and viewport guide](media/screenshots/calipers/Screenshot%202026-08-11%20020208.png)

### [Seam Path Tool](addons/seam_path_tool/) — v1.4.0

Interactive shortest-path UV seam marking in Edit Mode: click points on the mesh, each click commits a seam along the shortest path from the last anchor, with a live preview of the candidate path under the cursor. Occlusion-aware vertex picking, erase mode, per-segment undo, on-screen help panel. Fast on large meshes: commits reuse the previewed path (no pathfinding on click), and the hover path tree solves at C speed via an optional scipy dependency (pure-Python fallback included).

### [UV Island Overlay](addons/uv_island_overlay/) — v1.5.2

Viewport overlay that colors each UV island distinctly and/or drapes a texel-density checkerboard through the actual UVs — the default combined mode shows both at once (hue = island, checker scale = density). Islands can be computed from true UV connectivity or *predicted from seams live as you mark them*, no unwrap needed. Per-island density stats, deviation tint, live opacity controls. Drawn as a GPU overlay; the mesh is never modified.

Its **UV Health** section audits collapsed and duplicate UV mappings,
out-of-tile coordinates, low-density islands, and islands whose narrow span is
only a few pixels at the selected texture resolution. Each category can be
selected directly in face Edit Mode.

### [Kiln](addons/kiln/) — v1.2.1

A guided high-poly → low-poly normal-baking workflow in one sidebar panel:
pair the sculpt with a retopo mesh (existing, or a generated QuadriFlow
candidate), pass a bake-readiness checklist, then run the complete selected-to-
active bake without manually assembling nodes and settings. Automatic ray
distance, manual inner/outer shells, and an explicit cage are presented as
distinct projection modes with viewport guides; the explicit cage is the most
predictable option for difficult meshes. Kiln configures Cycles, the target
image and nodes, saves to `//textures/`, wires the normal map, and restores the
previous Blender state. Bakes integrate with an existing Impasto stack instead
of replacing its Normal connection. Normals-only for now.

### [Calipers](addons/calipers/) — v1.2.0

Scale-aware voxel-remesh preview and safety (the Proposal §5 prototype). A
sidebar panel shows, for both Sculpt Mode Voxel Remesh and the Remesh modifier,
what the current voxel size means for the selected mesh: cell counts along each
axis, a green/yellow/red cost band, bounding-box dimensions, and scale warnings.
Safe modifier creation and destructive-remesh preflight prevent Blender's
default `0.1 m` voxel size from triggering a prohibitively expensive operation
without review. A viewport guide draws grid slices and voxel-sized samples at
all eight bounding-box corners so scale can be judged visually.

### [Impasto](addons/impasto/) — v0.15.24 (active development)

A non-destructive Principled-PBR layer stack with Fill, Paint, and pass-through
Group layers. One logical Paint layer can own separate Base Color, Metallic,
Roughness, Tangent Normal, Height, Emission, and Subsurface images, with
generated node graphs that keep those channels composited independently. Kiln
normal bakes can become the stack's baseline normal layer without damaging the
active painting setup.

A Standard stack can be expanded later from **Add Material Channel** without
recreating it. Emission Color/Strength and the paintable Subsurface
Weight/Radius/Scale channels can be registered and bound to the selected Paint
layer in place; new canvases inherit that layer's resolution and existing
bindings/images remain untouched. Subsurface IOR and Anisotropy are supported
as register-only material channels rather than paint canvases.
A stack-wide selector chooses 1K, 2K, 4K, or experimental 8K for newly
created Paint layers.

Impasto's default and primary painting path is **GPU Paint All Channels**. It
keeps channel textures GPU-resident, previews the composed PBR result while
painting, and flushes them to Blender Images only at explicit synchronization
boundaries or on session exit. Blender's native Texture Paint remains available
for single-channel work. The older **Blender Brush Replay** path is retained
only as a prototype: replaying a stroke separately into each channel is too
slow and delayed for practical painting.

The streamlined GPU workflow includes a brush-sized reticle, front-surface
depth rejection for both painting and preview, per-stroke multi-channel GPU
undo/redo, continuous pressure-aware tablet strokes, and a Lit PBR preview with
adjustable environment, key, and fill lighting. It uses Blender corner normals
for smooth shading and makes roughness, metallic, tangent-normal, and Height
changes visible without routine synchronization. A shared image stencil can
act as a viewport stencil, per-dab alpha, or grayscale normal profile. In the
common same-UV/topmost-active-layer case, lower Fill/Paint layers—including
alpha-zero Kiln normals—are composed as a resident baseline from the first
preview draw. Kiln and Impasto tangent-normal layers compose bottom-up with
Reoriented Normal Mapping (RNM), preserving base form and upper detail across
the generated material, resident preview, and flattened Normal export. The
preview remains a perceptual approximation rather than Blender's exact
Material Preview HDRI.

For materials whose existing normal map cannot enter Impasto's restricted
resident stack, GPU painting also offers an explicit **Base Normal Map**
fallback. This user-validated manual workflow supplies that image, UV map,
strength, and optional green-channel inversion to Lit PBR and the normal
diagnostic previews only. It does not edit the material node graph, painted
images, flattened output, or Blender's authoritative Material Preview.
Rebuild automatically discovers a material-level `Kiln Bake Target` image and
imports it as the bottom normal layer; the explicit Base Normal picker remains
the authoritative preview-only manual override.
Impasto 0.15.15 added user-validated **Conservative UV Seam Paint** for
Paint and Erase. It evaluates the continuous brush footprint in a narrow
face-clamped boundary strip, eliminating the white staircase-like texel gaps
seen along UV islands on a complex 4K production mesh. The mode remains
default-off while extremely close exterior gutters and broader GPU coverage are
qualified.
Impasto 0.15.16 removes an unnecessary per-dab full-mesh UV scan from ordinary
GPU Paint and Erase and adds detailed stroke timings to guide the remaining 4K
performance work.
Impasto 0.15.17 replaces repeated conservative-seam Python scans with an exact
cached vectorized lookup and avoids GPU undo copies for strokes already known
to exceed the atomic undo budget; gradually expanding strokes stop recording
when they cross that limit.
On the production 4K validation mesh this made seam selection approximately
29× faster and reduced total live flush processing from 28.1 to 7.7 ms per
flush (3.65×). Detailed measurements and caveats are maintained in the
[Impasto GPU performance history](addons/impasto/docs/PERFORMANCE_HISTORY.md).
Impasto 0.15.18 makes Paint/Erase Undo sparse across scattered UV islands,
avoiding tile capture for the empty atlas space between hit regions.
Impasto 0.15.19 stops sparse bookkeeping after Undo becomes impossible, clips
camera-crossing triangles conservatively to prevent extreme zoom amplification,
and adds aggregated passive-hover profiling.
Impasto 0.15.20 removes the synchronous GPU-completion read that made every
camera-change depth prepass block viewport navigation.
Impasto 0.15.21 defers the remaining full-mesh CPU projection/clipping work
until painting resumes, keeping it out of viewport orbit and zoom frames.
Impasto 0.15.24 reuses process-stable capability probes and adds detailed,
bounded startup and shutdown phase telemetry for the remaining lifecycle delays.
Flatten/Export to combined per-channel Blender Images is implemented. Paint,
Soften, Smear, and Erase have independent per-channel target controls.
Same-UV visible layers remain composed around an intermediate active layer;
common affine Fill and single Paint-image upper layers update live while
brushes still write only to the active layer. Paintable per-layer masks now
support visibility, inversion, opacity, per-channel scope, generated materials,
and flattened export. A persistent brush-material preset palette provides
spherical color swatches and detailed channel-value tooltips. Mixed-UV resident
preview, complex upper sequences, arbitrary Blender brush textures, and
specialized brush parity remain future work. Ctrl-S
safely flushes before saving; menu-driven
save/export should be preceded by **Flush for Save / Export**.

## Documents

- [PROPOSAL.md](PROPOSAL.md) — the full proposal: the features above, plus
  longer-term layered painting and voxel-sculpting changes that ultimately need
  work in Blender's core.
- [research/](research/) — technical research feeding the flagship designs.
- [Impasto documentation](addons/impasto/docs/README.md) — current workflow,
  roadmap, changelog, technical references, and archived design history.
- [Impasto GPU performance history](addons/impasto/docs/PERFORMANCE_HISTORY.md)
  — measured optimization results and remaining performance bottlenecks.

## Approach

Features are piloted as Python add-ons with agent-assisted development. Each
add-on carries a test suite (`tests/run_tests.sh`) that exercises a real Blender
binary, including — where the domain allows — end-to-end assertions. Kiln's
suite performs an actual Cycles normal bake and checks its pixel statistics;
Impasto additionally has foreground GPU smoke coverage because viewport draw
handlers cannot be validated completely in background mode. API behavior is
probed against the running binary rather than assumed, and the traps found
along the way are documented in the add-on READMEs.

## Status

Active development. Seam Path Tool, UV Island Overlay, Kiln, and Calipers have
complete guided workflows. Impasto's layer stack, flattening, masks, stencils,
layered normals, and GPU multi-channel Paint/Erase workflow are usable. Its GPU
path remains under active qualification—especially 8K, complex mixed-UV stack
previews, sparse undo capture, and Soften/Smear seam behavior.

## Distribution

Users install from GitHub Release ZIPs via **Install from Disk**. Each add-on
ships a `blender_manifest.toml` (Blender 4.2+ extensions schema) and a
self-contained `LICENSE` (GPL-2.0-or-later).

Build the ZIPs locally with:

```
python scripts/package_addons.py
python scripts/package_addons.py impasto
```

Archives land in `dist/` (gitignored) as `<id>-<version>.zip`, matching
`bl_info` — for example `calipers-1.2.0.zip`, `kiln-1.2.1.zip`,
`uv_island_overlay-1.5.2.zip`, `seam_path_tool-1.4.0.zip`,
`impasto-0.15.31.zip`, `sculpt_stroke_recorder-0.1.0.zip`. Each ZIP has the
add-on folder at archive root (`impasto/__init__.py`, not `addons/impasto/...`).
Tests, `__pycache__/`, probes, and non-runtime docs are excluded; runtime
Python, `assets/`, `blender_manifest.toml`, `LICENSE`, and `README.md` are
included. Packaging does not require SciPy; Seam Path Tool's optional SciPy
extra remains a user-side install.

### Cutting a GitHub Release

Push a git tag matching `v*` (for example `v2026.08.28`) to run
`.github/workflows/package-addons.yml`. The workflow builds every add-on ZIP
and attaches them as release assets. `workflow_dispatch` builds the same ZIPs
as CI artifacts without creating a release. Do not push a tag until the
add-on versions in `bl_info` are the ones you want on the Release.

Submitting stable add-ons to the [Blender Extensions](https://extensions.blender.org/)
platform is a later step (Calipers, Kiln, UV Island Overlay first, then Seam
Path Tool). Impasto and Sculpt Stroke Recorder stay labeled experimental.

## License

The add-ons are GPL-2.0-or-later (as Blender add-ons must be; see SPDX headers). Documentation and research notes: all rights reserved for now.
