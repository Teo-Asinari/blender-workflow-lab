# Dropbox Blend Pause

Stops the Dropbox desktop client while a watched sculpture folder is open in
Blender, then starts Dropbox again when you open a file outside that folder,
disable the add-on, or quit Blender.

Dropbox has **no per-folder pause** that keeps the cloud copy. The official
“ignore” marker removes the folder from dropbox.com, so this add-on does not
use it. It quits `Dropbox.exe` instead.

Default watch folder:

`C:\Users\Teo Asinari\Dropbox\My Sculptures\2026-02-16_#1`

## Install

Copy `dropbox_blend_pause/` into Blender’s `scripts/addons/` directory, or
Install from Disk a packaged ZIP. Enable **Dropbox Blend Pause**.

## Use

1. Enable the add-on.
2. Open a `.blend` inside the watched folder. Dropbox should quit.
3. Save textures as usual (no Dropbox lock on the rename).
4. Open a different project or quit Blender. Dropbox should start again.

Change the folder under **Preferences > Add-ons > Dropbox Blend Pause**, or in
the **Dropbox Pause** N-panel. **Start Dropbox** forces a resume if you still
have the project open.

## Limits

- Windows only. Other OS: no-op.
- Pausing is **global** (the whole Dropbox client), not one folder.
- If two Blender windows have the watched project open, closing the first
  will start Dropbox while the second is still saving.
- Dropbox may take a few seconds to appear in the tray after restart.
