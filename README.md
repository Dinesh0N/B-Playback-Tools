# Playback Speed Controls for Blender 5.2 (Timeline & VSE)

A fast, video-editor-friendly playback speed control extension for Blender 5.2, designed specifically for long-format video editing and tutorial workflows.

---

## Features

- **Blender Playback Controls, Spacebar & Reverse Play Integration**:
  - **Forward Play `▶` & Spacebar**: When a custom speed is selected (e.g. `1.5x`, `2x`, `4x`), clicking Blender's Play button `▶` or pressing **Spacebar** plays at that speed with pitch-preserved audio.
  - **Reverse Play `◀` & Ctrl+Shift+Space**: Clicking Blender's Reverse button `◀` or pressing **Ctrl+Shift+Space** plays in reverse at the selected speed.
  - **Clean Pause**: Pressing **Spacebar** or clicking the **Pause button `⏸`** immediately pauses playback.
  - **100% Default Native Behavior at 1.00x**: When speed is `1.00x`, all buttons (`▶`, `◀`, Spacebar) run with 100% standard native Blender playback.


- **Variable Playback Speed Widget ("That Option")**:
  - Compact `[ 1.5x ⌵ ][ ↶ ]` widget in the timeline and sequencer playback controls bar.
  - **Instant Presets**: `0.25x`, `0.5x`, `0.75x`, `1.0x (Normal)`, `1.25x`, `1.5x`, `1.75x`, `2.0x`, `2.5x`, `3.0x`, `4.0x`. limit (`0.1x`to `16x`)
  - **Custom Speed Number Input**: Clean numeric entry field to type exact speed values (e.g. `1.25`, `1.75`, `2.5`, etc.).
  - **One-Click Reset `↶`**: Instantly return to `1.00x` standard speed.

- **Pitch-Preserved Audio (VLC / YouTube Style)**:
  - Powered by Blender 5.2's native **Audaspace Rubberband** formant preservation engine (`aud.Sound.timeStretchPitchScale`).
  - Speeds up or slows down voice tutorials without high-pitched "chipmunk" distortion, preserving clarity and speech comprehension even at 1.5×, 2×, or 4×.
  - Option to toggle off pitch preservation for classic tape-scrub effect.

- **Hotkeys**:
  - **`]` & `[`**: Step forward or backward through speed presets.
  - **Spacebar**: Standard Blender play/pause (cleanly pauses speed player when active).

## License
GPL-3.0-or-later
