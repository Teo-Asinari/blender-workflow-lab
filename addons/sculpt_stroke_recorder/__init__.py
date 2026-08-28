# SPDX-License-Identifier: GPL-2.0-or-later
"""Record completed native Sculpt Mode strokes and replay them later.

The recorder observes Blender's operator history after a native
``SCULPT_OT_brush_stroke`` completes.  It never replaces the sculpt operator,
so tablet handling and Blender's normal stroke interaction stay untouched.
Recordings are stored on the Scene and therefore survive saving the .blend.
"""

import json
import traceback

import bpy
from bpy.props import (BoolProperty, CollectionProperty, IntProperty,
                       StringProperty)


bl_info = {
    "name": "Sculpt Stroke Recorder",
    "author": "Teo Asinari",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Sculpt Recorder",
    "description": "Record native sculpt strokes and replay them from their "
                   "3D samples",
    "category": "Sculpt",
}


_TIMER_INTERVAL = 0.05
_seen_operator_pointers = set()

_SAMPLE_FIELDS = (
    "name", "location", "mouse", "mouse_event", "pressure", "size",
    "x_tilt", "y_tilt", "time", "is_start",
)


def _operator_identifier(operator):
    """Return an RNA operator identifier without depending on UI labels."""
    identifier = getattr(operator, "bl_idname", "")
    if identifier:
        return identifier
    rna = getattr(operator, "bl_rna", None)
    return getattr(rna, "identifier", "")


def _json_value(value):
    """Convert Blender scalar/vector values to JSON-compatible values."""
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return str(value)


def serialize_stroke_operator(operator):
    """Serialize one completed native sculpt operator.

    Kept independent of PropertyGroups so headless tests can exercise the
    capture contract with light-weight stand-ins.
    """
    if _operator_identifier(operator) != "SCULPT_OT_brush_stroke":
        return None
    properties = getattr(operator, "properties", operator)
    samples = []
    for point in getattr(properties, "stroke", ()):
        sample = {}
        for field in _SAMPLE_FIELDS:
            if hasattr(point, field):
                sample[field] = _json_value(getattr(point, field))
        if "location" in sample:
            samples.append(sample)
    if not samples:
        return None
    return {
        "schema": 1,
        "mode": str(getattr(properties, "mode", "NORMAL")),
        "brush_toggle": str(getattr(properties, "brush_toggle", "NONE")),
        "pen_flip": bool(getattr(properties, "pen_flip", False)),
        "samples": samples,
    }


def replay_samples(payload):
    """Return sanitized dictionaries accepted by sculpt.brush_stroke."""
    data = json.loads(payload) if isinstance(payload, str) else payload
    result = []
    for source in data.get("samples", ()):
        point = {}
        for field in _SAMPLE_FIELDS:
            if field not in source:
                continue
            value = source[field]
            if field in {"location", "mouse", "mouse_event"}:
                value = tuple(float(item) for item in value)
            elif field in {"pressure", "size", "x_tilt", "y_tilt", "time"}:
                value = float(value)
            elif field == "is_start":
                value = bool(value)
            else:
                value = str(value)
            point[field] = value
        if "location" in point:
            result.append(point)
    return result


def _brush_snapshot(context):
    sculpt = getattr(getattr(context, "tool_settings", None), "sculpt", None)
    brush = getattr(sculpt, "brush", None)
    if brush is None:
        return {}
    result = {"name": brush.name}
    for name in ("size", "unprojected_radius", "strength", "hardness",
                 "spacing", "stroke_method", "sculpt_tool"):
        if hasattr(brush, name):
            result[name] = _json_value(getattr(brush, name))
    return result


class SCULPTREC_PG_stroke(bpy.types.PropertyGroup):
    payload: StringProperty(options={'HIDDEN'})
    brush: StringProperty(options={'HIDDEN'})


class SCULPTREC_PG_recording(bpy.types.PropertyGroup):
    strokes: CollectionProperty(type=SCULPTREC_PG_stroke)
    object_name: StringProperty()


