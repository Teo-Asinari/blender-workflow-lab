# Calipers — extensions.blender.org listing copy

Paste these into the submit form. ZIP: `dist/calipers-1.2.0.zip`

## Support URL

https://github.com/Teo-Asinari/blender-workflow-lab/issues

## Website (if asked separately)

https://github.com/Teo-Asinari/blender-workflow-lab

## Description

Calipers sits in front of Blender's voxel remesh so you can see cell size and cost **before** a heavy operation runs.

Voxel size is an **object-space** length. Unapplied or non-uniform scale makes the number you typed differ from the cell size you see in the world. The datablock default (`0.1`) also ignores your mesh: on a large scan it can mean on the order of a billion bounding cells. Adding a Remesh modifier evaluates immediately at that default, which can stall Blender or exhaust memory.

The **Calipers** tab in the 3D Viewport sidebar (`N`) covers two separate entry points. They share arithmetic only; settings are never mixed.

**Voxel Remesh (destructive)**

- Shows `Mesh.remesh_voxel_size` verbatim (object space).
- Green / yellow / red risk band, longest-axis cell count, bounding-cell score, relative surface score, world-space cell size per axis, and scale warnings (unapplied, non-uniform, negative, shear).
- **Set from World Target** writes the object-space size so no transformed axis is coarser than the world-space cell size you want.
- **Voxel Remesh (Preflight)** confirms the estimate, then runs Blender's native operator. Warnings never hard-block. Sculpt Mode `Ctrl-R` and the native operator are unchanged.

**Remesh modifier (voxel mode)**

- **Add Remesh Modifier (Safe)** adds the modifier **pending** (`show_viewport` and `show_render` off) with an initial size from evaluated bounds (longest axis / Initial Cells, default 64), so the first evaluation does not use the scale-blind default.
- While pending, **Enable Remesh Modifier** is the expensive step; the estimate is on screen first.

**Visual guide**

A GPU overlay in object space (so unapplied scale is visible): bounding box, a sample cell at each corner, three mid-plane grid slices (capped; never draws every voxel), and optional world-space box dimensions. Source: Auto, Mesh, or Modifier.

**What this add-on cannot cover**

The native Add Modifier menu still evaluates Remesh immediately. Direct `voxel_remesh` calls (including Sculpt `Ctrl-R`) skip the preflight. Scores are relative domain indicators, not OpenVDB memory predictions.

Requires Blender 4.2 or later. Developed against 5.1.2. GPL-3.0-or-later.

## Release notes

**1.2.0**

- Sidebar estimates for destructive Voxel Remesh and the voxel Remesh modifier, with green/yellow/red risk bands.
- Safe-add for the Remesh modifier: pending (viewport and render off) until you confirm Enable.
- Set from World Target converts a world-space cell size into the correct object-space voxel size, including under shear.
- Viewport voxel-size guide: bounds, corner sample cells, capped grid slices, optional box dimensions.
- Scale warnings for unapplied, non-uniform, negative scale, and shear.
- Confirming preflight wrapper for destructive remesh; native keymap left intact.

## Images

Windows paths for the upload picker:

- **Featured (1920×1080):** `\\wsl.localhost\Ubuntu\home\tasinari\my_repos\blender-workflow-lab\media\extensions\calipers\featured-1920x1080.png`
- **Icon (512×512):** `\\wsl.localhost\Ubuntu\home\tasinari\my_repos\blender-workflow-lab\media\extensions\calipers\icon-512.png`
- **Preview (full UI):** `\\wsl.localhost\Ubuntu\home\tasinari\my_repos\blender-workflow-lab\media\extensions\calipers\preview-full-ui.png`

The featured image and preview are crops of a real viewport screenshot. The icon is a generated mark (no Blender logo). If the form wants a square icon next to the title, use `icon-512.png`.
