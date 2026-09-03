# Keyboard-first Blender UX direction

Blender remains the host, but frequent Blender Workflow Lab actions should not
depend on navigating narrow sidebar panels or clicking small controls. The
project should incrementally provide a Vim-style command layer: mnemonic key
sequences, explicit modes, numeric prefixes, repeat-last-action, a searchable
command palette, and a large transient HUD showing the active mode, pending
keys, and results. Sidebars should become configuration and inspection views,
not the primary way to work.

The immediate paint workflow exposes why this matters:

- Stroke Recorder appears to require entering Blender Texture Paint mode in
  normal use. Verify and remove any such coupling for Impasto: starting,
  monitoring, stopping, and replaying an `impasto_gpu` take must work directly
  from an active Impasto GPU paint session, without activating Blender's native
  Texture Paint workspace or tools.
- Blender's Texture Paint mode introduces its own large brush/tool icons. They
  consume viewport space and visually compete with Impasto even though Impasto
  already owns a substantial brush and material interface. The keyboard-first
  workflow should let an Impasto user avoid this redundant native paint UI.
- Stroke Recorder lacks basic take deletion. Add a keyboard-accessible delete
  command and a clear take-management control, require confirmation when the
  take contains strokes, and remove the corresponding compressed sidecar data
  rather than leaving orphaned payloads behind.
- Impasto's growing stencil, channel, material, preview, and recording controls
  are too dense for a sidebar-first interface. High-frequency actions should be
  commands; the HUD should provide state and feedback; detailed controls can
  remain in collapsible panels.

This is a partial UX overhaul, not a new DCC. Blender continues to provide the
mesh, paint, dependency-graph, rendering, and file infrastructure. The first
deliverable should be a shared add-on-level command registry and modal HUD that
Impasto and Stroke Recorder can register commands with, followed by user-editable
key sequences and macros.