class SCULPTREC_PG_settings(bpy.types.PropertyGroup):
    recording: BoolProperty(default=False, options={'SKIP_SAVE'})
    takes: CollectionProperty(type=SCULPTREC_PG_recording)
    active_take: IntProperty(default=0, min=0)


def _active_take(settings):
    if not settings.takes:
        return None
    index = min(max(settings.active_take, 0), len(settings.takes) - 1)
    return settings.takes[index]


def _operator_pointer(operator):
    try:
        return int(operator.as_pointer())
    except Exception:
        return id(operator)


def _prime_seen(context):
    global _seen_operator_pointers
    operators = getattr(getattr(context, "window_manager", None),
                        "operators", ())
    _seen_operator_pointers = {_operator_pointer(op) for op in operators}


def _capture_completed(context):
    """Capture unseen completed sculpt operators; return number appended."""
    scene = getattr(context, "scene", None)
    settings = getattr(scene, "sculpt_stroke_recorder", None)
    if settings is None or not settings.recording:
        return 0
    take = _active_take(settings)
    if take is None:
        return 0
    captured = 0
    operators = getattr(getattr(context, "window_manager", None),
                        "operators", ())
    for operator in operators:
        pointer = _operator_pointer(operator)
        if pointer in _seen_operator_pointers:
            continue
        _seen_operator_pointers.add(pointer)
        data = serialize_stroke_operator(operator)
        if data is None:
            continue
        item = take.strokes.add()
        item.payload = json.dumps(data, separators=(",", ":"))
        item.brush = json.dumps(_brush_snapshot(context),
                                separators=(",", ":"))
        captured += 1
    return captured


def _record_timer():
    try:
        settings = getattr(bpy.context.scene, "sculpt_stroke_recorder", None)
        if settings is None or not settings.recording:
            return None
        _capture_completed(bpy.context)
        return _TIMER_INTERVAL
    except Exception:
        print("[sculpt_stroke_recorder] capture failed:")
        traceback.print_exc()
        return _TIMER_INTERVAL


def _ensure_timer():
    if not bpy.app.timers.is_registered(_record_timer):
        bpy.app.timers.register(_record_timer,
                                first_interval=_TIMER_INTERVAL,
                                persistent=False)


class SCULPTREC_OT_record_toggle(bpy.types.Operator):
    bl_idname = "sculpt_recorder.record_toggle"
    bl_label = "Stop Recording"
    bl_description = "Start or stop capturing completed native sculpt strokes"

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    def execute(self, context):
        settings = context.scene.sculpt_stroke_recorder
        if settings.recording:
            _capture_completed(context)
            settings.recording = False
            self.report({'INFO'}, "Sculpt stroke recording stopped")
            return {'FINISHED'}
        obj = context.active_object
        if obj is None or obj.type != 'MESH' or obj.mode != 'SCULPT':
            self.report({'WARNING'}, "Enter Sculpt Mode on a mesh first")
            return {'CANCELLED'}
        take = settings.takes.add()
        take.name = "Take %03d" % len(settings.takes)
        take.object_name = obj.name
        settings.active_take = len(settings.takes) - 1
        _prime_seen(context)
        settings.recording = True
        _ensure_timer()
        self.report({'INFO'}, "Recording native sculpt strokes")
        return {'FINISHED'}


