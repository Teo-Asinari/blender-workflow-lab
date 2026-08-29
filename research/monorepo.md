# Keep blender-workflow-lab as a monorepo

Decision: do **not** split the seven add-ons into separate GitHub repositories.
User-facing packaging is already per add-on; a repo split would not make the
Install-from-Disk ZIPs, `blender_manifest.toml` files, or a future
[Blender Extensions](https://extensions.blender.org/) listing any cleaner.

## What is already independent

Each add-on already has its own version, manifest, LICENSE, README, and ZIP
(`<id>-<version>.zip` from `scripts/package_addons.py`). Blender and the
extensions platform consume one ZIP, not this tree. GitHub Release
[v2026.08.28](https://github.com/Teo-Asinari/blender-workflow-lab/releases/tag/v2026.08.28)
attaches those ZIPs as separate assets.

There is no shared Python package to extract. Cross-add-on similarity is
copy-and-adapt (Calipers’ overlay follows UV Island Overlay’s pattern) rather
than a common library.

## What a split would actually change

Development, not distribution. One commit can retarget every README, bump GPL
metadata, rebuild every ZIP, and run Kiln’s Impasto interop test
(`addons/kiln/tests/test_impasto_interop.py`) against the same tree. Kiln talks
to Impasto at runtime by image/node names (`Kiln Bake Target`,
`import_normal_baseline`); that coupling is optional when only one add-on is
enabled, but the test belongs in one checkout.

Seven repos would mean seven issue trackers, seven CI files, seven release
tags, and a PR stack for every cross-cutting change. Impasto still moves
weekly; Calipers, Kiln, and UV Island Overlay do not. Putting them on a
strict “one repo = one product” rule makes Impasto releases noisier for the
stable tools and makes the stable tools slower to patch.

## When a later extract *is* reasonable

- **Dropbox Blend Pause** is a personal Windows workaround. It must not go to
  extensions.blender.org (that platform forbids tampering with other
  software). It can stay here, or move to a private repo, but it does not
  belong in a public product catalog next to Calipers.
- **Calipers, Kiln, UV Island Overlay** could get their own public repos *if
  and when* they are submitted as standalone products and a dedicated issues
  URL is worth the overhead. That is a publishing choice. They can also keep
  living here, with listing website/support URLs pointed at
  `addons/<name>/` or a labeled issue filter.
- **Impasto, Sculpt Stroke Recorder, Seam Path Tool** stay in the lab until
  they are products you want people to file issues against in isolation.
  Impasto is most of the tree and still has an open roadmap.

## If the GitHub home page feels like a junk drawer

Fix that without splitting:

- Label issues per add-on.
- Tag releases per add-on (`calipers-v1.2.1`, `impasto-v0.15.32`) instead of
  one date tag that dumps every ZIP.
- Keep this repository as the development home.

The source of truth remains `blender-workflow-lab`. Extract a repo only for a
specific add-on that is going public as its own product, or to stop publishing
Dropbox Blend Pause.
