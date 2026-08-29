# Dropbox Blend Pause

Stops the Dropbox desktop client while a watched sculpture folder is open in
Blender, then starts Dropbox again when you open a file outside that folder,
disable the add-on, or quit Blender.

Dropbox has **no per-folder pause** that keeps the cloud copy. The official
“ignore” marker removes the folder from dropbox.com, so this add-on does not
use it. It quits `Dropbox.exe` instead. Do not submit this add-on to
extensions.blender.org: that platform forbids tampering with other software.

Default watch folder:

`C:\Users\Teo Asinari\Dropbox\My Sculptures\2026-02-16_#1`

## Install

Download `dropbox_blend_pause-0.1.1.zip` from the
[v2026.08.28 release](https://github.com/Teo-Asinari/blender-workflow-lab/releases/tag/v2026.08.28)
and install it with **Edit > Preferences > Add-ons > Install from Disk**,
then enable **Dropbox Blend Pause**. Copying `dropbox_blend_pause/` into
`scripts/addons/` remains a developer option.

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

## Support

Report issues at
[github.com/Teo-Asinari/blender-workflow-lab/issues](https://github.com/Teo-Asinari/blender-workflow-lab/issues).

## License

GPL-3.0-or-later.
