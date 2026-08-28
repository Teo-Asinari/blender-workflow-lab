# Seam Path Tool

Interactive UV seam marking for Blender. Click points on a mesh in Edit
Mode; the shortest path between consecutive clicks is marked as a UV
seam, with a live preview of the candidate path under the mouse.

Inspired by seam-marking workflows found in specialized 3D tools such as
3DCoat. This project is independent and is not affiliated with or
endorsed by Pilgway.

Tested against **Blender 5.1.2**.

## Relationship to Blender's built-in shortest-path seams

Blender already ships point-to-point seam marking: in Edit Mode with edge
select, `mesh.shortest_path_pick` (Ctrl+Click) supports an `edge_mode`
tag option (`SEAM`, among others — adjustable in the operator redo panel
after a pick), so vanilla Blender covers *no-preview* two-point seam
tagging.

What this add-on adds on top:

- **A dedicated stay-in-tool workflow** — keep clicking to chain seam
  segments without re-invoking anything.
- **Live path preview** — the candidate path from the last anchor to the
  vertex under your mouse is drawn as an overlay *before* you click, so
  you see exactly what will be marked.
- **Erase mode** — Ctrl+Click (or the operator's Erase toggle) clears
  seams along the path instead of marking.
- **Per-segment undo inside the tool** (Backspace) plus one Blender undo
  step per committed segment.

Why reimplement Dijkstra in Python? Blender's C shortest-path code is not
exposed to Python as a queryable API — `mesh.shortest_path_pick` /
`mesh.shortest_path_select` act only by side effect (changing the
selection/tags), with no way to ask "what *would* the path be?". A live
preview requires computing the path ourselves, so `core.py` implements it
over the bmesh edge graph.

## Install

Download `seam_path_tool-1.4.0.zip` from
[GitHub Releases](https://github.com/Teo-Asinari/blender-workflow-lab/releases)
and install it with **Edit > Preferences > Add-ons > Install from Disk**,
then enable **Seam Path Tool**. Copying or symlinking the `seam_path_tool`
folder into `scripts/addons/` remains a developer option (on Windows:
`%APPDATA%\Blender Foundation\Blender\5.1\scripts\addons\`).

## Optional dependency: scipy

Everything works without scipy. Installing it makes the per-click path
tree on large meshes a single C-speed solve (`scipy.sparse.csgraph.dijkstra`):
measured on a 300k-vert grid, ~45 ms per click instead of ~0.8 s of
background fill — during which a far hover no longer has to fall back to
a (sometimes slow) A* query. Without scipy, `Topology` mode still gets a
C-speed tree from a vectorized numpy BFS (numpy ships with Blender);
`Length` mode falls back to the incremental pure-Python fill, i.e. exactly
the 1.3.0 behaviour. Small meshes (< 25k verts) always use the
pure-Python fill — it already completes within the first couple of
timer slices there, so scipy buys nothing.

Blender's bundled Python does **not** ship scipy, and a plain
`pip install scipy` puts it in the Windows *user* site-packages, which
Blender deliberately ignores. Install it into Blender's user
`addons/modules` directory (on Blender's `sys.path`, no admin rights
needed) — Windows `cmd`/PowerShell, adjust the version numbers to yours:

```
"C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pip install --target "%APPDATA%\Blender Foundation\Blender\5.1\scripts\addons\modules" scipy
```

(From WSL, the same command works with the `/mnt/c/...` path to
`python.exe` and the expanded `C:\Users\<you>\AppData\Roaming\...` target;
verified against Blender 5.1.2 / scipy 1.18.0.) The add-on probes for
scipy lazily — no restart-time cost, and the one-off `import scipy`
(~0.7 s) happens in the background on the first click's timer tick, never
inside a click.

## Usage

Both operators live in **Edit Mode > Edge menu** (Ctrl+E):

### Seam Path (Interactive) — the stay-in-tool workflow

Also bound to **Ctrl+Alt+E** in mesh Edit Mode (verified unbound in the
stock 5.1 keymap; change it under Preferences > Keymap if it clashes with
another add-on).

While the tool is active, an **on-screen help panel** sits at the
bottom-left of the viewport (semi-transparent dark box): the top line
lists the controls, the bottom line shows live status — anchor count,
segment count, path mode, and an `[ERASE]` tag while Ctrl is held. It
scales with your UI scale preference.

- **Snap marker** — at all times, the vertex the cursor snaps to is
  marked with a white dot inside a cyan ring (constant screen size), so
  you always know exactly which vertex a click will hit — including
  before the first click.
- **LMB** — click a vertex to place the first anchor; every following
  click commits a seam segment along the shortest path from the previous
  anchor. Picking is **occlusion-aware**: a ray is cast under the mouse
  and the pick snaps to the hit surface's vertex nearest the cursor on
  screen, so vertices on the far side of the mesh never win. Clicking
  just off the silhouette falls back to the nearest on-screen vertex
  (within ~60 px) that is actually visible. With **X-ray shading** on,
  picking goes through the mesh (Blender's own selection convention).
  Hidden faces neither snap nor occlude.
- **Move mouse** — live preview (orange polyline) of the path that the
  next click would commit.
- **Anchors & committed segments** — anchors are drawn as large green
  points (with a dark outline); segments committed in this session are
  drawn as red polylines (erase-commits in muted grey), clearly distinct
  from the orange live preview and much more visible than the seam
  overlay alone.
- **Ctrl+LMB** — erase: clears seams along the path instead of marking.
  Erase paths tie-break **along existing seams**, so retracing a marked
  segment (in either direction) erases exactly its edges even on meshes
  where many equal-length paths exist. Erasing over a segment committed
  this session removes its red overlay (no grey line stacked on top);
  a grey line is shown only where a *pre-existing* seam was erased.
  (With the operator's *Erase* property enabled, plain clicks erase and
  Ctrl+clicks mark.)
- **Backspace** — undo the last committed segment (edges that were seams
  *before* that segment stay seams). Its red overlay disappears with it.
- **MMB / scroll / numpad views** — navigate as usual; the tool stays
  active.
- **Enter / RMB / Esc** — finish (all overlays are removed).

Each committed segment pushes one undo step, so Ctrl+Z after leaving the
tool steps back segment by segment.

Operator options (redo panel / menu):

- **Path Mode** — `Length` (geometric shortest path, default) or
  `Topology` (fewest edges).
- **Erase** — invert mark/erase behaviour for plain clicks.

### Mark Seam Path (2 Verts) — non-modal

Select exactly two vertices (or click one, Shift+click the other so they
are the last two entries in the selection history) and run
`Edge menu > Mark Seam Path (2 Verts)`. Options: Path Mode
(Length/Topology) and Clear. This operator is also the headless-testable
entry point (`bpy.ops.mesh.seam_path_mark`).

## Performance

Measured on a 300k-vert grid, Blender 5.1.2 (see
`tests/profile_commit.py`):

- **Committing a segment (click) is instant** (~45 ms at 300k verts,
  ~28 ms of which is Blender's own undo push). The click commits exactly
  the edge list the preview already computed — no pathfinding happens in
  the click handler.
- **The next anchor's path tree is solved at C speed when possible
  (1.4.0).** Above ~25k verts the tree is built in one call on the best
  available backend: `scipy.sparse.csgraph.dijkstra` if scipy is
  installed (see *Optional dependency* — ~45 ms at 300k verts, both path
  modes), else a vectorized numpy BFS for `Topology` mode (~80–95 ms at
  300k verts). Cheap solves (< ~30 ms estimated) run inside the commit
  click; bigger ones run on the first timer tick ~20 ms later, so the
  click itself never hitches. The flat edge-graph arrays behind these
  backends cost ~0.6 s of pure-Python extraction at 600k edges, so they
  are built **once per tool session** (topology, hide state, and vertex
  positions cannot change while the tool runs) and only the seam flags —
  which the erase tie-break weighting reads — are patched incrementally
  after each commit/undo.
- **The pure-Python incremental fill remains** for small meshes (< 25k
  verts, where it finishes within the first couple of slices anyway),
  for `Length` mode without scipy, and as the universal fallback. A full
  single-source Dijkstra tree costs ~0.7–0.9 s in pure Python at 300k
  verts; instead of paying that inside the click (the pre-1.3.0 hitch), a
  small ~15 ms slice runs at commit time and the rest fills in ~12 ms
  slices on modal timer events (fully filled after roughly a second),
  so the viewport never blocks.
- **Hover previews** read the tree wherever the hovered vertex is already
  settled (identical result to the full tree). Hovering *far* from the
  new anchor before the tree is ready falls back to a one-off early-exit
  A*/Dijkstra query for that vertex: usually fast, but on tie-rich
  meshes a worst-case far hover can briefly cost what one full tree does
  (never more than the old per-click hitch). With a C-speed backend that
  vulnerable window shrinks from ~a second to at most one timer tick
  (except right after the *first* click of a session, which also pays
  the one-off array build on its first tick). The fallback result is
  cached per hovered vertex.
- **Backends agree where it matters.** Path *costs* are identical across
  slicer/scipy/BFS everywhere (the array weights are built from the same
  `calc_length` values and the same seam-discount multiply); among
  *equal-cost* paths a backend may pick a different tie-break — exactly
  the latitude the A* fallback always had, and harmless because a commit
  reuses the previewed edge list verbatim. Seam-preferring erase paths
  are *strict* optima by construction, so every backend retraces a
  marked segment exactly (verified per backend in
  `tests/test_backends.py`).
- The occlusion-picking BVH (~0.5 s to build at 300k faces) is built
  once per tool session, on the first mousemove — geometry cannot change
  while the tool runs, so it is never rebuilt on commits.
- `bmesh.update_edit_mesh` for a seam-flag-only change is ~0 ms
  (`loop_triangles=False, destructive=False`); the ~28 ms `ed.undo_push`
  per committed segment is Blender-side and irreducible from Python.

## Notes / limitations

- Vertex picking is occlusion-aware (mouse ray against a BVH of the
  visible faces of the edit mesh); only surfaces facing/visible to the
  camera can be picked unless the viewport is in X-ray shading. Blender's
  `obj.ray_cast` sees only the stale pre-edit evaluated mesh in Edit Mode
  (verified on 5.1.2), so the BVH is built from the live edit bmesh with
  `BVHTree.FromPolygons` over the unhidden faces.
- Preview picking is O(verts) per mousemove for the screen-space pick
  (plus a few BVH ray casts). Pathfinding is a *single-source* Dijkstra
  per committed anchor (and per mark/erase toggle, which changes the
  tie-break weighting), built incrementally as described under
  Performance; steady-state mousemove previews are answered from the
  predecessor tree at O(path length).
- Hidden vertices/edges/faces are skipped by picking, occlusion, and
  pathfinding.

## Files

- `__init__.py` — registration (`bl_info`), both operators, menu entries,
  keymap. The modal operator is a thin event shell: picking plumbing plus
  delegation to `session.SeamSession`.
- `core.py` — pure logic: resumable single-source Dijkstra
  (`DijkstraSlicer`, with optional seam-preferring tie-breaking for erase
  paths; `dijkstra_tree` is the one-shot form), tree-walk path extraction
  (`path_from_tree`), early-exit two-point queries (`astar_path`),
  `shortest_path`, `mark_seam_path`, `apply_seams`; plus the 1.4.0 array
  backends (`GraphArrays` per-session edge arrays, `ArrayTree`
  scipy/numpy-BFS whole-tree solves, `make_tree` /
  `select_tree_backend`). No UI imports; fully headless-testable.
- `session.py` — pure session state for the modal tool: anchors,
  committed segments (edges + prior seam states + mark/erase flag),
  `commit_segment` / `undo_last`, and the derived overlay model. Fully
  headless-testable.
- `picking.py` — pure occlusion geometry for vertex picking (epsilon-
  tolerant visibility test against a BVH). Fully headless-testable.
- `preview.py` — all viewport overlays: 3D (POST_VIEW — live path,
  committed segments, anchors, snap-target dot; `POLYLINE_UNIFORM_COLOR`
  with `UNIFORM_COLOR` fallback) and 2D (POST_PIXEL — snap ring, help /
  status panel via `gpu` + `blf`). Pure helpers (`compose_help_lines`,
  `circle_points_2d`) are headless-testable.
- `tests/` — headless test suite.

## Running the tests

From WSL (Blender is the Windows binary):

```bash
addons/seam_path_tool/tests/run_tests.sh
# or with an explicit binary:
addons/seam_path_tool/tests/run_tests.sh "/mnt/c/Program Files/Blender Foundation/Blender 5.1/blender.exe"
```

The wrapper runs `test_core.py` (path-finding + seam marking over
procedural meshes), `test_incremental.py` (the 1.3.0 resumable/early-exit
path machinery: sliced fills identical to one-shot trees, early-exit
paths identical to full-tree paths incl. tie-breaking, partial-tree +
A* fallback consistency, and settled-count performance sanity),
`test_backends.py` (the 1.4.0 scipy / vectorized-BFS tree backends:
cross-backend cost parity with the slicer, exact seam-preferring erase
retraces per backend, hidden-element exclusion, backend selection and
the graceful no-scipy fallback — scipy cases skip cleanly when scipy is
absent, and setting `SEAM_PATH_NO_SCIPY=1` — from WSL:
`SEAM_PATH_NO_SCIPY=1 WSLENV=SEAM_PATH_NO_SCIPY` — runs the file as if
scipy were not installed), `test_session.py` (the modal tool's commit /
erase / undo bookkeeping and overlay model, including the erase
regression, plus the pure occlusion-picking logic against a constructed
BVH), and `test_register.py` (register/unregister lifecycle + the
non-modal operator end-to-end) in `--background`, greps for the
`*_TESTS_PASSED` sentinels (Blender exits 0 even when a `--python`
script raises), and exits nonzero on any failure. `tests/profile_commit.py` is a separate
profiling harness (not run by the wrapper) that measures the commit-click
cost breakdown on a ~300k-vert grid. Only the modal event plumbing and
GPU preview cannot run headlessly; they need a quick manual check in the
GUI.