class SCULPTREC_OT_replay(bpy.types.Operator):
    bl_idname = "sculpt_recorder.replay"
    bl_label = "Replay Take"
    bl_description = "Replay the selected take with the current sculpt brush"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "sculpt_stroke_recorder", None)
        obj = context.active_object
        return (settings is not None and not settings.recording
                and _active_take(settings) is not None
                and obj is not None and obj.type == 'MESH'
                and obj.mode == 'SCULPT')

    def execute(self, context):
        take = _active_take(context.scene.sculpt_stroke_recorder)
        if take is None or not take.strokes:
            self.report({'WARNING'}, "Selected take contains no strokes")
            return {'CANCELLED'}
        replayed = 0
        try:
            for stroke in take.strokes:
                data = json.loads(stroke.payload)
                samples = replay_samples(data)
                if not samples:
                    continue
                bpy.ops.sculpt.brush_stroke(
                    stroke=samples,
                    mode=data.get("mode", "NORMAL"),
                    override_location=True,
                    ignore_background_click=True)
                replayed += 1
        except RuntimeError as exc:
            self.report({'ERROR'}, "Replay failed: %s" % exc)
            return {'CANCELLED'}
        self.report({'INFO'}, "Replayed %d sculpt stroke(s)" % replayed)
        return {'FINISHED'}


class SCULPTREC_OT_remove_take(bpy.types.Operator):
    bl_idname = "sculpt_recorder.remove_take"
    bl_label = "Remove Take"
    bl_options = {'UNDO'}

    def execute(self, context):
        settings = context.scene.sculpt_stroke_recorder
        if settings.recording or not settings.takes:
            return {'CANCELLED'}
        index = min(settings.active_take, len(settings.takes) - 1)
        settings.takes.remove(index)
        settings.active_take = max(0, min(index, len(settings.takes) - 1))
        return {'FINISHED'}


class SCULPTREC_UL_takes(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False, icon='REC')
        row.label(text="%d strokes" % len(item.strokes))


class VIEW3D_PT_sculpt_stroke_recorder(bpy.types.Panel):
    bl_label = "Sculpt Stroke Recorder"
    bl_idname = "VIEW3D_PT_sculpt_stroke_recorder"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Sculpt Recorder"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.sculpt_stroke_recorder
        obj = context.active_object
        sculpt_ready = obj is not None and obj.type == 'MESH' \
            and obj.mode == 'SCULPT'
        button = layout.row()
        button.scale_y = 1.35
        button.operator(
            SCULPTREC_OT_record_toggle.bl_idname,
            text="Stop Recording" if settings.recording else "Record New Take",
            icon='PAUSE' if settings.recording else 'REC')
        if not sculpt_ready:
            layout.label(text="Enter Sculpt Mode to record or replay",
                         icon='INFO')
        layout.template_list(
            SCULPTREC_UL_takes.__name__, "", settings, "takes",
            settings, "active_take", rows=4)
        take = _active_take(settings)
        row = layout.row(align=True)
        row.enabled = sculpt_ready and not settings.recording \
            and take is not None and bool(take.strokes)
        row.operator(SCULPTREC_OT_replay.bl_idname, icon='PLAY')
        remove = row.row(align=True)
        remove.enabled = take is not None and not settings.recording
        remove.operator(SCULPTREC_OT_remove_take.bl_idname,
                        text="", icon='TRASH')
        if take is not None:
            layout.label(text="Recorded on: %s" % (
                take.object_name or "unknown object"))
            layout.label(text="Replay uses the current sculpt brush")


_CLASSES = (
    SCULPTREC_PG_stroke,
    SCULPTREC_PG_recording,
    SCULPTREC_PG_settings,
    SCULPTREC_OT_record_toggle,
    SCULPTREC_OT_replay,
    SCULPTREC_OT_remove_take,
    SCULPTREC_UL_takes,
    VIEW3D_PT_sculpt_stroke_recorder,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sculpt_stroke_recorder = bpy.props.PointerProperty(
        type=SCULPTREC_PG_settings)


def unregister():
    settings = getattr(getattr(bpy.context, "scene", None),
                       "sculpt_stroke_recorder", None)
    if settings is not None:
        settings.recording = False
    if bpy.app.timers.is_registered(_record_timer):
        bpy.app.timers.unregister(_record_timer)
    if hasattr(bpy.types.Scene, "sculpt_stroke_recorder"):
        del bpy.types.Scene.sculpt_stroke_recorder
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)

