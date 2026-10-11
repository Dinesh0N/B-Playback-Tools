# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Playback Tools Team

bl_info = {
    "name": "B Playback Tools",
    "author": "Dinesh007",
    "version": (1, 0, 1),
    "blender": (4, 2, 0),
    "location": "Timeline & Sequencer Playback Controls, Playback Menu, Sidebar",
    "description": "Variable timeline and VSE preview playback speed (0.25x - 16x) with pitch-preserved audio",
    "category": "Sequencer",
}

import os
import time
import aud
import bpy
import bl_ui.space_time as _st
import bl_ui.space_sequencer as _ss
import bl_ui.space_dopesheet as _sd
from bpy.props import (
    FloatProperty,
    IntProperty,
    BoolProperty,
    PointerProperty,
)
from bpy.types import (
    Operator,
    Panel,
    PropertyGroup,
    AddonPreferences,
)

# -------------------------------------------------------------------------
# Global State & Sound Cache
# -------------------------------------------------------------------------

_SOUND_CACHE = {}
_ACTIVE_PLAYBACK_OP = None
_PREV_AUDIO_SCRUB = None
_ORIG_ST_PLAYBACK_CONTROLS = getattr(_st, "playback_controls", None)
_ORIG_SS_PLAYBACK_CONTROLS = getattr(_ss, "playback_controls", None)
_ORIG_SD_PLAYBACK_CONTROLS = getattr(_sd, "playback_controls", None)



def get_cached_sound(filepath):
    """Return a cached aud.Sound object or load and cache it."""
    if not filepath or not os.path.isfile(filepath):
        return None
    sound = _SOUND_CACHE.get(filepath)
    if sound is None:
        try:
            sound = aud.Sound.file(filepath)
            _SOUND_CACHE[filepath] = sound
        except Exception as err:
            print(f"[Playback Speed] Error loading audio '{filepath}': {err}")
            return None
    return sound


def clear_sound_cache():
    """Clear memory cache of audio files."""
    _SOUND_CACHE.clear()


# -------------------------------------------------------------------------
# Settings (Scene Property Group)
# -------------------------------------------------------------------------

PRESET_VALUES = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0]


def on_speed_update(self, context):
    """Called when the speed property is modified."""
    global _ACTIVE_PLAYBACK_OP
    if _ACTIVE_PLAYBACK_OP and _ACTIVE_PLAYBACK_OP.is_running:
        _ACTIVE_PLAYBACK_OP.playback_speed = self.speed
        _ACTIVE_PLAYBACK_OP.restart_audio_at_current_frame(context)



class PlaybackSpeedSettings(PropertyGroup):
    speed: FloatProperty(
        name="Speed",
        description="Playback speed multiplier (e.g. 1.5 = 1.5x speed)",
        default=1.0,
        min=0.1,
        max=16.0,
        soft_min=0.25,
        soft_max=4.0,
        step=10,
        precision=2,
        update=on_speed_update,
    )

    preserve_pitch: BoolProperty(
        name="Preserve Pitch",
        description="Maintain natural voice pitch (no chipmunk effect) during high speed playback using Rubberband",
        default=True,
        update=on_speed_update,
    )

    volume: FloatProperty(
        name="Volume",
        default=1.0,
        min=0.0,
        max=1.0,
    )

    is_playing: BoolProperty(
        name="Is Playing",
        description="Whether custom speed playback is currently active",
        default=False,
    )

    loop_playback: BoolProperty(
        name="Loop Playback",
        default=True,
    )


# -------------------------------------------------------------------------
# Addon Preferences
# -------------------------------------------------------------------------

