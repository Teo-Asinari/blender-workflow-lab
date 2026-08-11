# Blender Workflow Lab — Core Improvements Proposal

A proposal for enhancing Blender with features and workflows inspired by those found in specialized tools such as 3DCoat and Substance Painter. The goal is to bring Blender's sculpting, painting, and UX capabilities closer to — or beyond — dedicated 3D content creation software, either through add-ons or upstream contributions.

> This is an independent project. It is not affiliated with, endorsed by, or derived from any product mentioned; product names are used only for factual comparison of workflows. All code in this repository is original.

---

## Proposed Features

### 1. Voxel-Based Sculpting

Implement a true voxel sculpting workflow, comparable to those in dedicated volumetric sculpting tools. Blender currently relies on mesh-based sculpting (multires, dyntopo), which has fundamental limitations when it comes to topology-free modeling.

A true voxel engine would enable:

- **Topology-free sculpting** — no stretching, pinching, or polygon-count artifacts
- **Real-time boolean/CSG operations** on sculpted volumes
- **Seamless resolution changes** without mesh dependency
- **Volume manipulation** — add, subtract, and blend material freely
- **Clean retopology pass** after voxel sculpting is complete

**Research needed:** Blender's data model, OpenVDB integration status, 3DCoat's voxel engine approach, performance considerations for real-time voxel rendering.

---

### 2. PBR Texture Painting with Layers

Blender's texture painting system is one of its weakest areas compared to dedicated tools. This proposal aims to bring it to parity with Substance Painter and 3DCoat.

#### Current Problems (Well-Documented)

