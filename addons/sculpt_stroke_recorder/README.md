# Stroke Recorder

Records completed native Blender **Sculpt Mode**, **Texture Paint**, and
**Impasto GPU** strokes without replacing those operators, then replays a
take from the stored samples. Take metadata stays in the Scene; completed
stroke streams are compressed into `<project>.blend.stroke-recordings.json.gz`.

The Python module and ZIP id remain `sculpt_stroke_recorder` so existing
installs and `.blend` takes keep loading.

## Install

This add-on is experimental. Rebuild the ZIP with
`python scripts/package_addons.py sculpt_stroke_recorder` and install
`dist/sculpt_stroke_recorder-0.4.0.zip` via **Edit > Preferences > Add-ons >
Install from Disk**, then enable **Stroke Recorder**. Copying or
symlinking the `sculpt_stroke_recorder` folder into `scripts/addons/`
remains a developer option. Impasto GPU capture needs Impasto 0.15.32+
enabled as well.

## Use

1. Select a mesh and enter **Sculpt Mode**, **Texture Paint**, or start
   **Impasto GPU painting**.
2. Open **3D Viewport > Sidebar (N) > Stroke Recorder**.
3. Press **Record New Take**, work normally, then press **Stop Recording**.
4. Select a take and press **Replay Take** in the same mode (or with GPU
painting still running, for Impasto takes).

While recording, the viewport shows a red **● REC** HUD with the take name,
mode, and live completed-stroke count. The sidebar also shows an alert state.
Recording can also be started or stopped from the persistent **REC/STOP**
control in the 3D Viewport header or with **Shift+Alt+R**, without opening the
Stroke Recorder sidebar tab.

**Recording Detail** defaults to compact **Basic** storage. Choose
**Enhanced** to independently capture viewport/camera state, sampled mesh
surface hits, object/material/Impasto layer context, and expanded brush state.
Surface Sample Stride controls the cost and density of ray casts; `1` samples
every pointer event and larger values reduce storage and recording overhead.
Enhanced fields are additive and remain replay-compatible. The individual
context switches may be changed before or during a take.

Impasto recording does **not** require Blender Texture Paint mode or its
workspace. While an Impasto GPU paint session is active, its stroke stream
takes precedence over Blender's incidental object/tool mode and the recorder
creates an `Impasto GPU` take.

Delete the selected take with the trash button, Blender's operator search, or
**Shift+Alt+D**. Non-empty takes ask for confirmation. Deletion also removes
the take's compressed sidecar payload and selects the next take (or the new
last take when deleting at the end).

Replay is an Undo-enabled operation for native sculpt/paint and applies the
recorded paths with the currently active brush of that mode. Impasto GPU
replay feeds the recorded pointer stream back into the live GPU session, so
Impasto's own stroke undo applies. This makes recordings useful immediately
while keeping the stored action data suitable for later imitation-learning
work.

A take is locked to the mode it started in. Switching from Sculpt to
Texture Paint or Impasto GPU (or the reverse) mid-take does not mix
operators into the same recording.

## Recorded data

Each completed stroke stores Blender's native sample stream, or Impasto's
GPU pointer stream:

- which path produced it (`sculpt`, `texture_paint`, or `impasto_gpu`);
- object-space 3D location (sculpt) or region-relative mouse (paint / GPU);
- mouse and mouse-event coordinates;
- pressure and brush size;
- pen tilt;
- relative sample time and start marker;
- stroke mode, pen flip, brush toggle, and a brush-settings snapshot.

The snapshot is metadata; replay deliberately uses the current
brush so it does not silently change the artist's tool or depend on a
missing brush asset.

Takes saved by v0.1.0 remain sculpt takes. v0.2.0 texture-paint takes remain
texture paint.

## Current boundaries

- The `.blend` must be saved before a sidecar path exists. Unsaved projects
  temporarily retain stroke streams in the Scene and externalize them after a
  later recording is stopped once the project has been saved.
- A take replays best on the same object and topology (sculpt), the same
  active paint canvas and view (texture paint), or the same Impasto GPU
  session, camera, and viewport size (Impasto GPU).
- Impasto GPU samples are region-relative. Replay after resizing the
  3D View or orbiting the camera will miss.
- Impasto GPU replay is sequential and waits for each stroke's GPU finalize;
  long takes are slower than native sculpt replay.
- Replay of native takes is sequential and can be expensive for long takes.
- The native recorder depends on completed strokes remaining visible in
  Blender's runtime operator history.
- Dyntopo/remesh operations, masks, face-set changes, view changes, and other
  non-stroke sculpt actions are not recorded.
- Vertex Paint and Weight Paint are not recorded.
- This is a dataset and deterministic-replay foundation, not yet a trained
  model.

## Interactive acceptance check

### Sculpt

1. Add a UV Sphere, enter Sculpt Mode, and start a take.
2. Make three clearly separated Draw strokes with different pressure.
3. Stop recording; the selected row must say `3 sculpt`.
4. Undo the three strokes, then press **Replay Take**.
5. All three deformations must return in their original locations.
6. Undo once; the entire replay should be reverted as one operator action.
7. Save and reopen the `.blend`; the take and its sample payloads must remain.

### Texture Paint

1. Add a UV Sphere, unwrap it, assign a new image in Texture Paint, and
   start a take.
2. Make three clearly separated Draw strokes with different pressure.
3. Stop recording; the selected row must say `3 texture paint`.
4. Undo the three strokes, then press **Replay Take** still in Texture Paint.
5. The same marks must return on the canvas.
6. Undo once; the entire replay should be reverted as one operator action.

### Impasto GPU

1. Create an Impasto Paint layer with at least Base Color, start GPU
   painting, and start a take.
2. Make three clearly separated Paint strokes with different pressure.
3. Stop recording; the selected row must say `3 Impasto GPU`.
4. Undo the three GPU strokes (`Ctrl-Z` in the GPU session), then press
   **Replay Take** without leaving GPU painting.
5. The same marks must return on the resident canvases.
6. A sculpt take must refuse replay until you enter Sculpt Mode, and an
   Impasto take must refuse replay after GPU painting stops.

## Support

Report issues at
[github.com/Teo-Asinari/blender-workflow-lab/issues](https://github.com/Teo-Asinari/blender-workflow-lab/issues).

## License

GPL-3.0-or-later.
