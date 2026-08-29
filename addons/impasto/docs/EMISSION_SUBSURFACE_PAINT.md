# Emission and subsurface paint contract

This contract targets Blender 5.1's Principled BSDF sockets and keeps stored
canvases authoritative. The resident viewport is an interactive approximation;
the compiled material is authoritative after synchronization.

## Paintable channels

| Impasto key | Principled socket | Stored domain | Stroke control | Neutral/default |
|---|---|---|---|---|
| `emission_color` | Emission Color | sRGB image, linear UI color encoded once on deposit | linear RGB | white |
| `emission_strength` | Emission Strength | Non-Color scalar | non-negative radiance multiplier, intentionally allowed above 1 | 0 |
| `sss_weight` | Subsurface Weight | Non-Color scalar | 0..1 fraction | 0 |
| `sss_radius` | Subsurface Radius | Non-Color RGB vector | non-negative per-channel scattering-radius ratios | (1, 0.2, 0.1) |
| `sss_scale` | Subsurface Scale | Non-Color scalar distance | non-negative scene-length scale | 0.05 scene units |

Blender 5.1 RNA identifies Scale as `NodeSocketFloatDistance`, Radius as a
plain vector, and Weight as a factor. Accordingly, Radius is not presented as
an RGB display color and is not color-managed; Scale carries Blender's length
unit. Painting Radius and Scale is meaningful because together they control
the per-channel scattering distance. Painting Weight alone remains the simple
and most common subsurface workflow.

`sss_ior` and `sss_anisotropy` remain compiler-supported but are not brush
targets in this iteration. They are specialized model controls, are easy to
misinterpret as appearance colors, and do not describe spatial scattering
distance. They remain usable as Fill/binding values.

## Composition and resident preview

All five channels use normal MIX deposition. Brush opacity and canvas alpha
gate them exactly once, so one multi-channel stroke has one footprint and one
undo transaction. Emission Color and Strength remain separate: HDR luminosity
is never baked into or clipped by the color canvas.

The live Lit PBR preview adds `emission_color * emission_strength` before its
tone mapper, allowing values above display white to create a visibly stronger
shoulder without clipping stored strength. Subsurface is shown as an
environment-lit forward/back-scatter approximation driven by Weight and the
Radius-times-Scale distance. It communicates direction and magnitude while
leaving Blender's Principled renderer authoritative.

Native Texture Paint activation remains available per canvas. Blender brush
replay writes the same channel-specific values, colorspaces, and MIX mode as
the resident GPU brush.

## Packed-canvas flush (0.15.33)

GPU flush must `pack()` after writing pixels when the canvas is already a
packed FILE image. Flatten/export already did this; GPU flush did not.

That gap stayed hidden while most paint canvases were generated (pixels
stored in the `.blend`). Packed emission canvases show the flushed pixels
in the viewport immediately, then File Save or `image.reload()` can restore
the previous packed PNG. BlackBody_Low Emission Color/Strength on
2026-08-29 lost today's white emission points this way. An empty Base Color
binding on an emission-only layer is intentional and not the same bug.