- **No native layer system.** Blender only has separate image texture slots that must be manually wired into shader nodes. There is no layer stack, no blend modes, no opacity control, no masks within the paint interface.
- **Single-channel painting only.** You can only paint to one PBR channel at a time (e.g., Base Color OR Roughness), requiring constant slot switching. No simultaneous multi-channel painting.
- **Persistent brush lag.** Documented across multiple Blender versions — soften/smear brushes, large brush sizes, and tablet input all suffer from significant lag ([T58465](https://developer.blender.org/T58465), [T74753](https://developer.blender.org/T74753), [T60965](https://developer.blender.org/T60965), [Issue #93796](https://projects.blender.org/blender/blender/issues/93796)).
- **Broken viewport preview.** Painted changes don't appear in real-time in Cycles or Material Preview mode — only Solid shading reflects changes live ([Issue #86787](https://projects.blender.org/blender/blender/issues/86787), [T101032](https://developer.blender.org/T101032)).
- **Memory issues.** Undo history on 4K–16K textures causes runaway RAM consumption. The undo memory limit setting doesn't reliably cap usage.
- **Manual PBR setup.** Each channel texture must be created separately, marked with correct color space (Non-Color for roughness/metallic), and manually connected to the Principled BSDF node.

#### Proposed Solution

**A. Layer Stack System**

- Full non-destructive layer stack with blend modes (multiply, overlay, screen, etc.), per-layer opacity, and visibility toggles
- Fill layers, adjustment layers, and procedural layers
- Smart masks (curvature-based, ambient occlusion, cavity, edge detection)
- Layer groups and clipping masks
- Dedicated, streamlined layer management panel in the paint workspace — no need to navigate the Shader Node Editor to manage paint layers

**B. PBR Multi-Channel Painting**

- **Template materials:** Select a PBR template (e.g., Principled BSDF) that automatically creates and wires all necessary channel textures (Base Color, Roughness, Metallic, Normal, Height, Emissive, AO, etc.)
- **Per-channel toggles:** Enable or disable individual PBR channels to customize which channels exist for a given material — not every material needs every channel
- **Simultaneous multi-channel painting:** A single brush stroke can affect multiple channels at once (e.g., paint a scratch that is both lighter in base color and higher in roughness)
- **Channel isolation:** Quickly solo/isolate a single channel for focused painting
- **Smart defaults:** Correct color spaces assigned automatically (Non-Color for roughness, metallic, normal, etc.)

**C. Performance**

- GPU-accelerated painting pipeline to eliminate brush lag
- Efficient undo system that doesn't consume unbounded memory on large textures
- Real-time viewport preview in all shading modes, not just Solid
- Optimized handling of 4K–8K+ texture resolutions

#### Prior Art

- Blender developers published a [Layered Textures Design proposal](https://code.blender.org/2022/02/layered-textures-design/) in February 2022 but it has not shipped as of 2026.
- The [Paint Mode Design Discussion](https://devtalk.blender.org/t/paint-mode-design-discussion-feedback/24243) proposed building a unified Paint Mode on top of Sculpt Mode's architecture.
- Third-party add-ons (Layer Painter, PBR Painter 3, Ucupaint, HAS Paint Layers) demonstrate demand and feasibility but are limited by Blender's architecture.

---

### 3. Interactive Brush Size & Intensity Control (Single Gesture)

In some dedicated sculpting tools (e.g. 3DCoat), holding the right mouse button and dragging allows interactive adjustment of brush **size** (horizontal drag) and **intensity/strength** (vertical drag) in a single gesture, without menus or keyboard shortcuts. This is significantly faster than Blender's current `F`-key resize workflow.

**Detailed specification to follow.**

---

### 4. UX and Navigation Improvements

General improvements to viewport navigation, tool switching, and overall workflow efficiency, drawing on design decisions proven in dedicated sculpting/painting tools.

#### Sidebar discoverability and ergonomics

The 3D Viewport sidebar (N-panel) is the only practical home Blender offers add-on UI, yet it is hard to discover and uncomfortable to use: it opens from a small edge arrow (or a shortcut the user must already know), and its vertical tab strip renders at a fixed small size that does not grow with the number of installed add-ons competing for it. Every sidebar-based tool inherits this friction. Proposed directions: a visible, always-on affordance for opening the sidebar; larger/legible tab targets (or user-scalable tabs); and first-class support for summoning any sidebar panel as a floating panel at the cursor.

**Further pain points and proposed solutions to follow.**

---

### 5. Scale-Aware Remesh Preview and Safety

Voxel remeshing can launch an unexpectedly expensive operation when the current voxel size is badly mismatched with the mesh. A value such as `0.1 m` may be reasonable for one object but imply a prohibitively large voxel domain for another. The numeric value alone does not communicate the resulting resolution, cost, or memory risk.

Before executing a voxel remesh, Blender should provide an interactive, mesh-relative sizing step.

#### Proposed Interaction

- **No remesh by default when adding the Remesh modifier.** Today the modifier evaluates immediately on add, with the datablock default voxel size (`0.1`) — on a dense or badly-scaled mesh this can stall Blender or exhaust memory before the user has entered a sensible value. The modifier should arrive in a pending (non-evaluating) state, or with a mesh-derived safe initial size, and only compute once the user confirms the voxel size.
- Show the voxel size together with estimated voxel counts along the mesh bounding-box axes, approximate domain size, and a coarse cost or memory-risk indicator.
- Let the user adjust voxel size interactively in the viewport before computation begins.
- **Draw a mesh-relative visual guide for the voxel size** — a voxel-sized sample box on the surface, a sparse 3D grid clipped to the object bounds, or representative grid slices — so the user can *see* the cell size against the mesh instead of judging an abstract number. Never draw every voxel. Applies to both the sculpt-mode overlay and the modifier panel.
- Clearly flag both extremes: voxels too coarse to preserve the form and voxel counts likely to cause excessive computation or memory use.
- Offer a mesh-relative suggested size and require confirmation above a configurable safety threshold. Warnings should not impose a hard limit.
- Preserve a fast path for experienced users who intentionally repeat the last settings.

The control should cover every relevant voxel-remesh entry point. Exact integration may differ between sculpt-mode Voxel Remesh and modifier-based workflows, but neither should begin an expensive default operation before communicating its scale.

#### Design Constraints

- Estimation and preview must be cheap and must not construct the full voxel grid.
- Object transforms and dimensions must be handled consistently, with a warning when unapplied scale would produce a surprising result.
- The aid must remain legible across very small and very large scenes.

**Research needed:** identify voxel-remesh entry points and defaults; derive useful cost estimates from object bounds, voxel size, and the voxel domain; prototype the viewport preview; and test warning thresholds across meshes at widely different physical scales.

---

## Implementation Strategy

The implementation path is under investigation. Possible approaches:

| Approach | Pros | Cons |
|---|---|---|
| **Blender Add-on (Python)** | Easy to distribute, no fork needed | Limited by Python API, poor performance for heavy features |
| **C/C++ Patch (PR to Blender)** | Full access to internals, best performance | Must align with Blender Foundation roadmap and standards |
| **Hybrid** | Core engine in C/C++, UI/workflow in Python | More complex build/distribution |

Some features (brush gesture control, basic layer UI) may be achievable as add-ons. Others (voxel sculpting, GPU paint pipeline) almost certainly require C/C++ work.

---

## Research Checklist

- [ ] Blender's sculpt and paint source code architecture
- [ ] 3DCoat's voxel engine — what makes it effective
- [ ] OpenVDB integration in Blender (current status and potential for sculpting)
- [ ] GPU texture painting techniques and Blender's current GPU paint path
- [ ] Blender Python API limitations for real-time input handling
- [ ] Existing add-ons (Layer Painter, PBR Painter, Ucupaint) — capabilities and limitations
- [ ] Blender Foundation contribution process and coding standards
- [ ] Substance Painter's PBR channel workflow for reference
- [ ] 3DCoat's brush size/intensity gesture implementation details
- [ ] Voxel-remesh entry points, defaults, cost estimation, and viewport preview APIs

---

## References

- [3DCoat](https://3dcoat.com/)
- [Substance Painter](https://www.adobe.com/products/substance3d-painter.html)
- [Blender Developer Documentation](https://developer.blender.org/)
- [Blender Source Code](https://projects.blender.org/blender/blender)
- [Layered Textures Design — Blender Developers Blog (2022)](https://code.blender.org/2022/02/layered-textures-design/)
- [Paint Mode Design Discussion](https://devtalk.blender.org/t/paint-mode-design-discussion-feedback/24243)
- [2025-01-28 Sculpt, Paint & Texture Module Meeting](https://devtalk.blender.org/t/2025-1-28-sculpt-paint-texture-module-meeting/38779)

---

*This is a living document. Technical details and research findings will be added as the project progresses.*
