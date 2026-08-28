# Sculpt Stroke Recorder

Records completed native Blender Sculpt Mode strokes without replacing
Blender's sculpt interaction, then replays a take from the native 3D stroke
samples. Recordings are stored in the Scene and survive saving the `.blend`.

## Install

Download `sculpt_stroke_recorder-0.1.0.zip` from the
[v2026.08.28 release](https://github.com/Teo-Asinari/blender-workflow-lab/releases/tag/v2026.08.28)
and install it with **Edit > Preferences > Add-ons > Install from Disk**,
then enable **Sculpt Stroke Recorder**. This add-on is experimental. Copying
or symlinking the `sculpt_stroke_recorder` folder into `scripts/addons/`
remains a developer option.

## Use

1. Select a mesh and enter Sculpt Mode.
2. Open **3D Viewport > Sidebar (N) > Sculpt Recorder**.
3. Press **Record New Take**, sculpt normally, then press **Stop Recording**.
4. Select a take and press **Replay Take**.

Replay is an Undo-enabled operation and applies the recorded 3D paths with the
currently active sculpt brush. This makes recordings useful immediately while
keeping the stored action data suitable for later imitation-learning work.

## Recorded data

Each completed stroke stores Blender's native sample stream:

- object-space 3D location;
- mouse and mouse-event coordinates;
- pressure and brush size;
- pen tilt;
- relative sample time and start marker;
- stroke mode, pen flip, brush toggle, and a brush-settings snapshot.

The snapshot is metadata in v0.1.0; replay deliberately uses the current brush
so it does not silently change the artist's tool or depend on a missing brush
asset.

## Current boundaries

- A take replays best on the same object and topology it was recorded against.
- Replay is sequential and can be expensive for long takes.
- The recorder depends on completed strokes remaining visible in Blender's
  runtime operator history. The interactive acceptance check below validates
  that contract on the installed Blender build.
- Dyntopo/remesh operations, masks, face-set changes, view changes, and other
  non-stroke sculpt actions are not recorded in v0.1.0.
- This is a dataset and deterministic-replay foundation, not yet a trained
  sculpting model.

## Interactive acceptance check

1. Add a UV Sphere, enter Sculpt Mode, and start a take.
2. Make three clearly separated Draw strokes with different pressure.
3. Stop recording; the selected row must say `3 strokes`.
4. Undo the three strokes, then press **Replay Take**.
5. All three deformations must return in their original locations.
6. Undo once; the entire replay should be reverted as one operator action.
7. Save and reopen the `.blend`; the take and its sample payloads must remain.

## Support

Report issues at
[github.com/Teo-Asinari/blender-workflow-lab/issues](https://github.com/Teo-Asinari/blender-workflow-lab/issues).

## License

GPL-3.0-or-later.

