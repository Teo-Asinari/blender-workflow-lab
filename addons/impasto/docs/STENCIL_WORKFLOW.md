# Impasto cross-channel image stencil and brush alpha v1

## User workflow

The selected Paint layer's **Experimental GPU Brush** section contains an
**Image Stencil** toggle and Blender Image selector. One selected image
modulates the coverage of every channel enabled on that Paint layer.

Two projection modes are available:

- **Planar Viewport** fixes the image in the active viewport. Position is a
  normalized viewport center: `(0.5, 0.5)` is the center. Scale is the image's
  normalized viewport width and height: `(0.5, 0.25)` spans half the viewport
  width and one quarter of its height. Rotation is counter-clockwise.
- **Brush Footprint** centers the image on every GPU dab. Position is irrelevant;
  its independent Brush Scale multiplies the brush diameter on X and Y. Its
  `(1, 1)` default maps the image exactly across the round dab footprint on
  first use. Switching modes never reuses or overwrites Viewport Scale.
  Rotation remains counter-clockwise.

**Alpha** interpretation reads the image alpha channel. **Luminance** uses
linear Rec.709 RGB luminance, useful for grayscale files without useful alpha.
Stencil Opacity multiplies the interpreted mask. Pixels outside the projected
image contribute zero coverage.

The image controls coverage only. Channel stroke values remain configured in
Impasto; their spatial mask is identical because the shader samples the stencil
once into the shared falloff before writing any MRT attachment.

## Performance and lifecycle

- There is one image sample per covered fragment per blend batch, not one per
  channel. Height's separate additive batch necessarily evaluates it again.
- `gpu.texture.from_image` is resolved in the owning draw context and cached by
  Image name. Transform/value edits do not recreate the texture.
- Pen-up remains GPU-only. Stencil painting causes no readback or Image write.
- Front-surface visibility runs before stencil evaluation, so the stencil does
  not reintroduce painting through the mesh.
- GPU tile history still records every affected channel in one atomic stroke.
- Settings refresh between strokes preserves resident textures and history.

## Normal-detail alpha contract

3DCoat-style alphas have two distinct semantics:

1. **Coverage mask:** intensity controls where configured channel values are
   deposited. This is fully implemented in v1.
2. **Normal profile:** intensity is a height/profile field. Aspect-corrected
   neighboring texel samples generate tangent-space detail; Strength controls
   magnitude and Invert reverses raised/recessed relief. The detail is
   slope-composed with the configured Normal paint value. In the same stroke,
   intensity remains the coverage mask for every other enabled material
   channel, so one stencil can deposit Normal, Base Color, Metallic,
   Roughness, and other configured values together.

The pure `stencil.profile_tangent_normal()` contract mirrors the shader's
profile polarity, central differences, strength, inversion, and encoded normal
output. `StencilSettings` carries `usage`, `profile_strength`, and
`profile_invert` between the persistent layer and GPU session.

Linked Height deposition is explicitly deferred. It needs separate repeated-
stroke/additive semantics and must not be inferred from Normal-profile opacity.

## Deferred work

- Direct viewport **translate** handles for Planar Viewport stencils.
  Scale and rotate handles ship in 0.15.27 (corners/edges and a top rotate
  knob; Shift preserves aspect). Position remains numeric.
- Preserve-aspect, fit/reset, rake, jitter, random rotation, tiling, UV, and
  triplanar projection.
- Color texture application into channel values; v1 uses one synchronized mask.
- Optional linked Height deposition for Normal Profile.
- Masks sourced from Impasto layers or procedural node graphs.
- The enlarged dab parameter set now uses a vec4-aligned uniform buffer rather
  than push constants, staying clear of the portable 128-byte push-constant
  limit as stencil and MRT state grows.
