# SPDX-License-Identifier: GPL-2.0-or-later
"""Record completed native Sculpt, Texture Paint, and Impasto GPU strokes.

Native strokes are observed in Blender's operator history after
``SCULPT_OT_brush_stroke`` or ``PAINT_OT_image_paint`` completes. Impasto GPU
strokes are received from ``impasto.gpu_engine`` at pen-up. The recorder never
replaces those operators. Recordings are stored on the Scene and survive
saving the .blend.
"""

import json
import traceback

import bpy
from bpy.props import (BoolProperty, CollectionProperty, IntProperty,
                       StringProperty)


bl_info = {
    "name": "Stroke Recorder",
    "author": "Teo Asinari",
    "version": (0, 3, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Stroke Recorder",
    "description": "Record native sculpt, texture-paint, and Impasto GPU "
                   "strokes and replay them from their samples",
    "category": "Paint",
}


_TIMER_INTERVAL = 0.05
_seen_operator_pointers = set()

_SAMPLE_FIELDS = (
    "name", "location", "mouse", "mouse_event", "pressure", "size",
    "x_tilt", "y_tilt", "time", "is_start",
)

KIND_SCULPT = "sculpt"
KIND_PAINT = "texture_paint"
KIND_IMPASTO = "impasto_gpu"

_OPERATOR_KINDS = {
    "SCULPT_OT_brush_stroke": KIND_SCULPT,
    "PAINT_OT_image_paint": KIND_PAINT,
}

MODE_IMPASTO = "IMPASTO_GPU"

_MODE_KIND = {
    "SCULPT": KIND_SCULPT,
    "TEXTURE_PAINT": KIND_PAINT,
    MODE_IMPASTO: KIND_IMPASTO,
}

_KIND_MODE = {
    KIND_SCULPT: "SCULPT",
    KIND_PAINT: "TEXTURE_PAINT",
    KIND_IMPASTO: MODE_IMPASTO,
}

_KIND_LABEL = {
    KIND_SCULPT: "sculpt",
    KIND_PAINT: "texture paint",
    KIND_IMPASTO: "Impasto GPU",
}


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
    """Serialize one completed native sculpt or texture-paint operator.

    Kept independent of PropertyGroups so headless tests can exercise the
    capture contract with light-weight stand-ins.
    """
    kind = _OPERATOR_KINDS.get(_operator_identifier(operator))
    if kind is None:
        return None
    properties = getattr(operator, "properties", operator)
    samples = []
    for point in getattr(properties, "stroke", ()):
        sample = {}
        for field in _SAMPLE_FIELDS:
            if hasattr(point, field):
                sample[field] = _json_value(getattr(point, field))
        if "location" in sample or "mouse" in sample:
            samples.append(sample)
    if not samples:
        return None
    default_toggle = "NONE" if kind == KIND_SCULPT else "None"
    return {
        "schema": 1,
        "kind": kind,
        "mode": str(getattr(properties, "mode", "NORMAL")),
        "brush_toggle": str(getattr(properties, "brush_toggle", default_toggle)),
        "pen_flip": bool(getattr(properties, "pen_flip", False)),
        "samples": samples,
    }


def replay_samples(payload):
    """Return sanitized dictionaries accepted by the native stroke operators."""
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
        if "location" in point or "mouse" in point:
            result.append(point)
    return result


def payload_kind(payload):
    """Return the stroke kind stored on a payload; old takes are sculpt."""
    data = json.loads(payload) if isinstance(payload, str) else payload
    kind = data.get("kind", KIND_SCULPT)
    if kind in _KIND_MODE:
        return kind
    return KIND_SCULPT


def _impasto_engine():
    try:
        from impasto import gpu_engine
    except ImportError:
        return None
    return gpu_engine


def _impasto_session_active():
    engine = _impasto_engine()
    return bool(engine is not None and engine.session_active())


def _bind_impasto_listener():
    engine = _impasto_engine()
    if engine is not None:
        engine.add_stroke_listener(_on_impasto_stroke)


def _unbind_impasto_listener():
    engine = _impasto_engine()
    if engine is not None:
        engine.remove_stroke_listener(_on_impasto_stroke)


def _on_impasto_stroke(payload):
    """Append a completed Impasto GPU stroke to the active recording."""
    if not payload or payload.get("kind") != KIND_IMPASTO:
        return
    scene = getattr(bpy.context, "scene", None)
    settings = getattr(scene, "sculpt_stroke_recorder", None)
    if settings is None or not settings.recording:
        return
    take = _active_take(settings)
    if take is None or _take_kind(take) != KIND_IMPASTO:
        return
    item = take.strokes.add()
    item.payload = json.dumps(payload, separators=(",", ":"))
    item.brush = json.dumps({
        "kind": KIND_IMPASTO,
        "brush_mode": payload.get("brush_mode"),
        "radius": payload.get("radius"),
        "hardness": payload.get("hardness"),
        "opacity": payload.get("opacity"),
        "channel_keys": payload.get("channel_keys"),
        "target_channel_keys": payload.get("target_channel_keys"),
        "layer_uid": payload.get("layer_uid"),
    }, separators=(",", ":"))


def _brush_snapshot(context, kind):
    if kind == KIND_IMPASTO:
        return {"kind": KIND_IMPASTO}
    tool_settings = getattr(context, "tool_settings", None)
    if tool_settings is None:
        return {}
    if kind == KIND_PAINT:
        paint = getattr(tool_settings, "image_paint", None)
        brush = getattr(paint, "brush", None)
    else:
        sculpt = getattr(tool_settings, "sculpt", None)
        brush = getattr(sculpt, "brush", None)
    if brush is None:
        return {}
    result = {"name": brush.name, "kind": kind}
    for name in ("size", "unprojected_radius", "strength", "hardness",
                 "spacing", "stroke_method", "sculpt_tool", "image_tool",
                 "blend"):
        if hasattr(brush, name):
            result[name] = _json_value(getattr(brush, name))
    return result


class SCULPTREC_PG_stroke(bpy.types.PropertyGroup):
    payload: StringProperty(options={'HIDDEN'})
    brush: StringProperty(options={'HIDDEN'})


class SCULPTREC_PG_recording(bpy.types.PropertyGroup):
    strokes: CollectionProperty(type=SCULPTREC_PG_stroke)
    object_name: StringProperty()
    source_mode: StringProperty(default="SCULPT")


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


def _mesh_record_kind(obj):
    if obj is None or obj.type != 'MESH':
        return None
    kind = _MODE_KIND.get(obj.mode)
    if kind is not None:
        return kind
    if _impasto_session_active():
        return KIND_IMPASTO
    return None


def _take_kind(take):
    if take is None:
        return KIND_SCULPT
    return _MODE_KIND.get(take.source_mode, KIND_SCULPT)


def _capture_completed(context):
    """Capture unseen completed native stroke operators; return count."""
    scene = getattr(context, "scene", None)
    settings = getattr(scene, "sculpt_stroke_recorder", None)
    if settings is None or not settings.recording:
        return 0
    take = _active_take(settings)
    if take is None:
        return 0
    wanted = _take_kind(take)
    if wanted == KIND_IMPASTO:
        _bind_impasto_listener()
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
        if data is None or data.get("kind") != wanted:
            continue
        item = take.strokes.add()
        item.payload = json.dumps(data, separators=(",", ":"))
        item.brush = json.dumps(_brush_snapshot(context, wanted),
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


def replay_impasto_payload(data):
    """Feed one recorded Impasto GPU stroke into the active session."""
    engine = _impasto_engine()
    if engine is None or not engine.session_active():
        raise RuntimeError("Start Impasto GPU painting before replaying")
    samples = replay_samples(data)
    if not samples:
        return False
    snapshot = engine.stroke_settings_snapshot()
    settings = snapshot[1] if snapshot else {}
    radius = float(settings.get("radius") or data.get("radius") or 40.0)
    started = False
    for point in samples:
        mouse = point.get("mouse") or (0.0, 0.0)
        x, y = float(mouse[0]), float(mouse[1])
        pressure = point.get("pressure", 1.0)
        extras = {
            "x_tilt": point.get("x_tilt", 0.0),
            "y_tilt": point.get("y_tilt", 0.0),
            "time": point.get("time"),
        }
        if not started or point.get("is_start"):
            if started:
                engine.end_stroke()
            engine.begin_stroke(x, y, pressure, extras)
            started = True
        else:
            engine.move_stroke(x, y, pressure, radius, extras)
    if started:
        engine.end_stroke()
    return True


def _invoke_replay(data):
    samples = replay_samples(data)
    if not samples:
        return False
    kind = payload_kind(data)
    if kind == KIND_IMPASTO:
        return replay_impasto_payload(data)
    kwargs = {
        "stroke": samples,
        "mode": data.get("mode", "NORMAL"),
        "pen_flip": bool(data.get("pen_flip", False)),
    }
    stored_toggle = data.get("brush_toggle")
    if stored_toggle:
        kwargs["brush_toggle"] = stored_toggle
    if kind == KIND_PAINT:
        bpy.ops.paint.image_paint(**kwargs)
    else:
        kwargs["override_location"] = True
        kwargs["ignore_background_click"] = True
        bpy.ops.sculpt.brush_stroke(**kwargs)
    return True


def _take_matches_context(take, obj):
    if take is None or obj is None or obj.type != 'MESH':
        return False
    kind = _take_kind(take)
    if kind == KIND_IMPASTO:
        return _impasto_session_active()
    return obj.mode == (take.source_mode or "SCULPT")


class SCULPTREC_OT_record_toggle(bpy.types.Operator):
    bl_idname = "sculpt_recorder.record_toggle"
    bl_label = "Stop Recording"
    bl_description = (
        "Start or stop capturing completed native sculpt, texture-paint, "
        "or Impasto GPU strokes")

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    def execute(self, context):
        settings = context.scene.sculpt_stroke_recorder
        if settings.recording:
            _capture_completed(context)
            settings.recording = False
            self.report({'INFO'}, "Stroke recording stopped")
            return {'FINISHED'}
        obj = context.active_object
        kind = _mesh_record_kind(obj)
        if kind is None:
            self.report(
                {'WARNING'},
                "Enter Sculpt or Texture Paint, or start Impasto GPU painting")
            return {'CANCELLED'}
        take = settings.takes.add()
        take.name = "Take %03d" % len(settings.takes)
        take.object_name = obj.name
        take.source_mode = _KIND_MODE[kind]
        settings.active_take = len(settings.takes) - 1
        _prime_seen(context)
        settings.recording = True
        if kind == KIND_IMPASTO:
            _bind_impasto_listener()
        _ensure_timer()
        self.report({'INFO'}, "Recording %s strokes" % _KIND_LABEL[kind])
        return {'FINISHED'}


class SCULPTREC_OT_replay(bpy.types.Operator):
    bl_idname = "sculpt_recorder.replay"
    bl_label = "Replay Take"
    bl_description = (
        "Replay the selected take with the current brush of the matching mode")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "sculpt_stroke_recorder", None)
        obj = context.active_object
        take = _active_take(settings) if settings is not None else None
        if (settings is None or settings.recording or take is None
                or not take.strokes):
            return False
        return _take_matches_context(take, obj)

    def execute(self, context):
        take = _active_take(context.scene.sculpt_stroke_recorder)
        if take is None or not take.strokes:
            self.report({'WARNING'}, "Selected take contains no strokes")
            return {'CANCELLED'}
        if _take_kind(take) == KIND_IMPASTO:
            return self._start_impasto_replay(context, take)
        replayed = 0
        try:
            for stroke in take.strokes:
                data = json.loads(stroke.payload)
                if _invoke_replay(data):
                    replayed += 1
        except RuntimeError as exc:
            self.report({'ERROR'}, "Replay failed: %s" % exc)
            return {'CANCELLED'}
        kind = _take_kind(take)
        self.report({'INFO'}, "Replayed %d %s stroke(s)"
                    % (replayed, _KIND_LABEL[kind]))
        return {'FINISHED'}

    def _start_impasto_replay(self, context, take):
        if not _impasto_session_active():
            self.report({'WARNING'},
                        "Start Impasto GPU painting to replay this take")
            return {'CANCELLED'}
        self._replay_index = 0
        self._replayed = 0
        self._timer = None
        self._region = getattr(context, "region", None)
        wm = context.window_manager
        win = context.window if context.window is not None else (
            wm.windows[0] if wm.windows else None)
        if win is None:
            self.report({'ERROR'}, "No window available for Impasto replay")
            return {'CANCELLED'}
        self._timer = wm.event_timer_add(_TIMER_INTERVAL, window=win)
        wm.modal_handler_add(self)
        self.report({'INFO'}, "Replaying %d Impasto GPU stroke(s)"
                    % len(take.strokes))
        return {'RUNNING_MODAL'}

    def _stop_impasto_replay(self, context):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None

    def modal(self, context, event):
        if event.type == 'ESC' and event.value == 'PRESS':
            self._stop_impasto_replay(context)
            self.report({'INFO'}, "Impasto GPU replay cancelled after %d"
                        % self._replayed)
            return {'CANCELLED'}
        if event.type not in {'TIMER', 'ESC'}:
            return {'RUNNING_MODAL'}
        take = _active_take(context.scene.sculpt_stroke_recorder)
        engine = _impasto_engine()
        if take is None or engine is None or not engine.session_active():
            self._stop_impasto_replay(context)
            self.report({'ERROR'}, "Impasto GPU session ended during replay")
            return {'CANCELLED'}
        if engine.stroke_active() or engine.busy():
            if self._region is not None:
                self._region.tag_redraw()
            return {'RUNNING_MODAL'}
        if self._replay_index >= len(take.strokes):
            self._stop_impasto_replay(context)
            self.report({'INFO'}, "Replayed %d Impasto GPU stroke(s)"
                        % self._replayed)
            return {'FINISHED'}
        try:
            data = json.loads(take.strokes[self._replay_index].payload)
            if replay_impasto_payload(data):
                self._replayed += 1
        except (RuntimeError, ValueError, TypeError) as exc:
            self._stop_impasto_replay(context)
            self.report({'ERROR'}, "Replay failed: %s" % exc)
            return {'CANCELLED'}
        self._replay_index += 1
        if self._region is not None:
            self._region.tag_redraw()
        return {'RUNNING_MODAL'}


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
        kind = _MODE_KIND.get(item.source_mode, KIND_SCULPT)
        row.label(text="%d %s" % (len(item.strokes), _KIND_LABEL[kind]))


class VIEW3D_PT_sculpt_stroke_recorder(bpy.types.Panel):
    bl_label = "Stroke Recorder"
    bl_idname = "VIEW3D_PT_sculpt_stroke_recorder"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Stroke Recorder"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.sculpt_stroke_recorder
        obj = context.active_object
        kind = _mesh_record_kind(obj)
        button = layout.row()
        button.scale_y = 1.35
        button.operator(
            SCULPTREC_OT_record_toggle.bl_idname,
            text="Stop Recording" if settings.recording else "Record New Take",
            icon='PAUSE' if settings.recording else 'REC')
        if kind is None:
            layout.label(
                text="Sculpt, Texture Paint, or Impasto GPU to record",
                icon='INFO')
        layout.template_list(
            SCULPTREC_UL_takes.__name__, "", settings, "takes",
            settings, "active_take", rows=4)
        take = _active_take(settings)
        matching = _take_matches_context(take, obj)
        row = layout.row(align=True)
        row.enabled = matching and not settings.recording \
            and take is not None and bool(take.strokes)
        row.operator(SCULPTREC_OT_replay.bl_idname, icon='PLAY')
        remove = row.row(align=True)
        remove.enabled = take is not None and not settings.recording
        remove.operator(SCULPTREC_OT_remove_take.bl_idname,
                        text="", icon='TRASH')
        if take is not None:
            take_kind = _take_kind(take)
            layout.label(text="Recorded on: %s (%s)" % (
                take.object_name or "unknown object",
                _KIND_LABEL[take_kind]))
            if not matching:
                if take_kind == KIND_IMPASTO:
                    layout.label(
                        text="Start Impasto GPU painting to replay",
                        icon='INFO')
                else:
                    layout.label(
                        text="Enter %s mode to replay this take"
                        % _KIND_LABEL[take_kind],
                        icon='INFO')
            else:
                layout.label(text="Replay uses the current %s brush"
                             % _KIND_LABEL[take_kind])


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
    _bind_impasto_listener()


def unregister():
    settings = getattr(getattr(bpy.context, "scene", None),
                       "sculpt_stroke_recorder", None)
    if settings is not None:
        settings.recording = False
    _unbind_impasto_listener()
    if bpy.app.timers.is_registered(_record_timer):
        bpy.app.timers.unregister(_record_timer)
    if hasattr(bpy.types.Scene, "sculpt_stroke_recorder"):
        del bpy.types.Scene.sculpt_stroke_recorder
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