class PlaybackSpeedPreferences(AddonPreferences):
    bl_idname = __name__

    enable_bracket_keys: BoolProperty(
        name="Enable [ / ] Preset Step Hotkeys",
        description="Press [ or ] to step through speed presets (0.5x, 1x, 1.5x, 2x, etc.)",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.prop(self, "enable_bracket_keys")

        layout.separator()
        box = layout.box()
        box.label(text="Playback Controls & Behavior:", icon='PLAY')
        col = box.column(align=True)
        col.label(text=" • Play Button ▶ & Spacebar : Play at selected speed (pitch-preserved audio)")
        col.label(text=" • Reverse Button ◀ & Ctrl+Shift+Space : Play in reverse at selected speed")
        col.label(text=" • Spacebar or Pause Button ⏸ : Pauses playback cleanly")
        col.label(text=" • At 1.00x Normal Speed : 100% standard default Blender playback")
        col.label(text=" • [ / ] : Next / Previous Speed Preset")




# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

def get_settings(context):
    """Retrieve settings from active scene."""
    scene = context.scene if context and context.scene else bpy.context.scene
    return scene.playback_speed_settings


def tag_redraw_all(context):
    """Tag timeline and sequencer areas for redraw."""
    if not context or not context.screen:
        return
    for area in context.screen.areas:
        if area.type in {'DOPESHEET_EDITOR', 'SEQUENCE_EDITOR', 'VIEW_3D', 'TIMELINE'}:
            area.tag_redraw()


def is_strip_channel_muted(se, strip):
    """Check if the channel containing the strip is muted (including parent meta strips)."""
    if hasattr(se, "channels") and hasattr(strip, "channel"):
        try:
            ch_idx = strip.channel
            if 0 <= ch_idx < len(se.channels):
                if bool(se.channels[ch_idx].mute):
                    return True
        except Exception:
            pass
    try:
        parent_func = getattr(strip, "parent_meta", None)
        if callable(parent_func):
            parent = parent_func()
            if parent:
                return bool(parent.mute) or is_strip_channel_muted(se, parent)
    except Exception:
        pass
    return False


def get_strip_timing(strip):
    """Return (start_frame, end_frame, trim_offset) compatible with Blender 4.2+ & 5.2."""
    try:
        start = int(getattr(strip, "frame_final_start", getattr(strip, "content_start", 0)))
        end = int(getattr(strip, "frame_final_end", getattr(strip, "content_end", start)))
        offset = float(getattr(strip, "frame_offset_start", getattr(strip, "content_trim_start", 0.0)))
        return start, end, offset
    except Exception:
        return 0, 0, 0.0


def get_sound_strips_fingerprint(scene):
    """Generate a lightweight signature of all sound strips to detect movement or edits."""
    if not scene or not scene.sequence_editor:
        return ()

    se = scene.sequence_editor
    strips = getattr(se, "strips_all", getattr(se, "strips", getattr(se, "sequences", [])))

    fp = []
    for strip in strips:
        try:
            if strip.type != 'SOUND':
                continue
            s_start, s_end, s_offset = get_strip_timing(strip)
            sound = getattr(strip, "sound", None)
            fpath = sound.filepath if sound else ""
            fp.append((
                strip.as_pointer(),
                s_start,
                s_end,
                s_offset,
                bool(strip.mute),
                is_strip_channel_muted(se, strip),
                round(getattr(strip, "volume", 1.0), 2),
                fpath,
            ))
        except (ReferenceError, Exception):
            continue

    return tuple(fp)


def collect_active_sound_strips(scene, start_frame, end_frame):
    """Find all active, unmuted sound strips overlapping [start_frame, end_frame]."""
    if not scene.sequence_editor:
        return []

    se = scene.sequence_editor
    strips = getattr(se, "strips_all", getattr(se, "strips", getattr(se, "sequences", [])))

    active_strips = []
    for strip in strips:
        try:
            if strip.type != 'SOUND':
                continue
            if strip.mute:
                continue
            if not strip.sound or not strip.sound.filepath:
                continue

            if is_strip_channel_muted(se, strip):
                continue

            s_start, s_end, s_offset = get_strip_timing(strip)
            if s_end > start_frame and s_start < end_frame:
                abs_path = bpy.path.abspath(strip.sound.filepath)
                if os.path.isfile(abs_path):
                    active_strips.append((strip, abs_path, s_start, s_end, s_offset))
        except (ReferenceError, Exception):
            continue

    return active_strips


# -------------------------------------------------------------------------
# Playback Engine & Modal Operator (With Live Playhead Scrubbing Support)
# -------------------------------------------------------------------------

def is_scrubbing_operator_active(context):
    """Detect if Blender's native timeline/sequencer scrub operator is currently active."""
    try:
        win = getattr(context, "window", None)
        if not win:
            return False
        modal_ops = getattr(win, "modal_operators", None)
        if not modal_ops:
            return False
        for op in list(modal_ops):
            try:
                idname = getattr(op, "bl_idname", "").lower()
                name = getattr(op, "name", "").lower()
                rna_id = getattr(getattr(op, "rna_type", None), "identifier", "").lower()
                if any(k in idname or k in name or k in rna_id for k in ("change_frame", "scrub")):
                    return True
            except (ReferenceError, Exception):
                continue
    except Exception:
        pass
    return False


def is_transform_operator_active(context):
    """Detect if Blender's native transform/translate/seq_slide/slip operator is currently active."""
    try:
        win = getattr(context, "window", None)
        if not win:
            return False
        modal_ops = getattr(win, "modal_operators", None)
        if not modal_ops:
            return False
        for op in list(modal_ops):
            try:
                idname = getattr(op, "bl_idname", "").lower()
                name = getattr(op, "name", "").lower()
                rna_id = getattr(getattr(op, "rna_type", None), "identifier", "").lower()
                if any(k in idname or k in name or k in rna_id for k in ("transform", "translate", "seq_slide", "slip", "duplicate_move")):
                    return True
            except (ReferenceError, Exception):
                continue
    except Exception:
        pass
    return False


class PLAYBACK_OT_speed_play_pause(Operator):
    """Play or Pause playback using the active playback speed"""
    bl_idname = "playback.speed_play_pause"
    bl_label = "Play / Pause"
    bl_options = {'INTERNAL'}

    reverse: BoolProperty(name="Reverse", default=False)

    _timer = None
    is_running = False
    handle = None
    aud_device = None
    start_frame = 1
    start_wall_time = 0.0
    playback_speed = 1.0
    fps = 24.0
    range_start = 1
    range_end = 250
    has_sound = False
    last_set_frame = 1
    mouse_pressed = False
    is_scrubbing = False
    was_scrub_op_active = False
    last_scrub_time = 0.0
    was_transform_op_active = False
    last_sound_strips_fingerprint = ()
    last_fp_check_time = 0.0

    def finish_scrub_and_resume(self, context):
        """Called when playhead scrubbing finishes to resume playback from the scrubbed frame."""
        self.is_scrubbing = False
        self.was_scrub_op_active = False
        self.mouse_pressed = False
        self.restart_audio_at_current_frame(context)
        tag_redraw_all(context)

    def modal(self, context, event):
        global _ACTIVE_PLAYBACK_OP

        # 1. Stop playback cleanly on ESC or SPACE
        if event.type in {'ESC'}:
            if is_transform_operator_active(context):
                return {'PASS_THROUGH'}
            self.stop_playback(context)
            return {'FINISHED'}

        if event.type == 'SPACE' and event.value == 'PRESS':
            self.stop_playback(context)
            return {'FINISHED'}

        # 2. Track mouse click/drag for interactive timeline playhead scrubbing & strip manipulation
        if event.type in {'LEFTMOUSE', 'RIGHTMOUSE'}:
            if event.value == 'PRESS':
                self.mouse_pressed = True
                self.last_scrub_time = time.perf_counter()
            elif event.value == 'RELEASE':
                self.mouse_pressed = False
                if self.is_scrubbing:
                    self.finish_scrub_and_resume(context)
                    return {'PASS_THROUGH'}
                elif not self.reverse:
                    # User released mouse outside playhead scrub (e.g. finished moving/dropping a strip)
                    self.was_transform_op_active = False
                    scene = context.scene
                    current_fp = get_sound_strips_fingerprint(scene)
                    if current_fp != self.last_sound_strips_fingerprint:
                        self.last_sound_strips_fingerprint = current_fp
                        self.restart_audio_at_current_frame(context)
                        tag_redraw_all(context)

        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            if self.is_scrubbing and self.mouse_pressed:
                self.last_scrub_time = time.perf_counter()

        # 3. Timer event drives playback & handles scrubbing
        if event.type == 'TIMER':
            if not self.is_running:
                return {'FINISHED'}

            scene = context.scene
            now = time.perf_counter()
            current_frame = scene.frame_current

            if scene.use_preview_range:
                self.range_start = scene.frame_preview_start
                self.range_end = scene.frame_preview_end
            else:
                self.range_start = scene.frame_start
                self.range_end = scene.frame_end

            scrub_op_active = is_scrubbing_operator_active(context)

            # A. User is actively scrubbing via Blender's native scrub operator
            if scrub_op_active:
                self.is_scrubbing = True
                self.was_scrub_op_active = True
                self.last_scrub_time = now
                self.last_set_frame = current_frame
                # Pause audio while scrubbing so it doesn't stutter or play stale frames
                if self.handle and self.handle.status:
                    try:
                        self.handle.pause()
                    except Exception:
                        pass
                return {'PASS_THROUGH'}

            # B. Frame moved externally without change_frame operator (single click, arrow keys, etc.)
            if current_frame != self.last_set_frame:
                self.is_scrubbing = True
                self.last_scrub_time = now
                self.last_set_frame = current_frame
                # Pause audio while scrubbing so it doesn't stutter or play stale frames
                if self.handle and self.handle.status:
                    try:
                        self.handle.pause()
                    except Exception:
                        pass
                return {'PASS_THROUGH'}

            # C. Scrubbing was active: detect release/completion and resume immediately!
            if self.is_scrubbing:
                # If native scrub operator was active and just finished, OR
                # if 80ms has elapsed since the playhead stopped moving (for single click/jumps):
                # The user released the cursor! Immediately resume playback from this frame!
                if self.was_scrub_op_active or (now - self.last_scrub_time > 0.08):
                    self.finish_scrub_and_resume(context)
                    now = time.perf_counter()
                else:
                    return {'PASS_THROUGH'}

            # D. Strip movement & modification detection (adapts audio immediately even while dragging/moving strips)
            if not self.reverse:
                transform_op_active = is_transform_operator_active(context)
                just_finished_transform = self.was_transform_op_active and not transform_op_active
                self.was_transform_op_active = transform_op_active

                # Check interval: 0.06s while actively dragging/transforming (smooth real-time audio adaptation without stutter),
                # 0.04s during normal playback, or immediately (0s delay) when transform finishes
                check_interval = 0.06 if transform_op_active else 0.04
                if just_finished_transform or (now - self.last_fp_check_time >= check_interval):
                    self.last_fp_check_time = now
                    current_fp = get_sound_strips_fingerprint(scene)
                    if current_fp != self.last_sound_strips_fingerprint:
                        self.last_sound_strips_fingerprint = current_fp
                        self.restart_audio_at_current_frame(context)
                        tag_redraw_all(context)
                        now = time.perf_counter()

            # E. Normal playback progression (runs immediately if not scrubbing)
            speed = self.playback_speed
            elapsed_wall_seconds = max(0.0, now - self.start_wall_time)
            timeline_elapsed = elapsed_wall_seconds * speed

            if not self.reverse:
                target_frame = int(round(self.start_frame + timeline_elapsed * self.fps))
                if target_frame > self.range_end:
                    settings = get_settings(context)
                    if settings.loop_playback:
                        self.start_frame = self.range_start
                        self.start_wall_time = now
                        scene.frame_set(self.range_start)
                        self.last_set_frame = self.range_start
                        self.restart_audio_at_current_frame(context)
                        return {'PASS_THROUGH'}
                    else:
                        scene.frame_set(self.range_end)
                        self.stop_playback(context)
                        return {'FINISHED'}
            else:
                target_frame = int(round(self.start_frame - timeline_elapsed * self.fps))
                if target_frame < self.range_start:
                    settings = get_settings(context)
                    if settings.loop_playback:
                        self.start_frame = self.range_end
                        self.start_wall_time = now
                        scene.frame_set(self.range_end)
                        self.last_set_frame = self.range_end
                        self.restart_audio_at_current_frame(context)
                        return {'PASS_THROUGH'}
                    else:
                        scene.frame_set(self.range_start)
                        self.stop_playback(context)
                        return {'FINISHED'}

            if target_frame != scene.frame_current:
                self.last_set_frame = target_frame
                scene.frame_set(target_frame)
                tag_redraw_all(context)

        return {'PASS_THROUGH'}

    def start_audio_stream(self, context, current_frame):
        """Construct the stretched audio stream from current frame to end."""
        scene = context.scene
        settings = get_settings(context)
        speed = settings.speed
        self.playback_speed = speed
        self.fps = scene.render.fps / max(1e-6, scene.render.fps_base)

        if scene.use_preview_range:
            self.range_start = scene.frame_preview_start
            self.range_end = scene.frame_preview_end
        else:
            self.range_start = scene.frame_start
            self.range_end = scene.frame_end

        if self.handle:
            try:
                self.handle.stop()
            except Exception:
                pass
            self.handle = None

        active_strips = collect_active_sound_strips(scene, current_frame, self.range_end)
        if not active_strips:
            self.has_sound = False
            return

        seq = aud.Sequence(channels=2, rate=48000.0, fps=self.fps, muted=False)
        added_count = 0

        for strip, abs_path, s_start, s_end, s_offset in active_strips:
            snd = get_cached_sound(abs_path)
            if not snd:
                continue

            vol = getattr(strip, "volume", 1.0)
            if abs(vol - 1.0) > 0.01:
                snd = snd.volume(max(0.0, vol))

            if s_start < current_frame:
                elapsed_frames = current_frame - s_start
                skip = (s_offset + elapsed_frames) / self.fps
                begin = 0.0
                end = (s_end - current_frame) / self.fps
            else:
                begin = (s_start - current_frame) / self.fps
                skip = s_offset / self.fps
                end = (s_end - current_frame) / self.fps

            if end > begin:
                seq.add(snd, begin, end, max(0.0, skip))
                added_count += 1

        if added_count == 0:
            self.has_sound = False
            return

        self.has_sound = True

        try:
            if settings.preserve_pitch:
                time_stretch = 1.0 / max(0.05, speed)
                sound_to_play = seq.timeStretchPitchScale(
                    time_stretch, 1.0, aud.STRETCHER_QUALITY_FAST, True
                )
            else:
                sound_to_play = seq.pitch(speed)

            if not self.aud_device:
                self.aud_device = aud.Device()

            self.handle = self.aud_device.play(sound_to_play)
        except Exception as err:
            print(f"[Playback Speed] Error starting audio stream: {err}")
            self.has_sound = False

    def restart_audio_at_current_frame(self, context):
        """Seamlessly restart audio stream when speed changes, strips move, or scrubbing occurs."""
        scene = context.scene if context and context.scene else bpy.context.scene
        self.start_frame = scene.frame_current
        self.last_set_frame = self.start_frame
        self.last_sound_strips_fingerprint = get_sound_strips_fingerprint(scene)
        self.last_fp_check_time = time.perf_counter()
        if not self.reverse:
            self.start_audio_stream(context, self.start_frame)
        self.start_wall_time = time.perf_counter()

    def execute(self, context):
        global _ACTIVE_PLAYBACK_OP, _PREV_AUDIO_SCRUB
        settings = get_settings(context)
        scene = context.scene

        # If our custom playback is running, stop it
        if _ACTIVE_PLAYBACK_OP and _ACTIVE_PLAYBACK_OP.is_running:
            _ACTIVE_PLAYBACK_OP.stop_playback(context)
            settings.is_playing = False
            tag_redraw_all(context)
            return {'FINISHED'}

        # If standard Blender animation is running, stop it cleanly
        if context.screen and context.screen.is_animation_playing:
            bpy.ops.screen.animation_play()
            return {'FINISHED'}

        # If speed is 1.0x (default), use standard Blender native playback cleanly
        if abs(settings.speed - 1.0) < 0.001:
            bpy.ops.screen.animation_play(reverse=self.reverse)
            return {'FINISHED'}


        # Start pitch-preserved audio-synced player (all speeds)
        self.is_running = True
        _ACTIVE_PLAYBACK_OP = self
        settings.is_playing = True
        self.mouse_pressed = False
        self.is_scrubbing = False
        self.was_scrub_op_active = False
        self.last_scrub_time = 0.0
        self.was_transform_op_active = False
        self.last_sound_strips_fingerprint = get_sound_strips_fingerprint(scene)
        self.last_fp_check_time = time.perf_counter()

        _PREV_AUDIO_SCRUB = scene.use_audio_scrub
        scene.use_audio_scrub = False

        if scene.use_preview_range:
            self.range_start = scene.frame_preview_start
            self.range_end = scene.frame_preview_end
        else:
            self.range_start = scene.frame_start
            self.range_end = scene.frame_end

        if not self.reverse and scene.frame_current >= self.range_end:
            scene.frame_set(self.range_start)
        elif self.reverse and scene.frame_current <= self.range_start:
            scene.frame_set(self.range_end)

        self.start_frame = scene.frame_current
        self.last_set_frame = self.start_frame
        self.start_wall_time = time.perf_counter()
        self.fps = scene.render.fps / max(1e-6, scene.render.fps_base)
        self.playback_speed = settings.speed

        if not self.reverse:
            self.start_audio_stream(context, self.start_frame)

        wm = context.window_manager
        timer_rate = 1.0 / max(60.0, self.fps * 2.0)
        self._timer = wm.event_timer_add(timer_rate, window=context.window)
        wm.modal_handler_add(self)

        tag_redraw_all(context)
        return {'RUNNING_MODAL'}

    def stop_playback(self, context):
        """Stop playback and clean up resources."""
        global _ACTIVE_PLAYBACK_OP, _PREV_AUDIO_SCRUB
        self.is_running = False
        self.mouse_pressed = False
        self.is_scrubbing = False
        self.was_scrub_op_active = False
        self.last_scrub_time = 0.0
        self.was_transform_op_active = False
        self.last_sound_strips_fingerprint = ()

        if self.handle:
            try:
                self.handle.stop()
            except Exception:
                pass
            self.handle = None

        wm = getattr(context, "window_manager", None) or getattr(bpy.context, "window_manager", None)
        if self._timer and wm:
            try:
                wm.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None

        scene = getattr(context, "scene", None) or getattr(bpy.context, "scene", None)
        if _PREV_AUDIO_SCRUB is not None and scene:
            try:
                scene.use_audio_scrub = _PREV_AUDIO_SCRUB
            except Exception:
                pass
            _PREV_AUDIO_SCRUB = None

        try:
            settings = get_settings(context)
            settings.is_playing = False
        except Exception:
            pass
        _ACTIVE_PLAYBACK_OP = None
        tag_redraw_all(context)


# -------------------------------------------------------------------------
# Speed Control Operators (Presets, Reset, Step, Shuttle)
# -------------------------------------------------------------------------

class PLAYBACK_OT_speed_preset_set(Operator):
    """Set playback speed to a specific preset multiplier"""
    bl_idname = "playback.speed_preset_set"
    bl_label = "Set Playback Speed Preset"
    bl_options = {'INTERNAL'}

    speed: FloatProperty(name="Speed", default=1.0)

    def execute(self, context):
        settings = get_settings(context)
        settings.speed = self.speed
        tag_redraw_all(context)
        return {'FINISHED'}


class PLAYBACK_OT_speed_reset(Operator):
    """Reset playback speed to 1.00x"""
    bl_idname = "playback.speed_reset"
    bl_label = "Reset Speed to 1.00x"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = get_settings(context)
        settings.speed = 1.0
        tag_redraw_all(context)
        return {'FINISHED'}


class PLAYBACK_OT_speed_step(Operator):
    """Step to the next or previous speed preset"""
    bl_idname = "playback.speed_step"
    bl_label = "Step Playback Speed"
    bl_options = {'INTERNAL'}

    delta: IntProperty(name="Delta", default=1)

    def execute(self, context):
        settings = get_settings(context)
        current = settings.speed

        closest_idx = 0
        min_diff = float("inf")
        for i, val in enumerate(PRESET_VALUES):
            diff = abs(val - current)
            if diff < min_diff:
                min_diff = diff
                closest_idx = i

        new_idx = max(0, min(len(PRESET_VALUES) - 1, closest_idx + self.delta))
        settings.speed = PRESET_VALUES[new_idx]
        tag_redraw_all(context)
        return {'FINISHED'}




# -------------------------------------------------------------------------
# UI: Popover & Sidebar Panels (Cleaned per user request)
# -------------------------------------------------------------------------

class PLAYBACK_PT_speed_popover(Panel):
    """Playback Speed Popover Menu"""
    bl_label = "Playback Speed"
    bl_space_type = 'SEQUENCE_EDITOR'
    bl_region_type = 'HEADER'
    bl_ui_units_x = 13

    def draw(self, context):
        layout = self.layout
        settings = get_settings(context)

        # 1. Speed Presets Grid (YouTube Player Style)
        box = layout.box()
        box.label(text="Speed Presets:", icon='TIME')

        # Row 1: 0.25x to 1.0x
        row1 = box.row(align=True)
        for val in [0.25, 0.5, 0.75]:
            op = row1.operator("playback.speed_preset_set", text=f"{val:g}x", depress=(abs(settings.speed - val) < 0.01))
            op.speed = val
        op = row1.operator("playback.speed_preset_set", text="1.0x", depress=(abs(settings.speed - 1.0) < 0.01))
        op.speed = 1.0

        # Row 2: 1.25x to 2.0x
        row2 = box.row(align=True)
        for val in [1.25, 1.5, 1.75, 2.0]:
            op = row2.operator("playback.speed_preset_set", text=f"{val:g}x", depress=(abs(settings.speed - val) < 0.01))
            op.speed = val

        # Row 3: 2.5x to 4.0x
        row3 = box.row(align=True)
        for val in [2.5, 3.0, 4.0]:
            op = row3.operator("playback.speed_preset_set", text=f"{val:g}x", depress=(abs(settings.speed - val) < 0.01))
            op.speed = val

        layout.separator()

        # 2. Custom Speed: Direct Number Input (NO SLIDER)
        row = layout.row(align=True)
        row.prop(settings, "speed", text="Custom", slider=False)

        layout.separator()

        # 3. Audio Settings (ONLY Preserve Pitch, no volume, no loop)
        box_audio = layout.box()
        box_audio.label(text="Audio Settings:", icon='SPEAKER')
        box_audio.prop(
            settings,
            "preserve_pitch",
            text="Preserve Pitch (Rubberband)",
            icon='CHECKBOX_HLT' if settings.preserve_pitch else 'CHECKBOX_DEHLT',
        )

        # 4. Shortcuts hint
        layout.separator()
        col = layout.column(align=True)
        col.scale_y = 0.8
        col.label(text="[ / ]: Speed Presets", icon='INFO')


class SEQUENCER_PT_playback_speed(Panel):
    """Playback Speed Panel in VSE Sidebar"""
    bl_label = "Playback Speed"
    bl_space_type = 'SEQUENCE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Playback"

    def draw(self, context):
        layout = self.layout
        settings = get_settings(context)

        # Presets Grid
        box = layout.box()
        box.label(text="Presets:", icon='TIME')
        row1 = box.row(align=True)
        for val in [0.25, 0.5, 0.75, 1.0]:
            op = row1.operator("playback.speed_preset_set", text=f"{val:g}x", depress=(abs(settings.speed - val) < 0.01))
            op.speed = val

        row2 = box.row(align=True)
        for val in [1.25, 1.5, 1.75, 2.0]:
            op = row2.operator("playback.speed_preset_set", text=f"{val:g}x", depress=(abs(settings.speed - val) < 0.01))
            op.speed = val

        row3 = box.row(align=True)
        for val in [2.5, 3.0, 4.0]:
            op = row3.operator("playback.speed_preset_set", text=f"{val:g}x", depress=(abs(settings.speed - val) < 0.01))
            op.speed = val

        layout.separator()

        # Custom Number Input
        row = layout.row(align=True)
        row.prop(settings, "speed", text="Custom", slider=False)

        layout.separator()

        # Audio Settings
        box_audio = layout.box()
        box_audio.label(text="Audio:", icon='SPEAKER')
        box_audio.prop(settings, "preserve_pitch", text="Preserve Pitch")


# -------------------------------------------------------------------------
# Playback Controls Integration (Blender Play Button uses Playback Speed)
# -------------------------------------------------------------------------

def draw_playback_speed_widget(layout, context):
    """Draw the compact [ 4x ⌵ ][ ↶ ] widget in playback controls footer."""
    settings = get_settings(context)
    speed_text = f"{settings.speed:g}x" if abs(settings.speed - round(settings.speed, 2)) < 0.001 else f"{settings.speed:.2f}x"

    row = layout.row(align=True)
    row.popover(
        panel="PLAYBACK_PT_speed_popover",
        text=speed_text,
    )
    sub = row.row(align=True)
    sub.active = abs(settings.speed - 1.0) > 0.001
    sub.operator("playback.speed_reset", text="", icon='LOOP_BACK')


def draw_in_dopesheet_playback_controls(self, context):
    """Append speed widget to Timeline/Dopesheet playback controls footer."""
    draw_playback_speed_widget(self.layout, context)


def draw_in_sequencer_playback_controls(self, context):
    """Append speed widget to Sequencer playback controls footer."""
    draw_playback_speed_widget(self.layout, context)


# -------------------------------------------------------------------------
# Playback Controls Integration (Direct Transport Bar Play Button Binding)
# -------------------------------------------------------------------------

def custom_playback_controls(layout, context):
    """
    Playback Controls override:
    - When speed != 1.0x (or custom speed playback is active):
        Blender's forward Play button in Playback Controls routes directly to
        playback.speed_play_pause with pitch-preserved audio.
    - When speed == 1.0x:
        Delegates 100% to Blender's native playback_controls drawing function.
    - All other buttons (Reverse Play ◀, Frame Jump |◀ ▶|, Keyframe Jump, Snapping):
        Always 100% native Blender controls.
    """
    st = context.space_data
    is_sequencer = st.type == 'SEQUENCE_EDITOR'
    is_timeline = st.type == 'DOPESHEET_EDITOR' and getattr(st, "mode", None) == 'TIMELINE'

    scene = context.scene if not is_sequencer else context.sequencer_scene
    if not scene:
        return

    settings = getattr(scene, "playback_speed_settings", None)
    speed = settings.speed if settings else 1.0
    use_speed_player = abs(speed - 1.0) > 0.001

    # At 1.0x normal speed and when speed player is idle, use 100% default native controls
    if not use_speed_player and not (settings and settings.is_playing):
        if _ORIG_ST_PLAYBACK_CONTROLS:
            _ORIG_ST_PLAYBACK_CONTROLS(layout, context)
            return

    tool_settings = scene.tool_settings if scene else None
    screen = context.screen

    layout.popover(
        panel="TIME_PT_playback",
        text="Playback",
    )

    if tool_settings and not is_timeline:
        icon_keytype = f'KEYTYPE_{tool_settings.keyframe_type}_VEC'
        ctx_wm = getattr(getattr(_st, "i18n_contexts", None), "id_windowmanager", None)
        if ctx_wm is not None:
            layout.popover(
                panel="TIME_PT_keyframing_settings",
                text_ctxt=ctx_wm,
                icon=icon_keytype,
            )
        else:
            layout.popover(
                panel="TIME_PT_keyframing_settings",
                icon=icon_keytype,
            )

    if is_sequencer:
        layout.prop(context.workspace, "use_scene_time_sync")

    layout.separator_spacer()

    if tool_settings:
        row = layout.row(align=True)
        row.prop(tool_settings, "use_keyframe_insert_auto", text="", toggle=True)
        sub = row.row(align=True)
        sub.active = tool_settings.use_keyframe_insert_auto
        sub.popover(
            panel="TIME_PT_auto_keyframing",
            text="",
        )

    row = layout.row(align=True)
    row.operator("screen.frame_jump", text="", icon='REW').end = False
    row.operator("screen.keyframe_jump", text="", icon='PREV_KEYFRAME').next = False

    is_speed_playing = settings.is_playing if settings else False

    if is_speed_playing:
        row.scale_x = 2
        row.operator("playback.speed_play_pause", text="", icon='PAUSE')
        row.scale_x = 1
    elif screen and screen.is_animation_playing:
        row.scale_x = 2
        row.operator("screen.animation_pause", text="", icon='PAUSE')
        row.scale_x = 1
    else:
        if scene and scene.sync_mode == 'AUDIO_SYNC' and context.preferences.system.audio_device == 'JACK':
            row.scale_x = 2
            if use_speed_player:
                op = row.operator("playback.speed_play_pause", text="", icon='PLAY')
                op.reverse = False
            else:
                row.operator("screen.animation_play", text="", icon='PLAY')
            row.scale_x = 1
        else:
            # Reverse play button: speed player when speed != 1.0, otherwise native
            if use_speed_player:
                op_rev = row.operator("playback.speed_play_pause", text="", icon='PLAY_REVERSE')
                op_rev.reverse = True
            else:
                row.operator("screen.animation_play", text="", icon='PLAY_REVERSE').reverse = True

            # Forward play button: pitch-preserved speed player when speed != 1.0, otherwise native
            if use_speed_player:
                op_fwd = row.operator("playback.speed_play_pause", text="", icon='PLAY')
                op_fwd.reverse = False
            else:
                row.operator("screen.animation_play", text="", icon='PLAY')


    row.operator("screen.keyframe_jump", text="", icon='NEXT_KEYFRAME').next = True
    row.operator("screen.frame_jump", text="", icon='FF').end = True

    # Time jump
    row = layout.row(align=True)
    row.operator("screen.time_jump", text="", icon='FRAME_PREV').backward = True
    row.operator("screen.time_jump", text="", icon='FRAME_NEXT').backward = False
    row.popover(panel="TIME_PT_jump", text="")

    if tool_settings:
        row = layout.row(align=True)
        row.prop(tool_settings, "use_snap_playhead", text="")
        sub = row.row(align=True)
        sub.popover(panel="TIME_PT_playhead_snapping", text="")

    layout.separator_spacer()

    if scene:
        row = layout.row()
        if scene.show_subframe:
            row.scale_x = 1.15
            row.prop(scene, "frame_float", text="")
        else:
            row.scale_x = 0.95
            row.prop(scene, "frame_current", text="")

        row = layout.row(align=True)
        row.prop(scene, "use_preview_range", text="", toggle=True)
        sub = row.row(align=True)
        sub.scale_x = 0.8
        if not scene.use_preview_range:
            sub.prop(scene, "frame_start", text="Start")
            sub.prop(scene, "frame_end", text="End")
        else:
            sub.prop(scene, "frame_preview_start", text="Start")
            sub.prop(scene, "frame_preview_end", text="End")


# -------------------------------------------------------------------------
# Keymaps
# -------------------------------------------------------------------------

_ADDON_KEYMAPS = []


def register_keymaps():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    prefs = bpy.context.preferences.addons.get(__name__)
    enable_brackets = prefs.preferences.enable_bracket_keys if prefs else True


    # 1. Sequencer Keymap
    km = kc.keymaps.new(name="Sequencer", space_type='SEQUENCE_EDITOR')

    if enable_brackets:
        kmi = km.keymap_items.new("playback.speed_step", 'RIGHT_BRACKET', 'PRESS')
        kmi.properties.delta = 1
        _ADDON_KEYMAPS.append((km, kmi))

        kmi = km.keymap_items.new("playback.speed_step", 'LEFT_BRACKET', 'PRESS')
        kmi.properties.delta = -1
        _ADDON_KEYMAPS.append((km, kmi))

    # Spacebar (Forward Play/Pause) & Ctrl+Shift+Space (Reverse Play/Pause)
    kmi = km.keymap_items.new("playback.speed_play_pause", 'SPACE', 'PRESS')
    kmi.properties.reverse = False
    _ADDON_KEYMAPS.append((km, kmi))

    kmi = km.keymap_items.new("playback.speed_play_pause", 'SPACE', 'PRESS', shift=True, ctrl=True)
    kmi.properties.reverse = True
    _ADDON_KEYMAPS.append((km, kmi))

    # 2. Timeline / Dopesheet Keymap
    km_dope = kc.keymaps.new(name="Dopesheet", space_type='DOPESHEET_EDITOR')

    kmi = km_dope.keymap_items.new("playback.speed_play_pause", 'SPACE', 'PRESS')
    kmi.properties.reverse = False
    _ADDON_KEYMAPS.append((km_dope, kmi))

    kmi = km_dope.keymap_items.new("playback.speed_play_pause", 'SPACE', 'PRESS', shift=True, ctrl=True)
    kmi.properties.reverse = True
    _ADDON_KEYMAPS.append((km_dope, kmi))

    if enable_brackets:
        kmi = km_dope.keymap_items.new("playback.speed_step", 'RIGHT_BRACKET', 'PRESS')
        kmi.properties.delta = 1
        _ADDON_KEYMAPS.append((km_dope, kmi))

        kmi = km_dope.keymap_items.new("playback.speed_step", 'LEFT_BRACKET', 'PRESS')
        kmi.properties.delta = -1
        _ADDON_KEYMAPS.append((km_dope, kmi))

    # 3. Frames Keymap (Global Timeline Playback)
    km_frames = kc.keymaps.new(name="Frames", space_type='EMPTY')

    kmi = km_frames.keymap_items.new("playback.speed_play_pause", 'SPACE', 'PRESS')
    kmi.properties.reverse = False
    _ADDON_KEYMAPS.append((km_frames, kmi))

    kmi = km_frames.keymap_items.new("playback.speed_play_pause", 'SPACE', 'PRESS', shift=True, ctrl=True)
    kmi.properties.reverse = True
    _ADDON_KEYMAPS.append((km_frames, kmi))




def unregister_keymaps():
    for km, kmi in _ADDON_KEYMAPS:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _ADDON_KEYMAPS.clear()


# -------------------------------------------------------------------------
# Registration
# -------------------------------------------------------------------------

classes = (
    PlaybackSpeedSettings,
    PlaybackSpeedPreferences,
    PLAYBACK_OT_speed_play_pause,
    PLAYBACK_OT_speed_preset_set,
    PLAYBACK_OT_speed_reset,
    PLAYBACK_OT_speed_step,
    PLAYBACK_PT_speed_popover,
    SEQUENCER_PT_playback_speed,
)



def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.playback_speed_settings = PointerProperty(type=PlaybackSpeedSettings)

    # Patch Playback Controls in timeline, sequencer, and dopesheet
    if hasattr(_st, "playback_controls"):
        _st.playback_controls = custom_playback_controls
    if hasattr(_ss, "playback_controls"):
        _ss.playback_controls = custom_playback_controls
    if hasattr(_sd, "playback_controls"):
        _sd.playback_controls = custom_playback_controls

    # Append speed widget to footer controls
    if hasattr(bpy.types, "DOPESHEET_HT_playback_controls"):
        bpy.types.DOPESHEET_HT_playback_controls.append(draw_in_dopesheet_playback_controls)

    if hasattr(bpy.types, "SEQUENCER_HT_playback_controls"):
        bpy.types.SEQUENCER_HT_playback_controls.append(draw_in_sequencer_playback_controls)

    register_keymaps()


def unregister():
    global _ACTIVE_PLAYBACK_OP
    if _ACTIVE_PLAYBACK_OP and _ACTIVE_PLAYBACK_OP.is_running:
        try:
            _ACTIVE_PLAYBACK_OP.stop_playback(bpy.context)
        except Exception:
            pass

    clear_sound_cache()
    unregister_keymaps()

    # Restore original Playback Controls functions
    if _ORIG_ST_PLAYBACK_CONTROLS and hasattr(_st, "playback_controls"):
        _st.playback_controls = _ORIG_ST_PLAYBACK_CONTROLS
    if _ORIG_SS_PLAYBACK_CONTROLS and hasattr(_ss, "playback_controls"):
        _ss.playback_controls = _ORIG_SS_PLAYBACK_CONTROLS
    if _ORIG_SD_PLAYBACK_CONTROLS and hasattr(_sd, "playback_controls"):
        _sd.playback_controls = _ORIG_SD_PLAYBACK_CONTROLS

    if hasattr(bpy.types, "DOPESHEET_HT_playback_controls"):
        try:
            bpy.types.DOPESHEET_HT_playback_controls.remove(draw_in_dopesheet_playback_controls)
        except Exception:
            pass

    if hasattr(bpy.types, "SEQUENCER_HT_playback_controls"):
        try:
            bpy.types.SEQUENCER_HT_playback_controls.remove(draw_in_sequencer_playback_controls)
        except Exception:
            pass

    if hasattr(bpy.types.Scene, "playback_speed_settings"):
        del bpy.types.Scene.playback_speed_settings

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


if __name__ == "__main__":
    register()