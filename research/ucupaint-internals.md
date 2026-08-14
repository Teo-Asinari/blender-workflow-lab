# Ucupaint Internals — Architecture Study

**Purpose:** input for the design of our own layer-stack add-on. This is a study of the *internals* of Ucupaint (an open-source, GPL-3.0-or-later licensed Blender layer-painting add-on, cited here as prior art under its license). UI/UX is out of scope by design; UI is mentioned only where an architectural constraint forces a workflow behavior.

**Source studied:** `github.com/ucupumar/ucupaint`, commit `cfcd5aa5e9e4e4acabfee2a56a247df25fe9250d` (2026-07-02), version 2.4.8 (+8 commits). ~73k lines of Python across ~40 modules. Citations below are `file:line` or `file:function` at this commit.

**Version support posture:** `bl_info` claims Blender 2.80+ (`__init__.py:5`), the extensions manifest claims 4.2+ (`blender_manifest.toml`), and the code still carries live 2.79 branches. There are **691 call sites** of the version-gate helper `is_bl_newer_than()` (`common.py:54`). They track Blender alphas: there are already shims referencing 5.0, 5.1, and 5.2 behavior (see §5).

---

## 1. Core data model

**Everything hangs off a node group's tree.** The root PropertyGroup `YPaint` is registered as `bpy.types.ShaderNodeTree.yp` (`Root.py:4854`). A "Ucupaint setup" is a node group placed in the material; the group's `ShaderNodeTree` datablock carries the whole stack state, so persistence in the .blend is free (PropertyGroups on an ID datablock serialize automatically), and the setup travels with the node group when appended/linked into another file. Material-level state is minimal: `YPaintMaterialProps` only remembers the original BSDF connection so it can be restored (`Root.py:4444`).

Hierarchy of PropertyGroups:

- `YPaint` (`Root.py:4252`) — owns `channels : CollectionProperty(YPaintChannel)` (`Root.py:4266`), `layers : CollectionProperty(YLayer)` (`Root.py:4276`), `uvs : CollectionProperty(YPaintUV)` (`Root.py:4309`), `bake_targets` (`Root.py:4312`), plus global flags including the load-bearing `halt_update` / `halt_reconnect` (`Root.py:4420-4423`, see §2).
- `YPaintChannel` (`Root.py:3832`) — a *root* channel definition: `type` in {`VALUE`, `RGB`, `NORMAL`} (`Root.py:3846`), colorspace policy, alpha pairing, parallax/displacement options — and ~30 `StringProperty` node-name references (`Root.py:4163-4201`).
- `YLayer` (`Layer.py:7420`) — layer `type` (IMAGE, VCOL, COLOR, GROUP, BACKGROUND, HEMI, AO, EDGE_DETECT, procedural noises…), `group_node` reference (`Layer.py:7437`), `channels : CollectionProperty(YLayerChannel)` (`Layer.py:7435`) — **always one entry per root channel, index-aligned** — `masks : CollectionProperty(YLayerMask)` (`Layer.py:7715`), UV/transform properties, and dozens of node-name strings.
- `YLayerChannel` (`Layer.py:6746`) — per-layer-per-channel state: `enable`, `blend_type`, `intensity_value`, override sources (`Layer.py:6852`), bump/normal parameters, transition effects, and again dozens of node-name strings (`Layer.py:6916-6978`).
- `YLayerMask` (`Mask.py:2447`) with per-channel `YLayerMaskChannel` (`Mask.py:2397`) — a mask is shared across the layer, but its *application* to each channel is per-channel toggleable.

**Layer ordering and grouping is index-based, not tree-based.** Layers live in one flat collection ordered top-to-bottom; hierarchy is encoded by `parent_idx : IntProperty` per layer (`Layer.py:7585`). Consequences the code pays for:

- Moving/inserting layers requires rebuilding a name→parent map, calling `yp.layers.move()`, then remapping every layer's `parent_idx` (`Layer.py:100-170` in `add_new_layer`).
- Animation F-Curves address layers by collection index (`yp.layers[3].intensity_value`), so every reorder must rewrite F-Curve `data_path` strings by regex (`remap_layer_fcurves`, `common.py:7030`; same for channels, `swap_channel_fcurves`).
- Layer↔channel alignment is positional: adding a root channel appends a `YLayerChannel` to every layer at the same index (`create_new_yp_channel`, `Root.py:143`), and any mismatch is a corruption class serious enough to warrant a dedicated repair operator (`YFixChannelMissmatch`, `Root.py:2105`).

**One layer = one node group instance.** `add_new_layer` creates a fresh `ShaderNodeTree` per layer (`bpy.data.node_groups.new(LAYERGROUP_PREFIX + name)`, `Layer.py:176`), flags it `is_ypaint_layer_node` (`Layer.py:177`), and instantiates it as a `ShaderNodeGroup` in the root tree (`Layer.py:181`). Inside the layer tree live: the source node (image/vcol/procedural), a mapping node, per-channel linearize + blend + intensity nodes, mask group nodes, and modifier subtrees. Masks may themselves get their own subtree (`subtree.py:435`), and sources are wrapped in a subtree when the smooth-bump feature needs to sample the source 4 extra times (`subtree.py:208`, and directional copies `source_n/s/e/w`, `Layer.py:7637-7640`).

**All node references are name strings, resolved lazily.** Nodes are created through `new_node(tree, entity, prop, idname)` which appends a random 4-char suffix for uniqueness and stores `node.name` into the entity's StringProperty (`common.py:1925-1941`, `id_generator` at `common.py:1922`); lookup is `tree.nodes.get(entity.prop)` everywhere (`get_tree`, `common.py:2309`). This is deliberate: Python node references die on undo/redo/reload, while name strings survive and merely go stale-but-checkable. The cost is that *every* rebuild pass does thousands of string-keyed dictionary lookups, and every structural function starts with a wall of `nodes.get(...)` calls (`node_connections.py:1775-1806`).

## 2. Node compilation

There is no separate "compile" step — **the node graph is the runtime and the data model's mirror, kept in sync by check-and-repair passes.** The pattern, repeated in ~70 property-`update` callbacks:

1. Guard: `if yp.halt_update: return` (batch-suppression flag).
2. Structural sync: idempotent `check_*` functions create/remove/replace nodes so the graph matches current properties. Examples: `check_blend_type_nodes` (`subtree.py:2300`) swaps the blend node implementation (native Mix vs. library groups for straight-over compositing) depending on blend type, alpha pairing, and parenthood; `check_all_layer_channel_io_and_nodes` (`input_outputs.py:511`); `check_uv_nodes` (`subtree.py:1611`). These use `check_new_node` (create-if-missing, `common.py:1943`) and `replace_new_node`, returning "dirty" flags so unchanged graphs aren't touched.
3. Socket sync: `check_layer_tree_ios` (`input_outputs.py:685`) adds/removes the layer group's interface sockets.
4. Rewire: `reconnect_layer_nodes(layer)` (`node_connections.py:1775`) rebuilds all links *inside* the layer tree; `reconnect_yp_nodes(tree)` (`node_connections.py:949`) rebuilds all links in the root tree.
5. Layout: `rearrange_layer_nodes` / `rearrange_yp_nodes` (`node_arrangements.py:641,1953`) recompute every node's location purely for human inspection of the generated graph.

See `update_channel_enable` (`Layer.py:6156`) for the canonical sequence, and `update_blend_type` (`Layer.py:6251`) for the one scoping optimization present: non-normal channels reconnect only that channel index (`reconnect_layer_nodes(layer, ch_index)`).

**Incrementality is at the "repair" level, not the "diff" level.** Reconnect passes unconditionally re-walk the whole tree, but two idempotence tricks keep them from being catastrophically expensive:

- `create_link` checks whether the link already exists before creating it (`node_connections.py:3-9`), so a reconnect pass over an unchanged region produces zero datablock mutations — which also avoids needlessly dirtying the tree and triggering EEVEE shader recompiles.
- `check_new_node`/`replace_new_node` only mutate when the required node is missing or of the wrong type.

**Rebuild scope on any structural change is O(layers × channels) in the root tree** plus O(nodes-in-layer) in the touched layer. `reconnect_yp_nodes` iterates every channel, and for each channel walks every layer group node, chaining value/alpha sockets through them (`node_connections.py:1065-1359`). The developers instrument this: most update callbacks end with `print('INFO: ... is updated in X ms')` timing lines (e.g. `Layer.py:6214`, `Layer.py:6623`) — 71 `T = time.time()` instrumentation sites exist.

**The inter-layer socket protocol is wide.** Layers are chained via named group sockets per channel: `{Channel}`, `{Channel} Alpha`, and for the NORMAL channel `{Channel} Height`, `Height Alpha`, `Max Height`, `Vector Displacement` — and when "smooth bump" is enabled, four *directional* height sockets (N/S/E/W) each with alpha (`input_outputs.py:932-1109`, chained in `node_connections.py:1316-1349`). Group-type layers pass everything again with a ` Group` suffix (`node_connections.py:825-890`, `input_outputs.py:1046-1102`). A 3-channel setup with normal+smooth-bump easily puts >20 sockets on every layer group. This is the single biggest node-graph-size multiplier in the design.

**Batching:** long operations (adding a layer with masks, baking, migrations) set `yp.halt_reconnect = True` up front (`Layer.py:97`) and/or `halt_update`, build all property state, then run one reconnect/rearrange at the end. Because these are *persisted* BoolProperties on the tree, an exception mid-operation leaves them stuck on — there is a repair operator that resets them (`Root.py:2105-2112`).

**Disabled layers:** links into/out of a disabled layer's group node are broken (`node_connections.py:1203-1215`) rather than muting; an alternative "move disabled layer groups into a trash group" mechanism exists but is disabled (`group_trash_update`, `Layer.py:6551`; call commented out at `Layer.py:6599`).

**Node group library:** ~200 hand-built utility groups (blend modes like straight-over, normal processing, parallax loops, FXAA…) live in bundled `lib.blend` files, appended on demand by `get_node_tree_lib` (`common.py:2123`) and either shared or duplicated per-layer with a `_Copy` suffix when per-layer edits are needed (`common.py:2789`). Each lib tree carries a `revision` marker node; on file load the add-on re-appends lib trees and hot-patches outdated copies inside the user's file (`versioning.py:1699-1860`). This is how they ship shader fixes without regenerating user graphs.

## 3. Channels model

Channels are **user-defined, typed slots** (`VALUE` / `RGB` / `NORMAL`, `Root.py:3846`), not a fixed PBR set; presets just create the usual BaseColor/Roughness/etc. and wire the root group node's outputs to matching BSDF inputs (restorable via `YPaintMaterialProps.ori_bsdf`, `Root.py:4444`). Only one NORMAL channel can exist; it is heavily special-cased (height, vector displacement, parallax, smooth bump).

**One layer has one source; channels share it.** The layer's single source (image/vcol/procedural) feeds *all* enabled layer-channels; each `YLayerChannel` selects which source output socket to consume (`socket_input_name`, `Layer.py:6761`), optionally swizzles (`swizzle_input_mode`, `Layer.py:6775`), linearizes, runs per-channel modifiers, then blends into that channel's chain with its own blend type and opacity. So "paint white onto this layer" can simultaneously affect BaseColor and Roughness — but only because both channels read the *same* stroke data.

**Per-channel overrides break the sharing.** A layer channel can be overridden with its own value/color/image/vcol/procedural source (`override`, `override_type`, `Layer.py:6852-6882`; node management in `check_override_layer_channel_nodes`, `subtree.py:2229`), and the NORMAL channel has a *second* override slot for the bump-vs-normal-map duality (`override_1`, `Layer.py:6894`). This is their answer to "Substance-style one layer with distinct per-channel content": a layer becomes a bundle of per-channel sources sharing masks, transforms, and opacity.

**Masks are layer-scoped and channel-gated.** A `YLayerMask` (image/vcol/procedural/color-ID/edge-detect/AO…) applies to the layer as a whole, with per-channel enable and per-channel mix nodes (`YLayerMaskChannel`, `Mask.py:2397`; `check_mask_mix_nodes`, `subtree.py:562`). So one painted mask genuinely drives all channels of the layer — this is the multi-channel behavior that matters in practice, and it's structural, not faked.

**Painting is strictly single-image.** There is no multi-channel stroke: exactly one image is the paint canvas at any time. The "active edit" entity (layer source, mask, or channel override image) is resolved and pushed into `scene.tool_settings.image_paint.canvas` / the active texture paint slot by `set_active_paint_slot_entity` (`common.py:5170`), triggered from `update_layer_index` (`Root.py:3208`). Multi-channel results from one stroke exist only in the shared-source case above. A `depsgraph_update_post` handler exists solely to fight Blender picking the wrong paint slot (`ypaint_missmatch_paint_slot_hack`, `Root.py:4669`; noted as unnecessary from Blender 5.1).

**Layer-space transforms constrain painting.** Because strokes land in the image's own UV space, a layer with mapping transforms can't be painted WYSIWYG; the add-on generates a *temporary UV layer* matching the transformed mapping on layer selection (`refresh_temp_uv` call in `update_layer_index`, `Root.py:3248`) and nags the user to refresh it (`need_temp_uv_refresh`, `Root.py:4426`). This is a forced UX consequence of painting into transformed layers with Blender's paint tools.

**Color/alpha channel pairing:** rather than a hardcoded alpha, any channel can be declared the alpha pair of a color channel (`is_alpha`, `alpha_pair_name`, `Root.py:3883-3893`), and enabling it rewires material blend modes (`update_channel_alpha_blend_mode`, `Root.py:3902`).

## 4. Bake pipeline

Orchestrated by `YBakeChannels` (`Bake.py:1535`) with the machinery in `bake_common.py`. Shape:

1. **Snapshot & restore world state.** `remember_before_bake` (`bake_common.py:201`) books ~50 scene/render/bake/UV/visibility settings into a dict; `recover_bake_settings` (`bake_common.py:948`) restores. `prepare_bake_settings` (`bake_common.py:691`) forces Cycles, `samples=1` (default; emit bakes don't need more), motion blur/simplify off, denoising off, clears `material_override`, disables MIRROR/SOLIDIFY/ARRAY modifiers (`bake_common.py:13,851-867`), sets the target UV active *and* `active_render`.
2. **Emit-bake per channel.** `bake_channel` (`bake_common.py:2155`) creates a temp image node + emission node in the material, wires the channel's group output into the emission, and runs `bpy.ops.object.bake(type='EMIT')` (`bake_common.py:184`). This sidesteps most Cycles bake-pass semantics entirely.
3. **Normal channel is the exception:** a temp Principled BSDF is fed the channel's normal output and a true `bake_type='NORMAL'` (tangent space) is run (`bake_common.py:2249-2258,2439-2467`); armatures forced to rest pose (only tangent space is supported, `Bake.py:1734`). Additional passes bake normal-without-bump overlay, scalar displacement (from the HEIGHT socket), vector displacement, and max-height (a 100×100 float image with margin 1000, `bake_common.py:1957-2016`).
4. **Alpha is baked separately** into a copy image via the channel's ALPHA output, then composited into the RGB image's alpha channel pixel-wise, per UDIM tile (`bake_common.py:2736-2780`).
5. **Post passes are themselves bakes/renders:** denoising runs through a throwaway compositor scene per tile with disk round-trips (`bake_common.py:1333-1409`); FXAA is a bake of a temp plane with an FXAA node group (`bake_common.py:1691-1825`); anti-aliasing is SSAA (bake at N×, resize down — resize is also implemented as a plane bake, `bake_common.py:4890`).
6. **Multi-object/material:** objects sharing the material are temp-joined unless join is "problematic" (breaks Object/Generated texcoords or mismatched color attributes, `bake_common.py:131`); other materials' polygons get their UVs moved to (0,0) so they receive no rays (`Bake.py:1630-1660` — with a comment warning it "can freeze blender if there are too many polygons").

**Consuming bakes:** each channel stores `baked*` node references (`Root.py:4177-4201`); toggling `yp.use_baked` (`Root.py:4367`) makes `reconnect_yp_nodes` feed tree outputs from baked image nodes instead of the live chain (`node_connections.py:1434-1500`) — live layers stay in the graph, merely disconnected. `enable_baked_outside` (`Bake.py:2964`) additionally materializes plain image-texture nodes *outside* the group in the material tree (recording original links in `ori_to` collections, `Root.py:4215-4220`) for exporter compatibility, including a glTF ORM settings group (`Bake.py:3242-3268`). Layers and masks can individually be baked and switch to `use_baked` (`bake_common.py:4318`), which is also how procedural-only features (HEMI lighting, edge detect on EEVEE) get made bakeable.

**Bake-to-layer** (`BakeToLayer.py`, engine `bake_to_entity` at `bake_common.py:2858`) bakes AO/pointiness/cavity/bevel/multires/other-object projections *into* new layers or masks, records provenance on the image (`Image.y_bake_info`, `BakeInfo.py:20`) so everything can be re-baked later (`rebake_baked_images`, `bake_common.py:4713`).

**Encoded Cycles/Blender bake traps** (each is a workaround in code — treat as a checklist for our bake design):
- GPU bake failures: every bake wrapped in try/except with CPU retry (`bake_common.py:184-199`); OSL forces CPU (`bake_common.py:757`); OptiX couldn't bake on 2.81–2.9x so it silently switches to CUDA (`bake_common.py:762-766`).
- UDIM pixel access only works on tile 1001 → they *rename image files on disk* to rotate each tile into slot 1001 and back (`UDIM.swap_tile`, `UDIM.py:347-415`).
- `use_clear=False` always, except other-object alpha bakes which need `use_clear=True` to get alpha at all (`bake_common.py:3840-3844`).
- Depsgraph handlers must be halted during bakes: "can cause crash or bake inconsistencies" (`bake_common.py:5222-5225`).
- Bake margin: default 5px adjacent-faces, but 1000 for the max-height helper "to make sure all pixels are covered" (`bake_common.py:1971`).
- Tangent-sign vertex-color hack for 2.8x–2.9x Cycles normal-map behavior differences (`is_tangent_sign_hacks_needed`, `common.py:7484`; `Root.py:4406`).
- Misc comment-documented bugs: Blender 4.5 image node "can mistakenly use previous index image" (`bake_common.py:3883`); string attributes crash bakes on 5.1 and are removed (`bake_common.py:4866-4870`); geometry-node UVs are 3D vectors in 3.5 and must be converted to FLOAT2 (`bake_common.py:4857-4864`); baked nodes accidentally shared between channels per user reports, so uniqueness is re-checked (`bake_common.py:2292-2295`).
- Bake-to-vertex-color (2.92+): emit-bake with `bake_target='VERTEX_COLORS'`, alpha baked to a temp attribute and merged via numpy (`bake_common.py:1827-1902`).

## 5. Color space and Blender API landmines

The codebase is effectively a fossil record of Blender API churn. Clusters, roughly by pain:

1. **Node-tree interface API (Blender 4.0).** `tree.inputs/outputs` → `tree.interface.items_tree` forced a full wrapper family: `get_tree_inputs/outputs`, `new_tree_input/output`, index-fixing helpers (`common.py:2825-2965`, `input_outputs.py:14-79`). Sub-landmines: `NodeSocketFloatFactor` no longer exists (use `NodeSocketFloat` + subtype, `common.py:2871-2875`); socket type identification changed (`socket_type` string vs `type` enum, `common.py:4911`); and — directly relevant to us — a comment records that **setting a socket subtype in Blender 5.1 alpha makes the input socket disappear** (`common.py:2891-2892`).
2. **Color attributes vs vertex colors (3.2).** Accessor shims (`common.py:4896-4943`), BMesh layer-type branching (`common.py:4996`), plus two hacks: baking to vcol still uses the *legacy* active vertex color so both actives must be set (`common.py:4949-4953`), and 3.2+ doesn't refresh the viewport after vcol rename — worked around by toggling every object through sculpt mode (`common.py:2605-2616`).
3. **ShaderNodeMix vs ShaderNodeMixRGB (3.4)** — node id and input-index abstraction (`common.py:6716-6790`), migration converter (`versioning.py:1301`).
4. **Colorspace names are never trusted.** `get_srgb_name()`/`get_noncolor_name()` enumerate the RNA enum items of `colorspace_settings`; under custom OCIO configs they fall back to prefix matching, and as a last resort create a throwaway 1×1 image and read which colorspace Blender assigned by default (`common.py:788-825`). The 4.0 'Linear' → 'Linear Rec.709' rename gets a fuzzy matcher (`common.py:830-842`). Comment-documented traps: freshly loaded UDIMs can have an *empty* colorspace name (`common.py:6477-6480`); "Generated float images is behaving like srgb for some reason" (`common.py:6491`).
5. **Color management is done manually anyway.** Rather than relying on image colorspace alone, explicit gamma/power "linear" nodes are inserted per image/channel/mask (`check_layer_image_linear_node` et al., `subtree.py:2510-2597`, `GAMMA = 2.2` at `common.py:693`) so channel blending happens in a controlled space; the user-facing `use_linear_blending` toggle (`Root.py:4429`) exists because linear blending "will behave differently than Photoshop". Two shipped migrations exist purely to fix earlier wrong linearization decisions (`versioning.py:494-531`, `versioning.py:938-1004`) — evidence that colorspace policy is the most regression-prone design decision in this domain.
6. **Image lifecycle.** Float image atlases must force `alpha_mode='PREMUL'` (`ImageAtlas.py:74-78`); packing UDIMs requires a real filepath, so images are saved to temp `<UDIM>.png` paths, packed, then the disk files deleted (`UDIM.py:304-345`); empty tiles break packing (`UDIM.py:293-301`); 2.7x float images couldn't be packed at all without a temp-scene save (`image_ops.py:9-63`); float images sometimes need `image.reload()` to display (`image_ops.py:75-77`).
7. **Undo.** There is no undo handler; resilience comes from (a) operators using `REGISTER`/`UNDO`, (b) all node references being lazily-resolved name strings (§1), and (c) a `depsgraph_update_post` self-heal pass that detects post-undo/duplication image renames by comparing `img.name` to the stored `layer.image_name` (`Root.py:4549-4560`) and a `refresh_tree` flag that re-links one link twice to force a shader recompile ("HACK: Refresh normal", `Root.py:4530-4547`). `msgbus` subscriptions (active object/mode/material) must be re-registered on every file load (`Root.py:4711-4724`).
8. **Animation of custom PropertyGroups doesn't propagate:** a `frame_change_pre` handler manually re-evaluates yp F-Curves each frame (`Root.py:4757-4800`), and Blender 5.0's slotted Actions required a new fcurve-access shim (`common.py:6814-6880`).
9. **Their own format versioning.** `yp.version`, `yp.blender_version`, and `yp.is_unstable` are stamped on every tree (`Root.py:4256-4263`); on `load_post`, `update_routine` (`versioning.py:1325`) runs ~30 gated migration blocks (node-layout refactors, colorspace fixes, Musgrave→Noise on 4.1+, Legacy→modern lib-group swaps for 2.7x files, Mix node conversion…), then re-stamps. Combined with the lib-tree `revision` hot-patch system (§2), this is a complete, battle-tested two-axis versioning scheme (addon version × Blender version) — the part of Ucupaint most worth copying wholesale.

**Blender 5.x churn already visible in their code** (what we'll face targeting 5.1.2): slotted-Actions fcurve access (5.0), brush API renames `image_tool`→`image_brush_type` (`common.py:8134-8146`), socket-subtype bug on input creation (5.1 alpha, `common.py:2891`), string attributes crashing bakes (5.1, `bake_common.py:4866`), geometry-nodes modifier input access change (5.2, `common.py:2861-2865`), compositor node API rework (`bake_common.py:76-88`), paint-slot mismatch hack retired as of 5.1 (`Root.py:4863-4866`).

## 6. Performance cliffs

**Architecturally slow (inherent to the design):**

- **Root-scope rebuild on nearly every structural edit.** Toggling one layer channel runs `check_*` + `reconnect_layer_nodes` + `reconnect_yp_nodes` + two full rearrange passes (`Layer.py:6156-6215`). Each pass is O(all layers × all channels) of string-keyed `nodes.get()` and socket scans through `bpy`. The per-callback ms-print instrumentation (71 sites) shows the devs fighting this constantly.
- **Socket-count explosion.** The inter-layer protocol (§2) multiplies sockets per layer by channels and by the smooth-bump ×5 height fan-out (`input_outputs.py:932-1109`). More sockets → bigger interface updates (each interface edit re-syncs every instance of the group), bigger graphs, slower EEVEE compiles.
- **EEVEE shader recompilation.** Any actual link/node change dirties the material and triggers a full shader recompile; with many layers the generated shader itself becomes enormous (smooth bump samples every source 5×; parallax iterates the whole depth chain `parallax_num_of_layers` times, `Root.py:3981`). Their mitigations: idempotent `create_link` (§2) so no-op passes don't dirty; baking (§4) as the pressure-release valve; the abandoned trash-group experiment (`Layer.py:6551`) and an abandoned `disable_quick_toggle` option (`Root.py:4400-4404`) are two visible scars of trying to make toggling cheap.
- **Per-layer node-group duplication** (`_Copy` lib trees, per-layer trees) means datablock count grows linearly with layers, and lib hot-patching must walk all of them on load (`versioning.py:1699-1860`).
- **Index-based collections** make reorder O(layers) property rewrites plus regex rewriting of every F-Curve path (`common.py:7030`).

**Incidentally slow (implementation choices, avoidable):**

- Rearrange passes recompute node positions for the entire tree on every change (`node_arrangements.py:1953`) — pure cosmetics for a graph users are told not to edit ("WARNING: Do NOT edit this group manually!", `common.py:2000`).
- `refresh_list_items` rebuilds the flattened UI list collection after most operations (`ListItem.py:30`).
- Pixel operations (alpha compositing after bakes, atlas segment copies, UDIM handling) go through per-tile Python/numpy copies with disk round-trips for UDIM (`UDIM.py:347-415`, `bake_common.py:2736-2780`).
- Multi-material bake prep iterates all polygons in Python (`Bake.py:1630-1660`, with the in-code freeze warning).

**Scaling mitigations they did build:** image atlases pack many small mask/layer images into one shared texture with mapping-node offsets, both to cut datablock count and image-editor clutter (`ImageAtlas.py:143-185,257-293`; UDIM variant allocates tile rows, `UDIM.py:632-657`); default atlas 8192px (`preferences.py:23`); bake samples default to 1; `halt_update`/`halt_reconnect` batching.

**Confirmed in the issue tracker** (cross-checked against the code above):

- *EEVEE recompile per tweak is the #1 complaint.* Issue #122 ("Reduce shader compilation frequency"): every edit rebuilds the tree and "the interface hangs for about a second" per change in material preview; a debounce was requested, never implemented — the maintainer's answer is "go to solid mode." Issue #64 (dev-authored, done): deleting unused nodes/sockets "can improve shader compilation and playback FPS" — direct confirmation that graph size drives both compile time and framerate.
- *Hard GPU ceilings from graph growth.* Issue #361 (closed **wontfix**): ~6 procedural noise masks generated >66,000 lines of shader code, exceeding an RTX 3090's instruction limit; accepted workaround was baking masks to images. Issue #315 (wontfix): >7 image-mask layers blanked all textures on macOS/Intel because "Blender 3.x can only do up to 8 images in macOS" — the image atlas was the suggested mitigation. Issue #264: pink materials on complex setups, acknowledged as a known Blender limit.
- *Many layers → UI and layer-switch lag independent of EEVEE.* Issue #333 ("Improve performance", open): lag with many layers and during bakes, with the maintainer's root-cause admission that "most of the issues come from the ucupaint UI itself"; release notes later claim UI drawing "up to five times faster" on complex setups. Issue #72: switching layers is laggy with many layers *even in solid view* — consistent with the per-selection reconnect/refresh path (`update_layer_index`, `Root.py:3208`). Issue #246: a blend-mode change froze the machine and corrupted the .blend.
- *Bake cost.* Issue #138: ~5 min for a 4K bake on defaults; recommended mitigations are exactly the knobs in §4 (FXAA/denoise off, samples=1, GPU). Issue #269 (open): 10–20 GB RAM spikes baking with heavy scenes — Cycles evaluates all render-enabled objects during bakes.
- *File size and undo.* Issue #125 (fixed): layer duplication used to copy packed images, ballooning files. Issues #252/#342 (both wontfix): undo "goes too far" / behaves weirdly outside object mode — blamed on Blender's undo stack; the community workaround is running a cache-clean operator to insert a history entry. Undo remains the least-solved area.
- *bpy overhead awareness:* commit history shows deliberate `bpy.ops` avoidance for perf ("Use bmesh instead of bpy.ops"), but node-creation cost itself is never quantified in issues; the recompile cost is treated as "currently unavoidable" in EEVEE (#122).

## 7. Worth stealing vs. worth avoiding

**Steal:**

1. **State on the node group's tree** (`ShaderNodeTree.yp`). Free persistence, free append/link portability, material-agnostic, survives material rebuilds. This is the single best structural decision in the codebase.
2. **Node references as unique-suffixed name strings with lazy `nodes.get()` resolution** (`common.py:1925`). Undo-safe by construction. Any pointer-based scheme fights Blender's ID lifecycle and loses.
3. **Idempotent check-and-repair + `create_link`-only-if-missing** (`node_connections.py:3`). Self-healing against user tampering and undo weirdness, and it naturally avoids dirtying unchanged graph regions (recompile avoidance).
4. **Two-axis versioning + lib-revision hot-patching** (§5.9). They ship node-graph bug fixes to existing .blends without full regeneration. Design this in from day one; retrofitting it is what half of `versioning.py`'s complexity is.
5. **Never trust environment names:** RNA-probed colorspace names, capability probes over version checks where possible (`common.py:788-842`).
6. **Emit-bake through a temp emission node + settings book/restore** (§4). The entire bake snapshot/restore discipline is a hard-won checklist.
7. **Masks shared across channels with per-channel gating** — the genuinely useful multi-channel semantics (§3).
8. **Batch flags around bulk operations** — but make them non-persistent (runtime, e.g. WindowManager or module state) with context-manager semantics, fixing their stuck-flag failure mode (`Root.py:2105`).

**Avoid:**

1. **Flat collection + `parent_idx` + index-aligned per-layer channel lists.** It infects everything: fcurve rewriting, parent remapping dictionaries, mismatch-repair operators, positional coupling between `yp.channels` and every `layer.channels`. Use stable keys (UUID-ish names) for cross-references, and treat indices as presentation order only.
2. **~100 `StringProperty` node-name slots spread across every PropertyGroup level.** This is schema-as-shrapnel: adding a feature means touching the PropertyGroup, the check function, the reconnect function, the rearrange function, and versioning. A single declarative "node role table" per entity type would collapse four hand-maintained passes into one driven by data.
3. **Root-scope reconnect on every edit.** With a declarative graph spec you can diff desired-vs-actual per subtree and touch only the changed layer + the root chain segment behind it.
4. **The smooth-bump directional socket fan-out** as a default-on feature. It quintuples source evaluations and dominates socket counts; it exists because they compute bump from height *inside* the stack across layer boundaries. Consider computing normal-from-height once at the end of the chain, or making height a bake-time-only refinement.
5. **Cosmetic full-tree rearrangement in the hot path.** If the graph is machine-owned, lay out lazily (on inspection) or not at all.
6. **In-graph feature maximalism** (parallax loops with 8–128 iterations as node groups, fake lighting, edge detection via bevel tricks). Each became hundreds of lines of reconnect logic and a permanent compile-time tax. Anything that is bake-like should live in the bake pipeline, not the live graph.
7. **Organic growth artifacts:** deprecated properties kept forever (`enable_alpha`, `mod_group`, `subdiv_tweak`…), commented-out experiments left in hot functions, 8k-line modules mixing operators/model/logic. Normal for a decade-old volunteer project; a clean-room design should separate model, compiler, and operators from the start.

## 8. Feasibility sketch for a clean-room layer-stack add-on

**Data model.** PropertyGroups on a `ShaderNodeTree` (steal #1), but: layers keyed by immutable generated IDs; order stored as an explicit ordered list of IDs; hierarchy by `parent_id`; channel references by channel ID, with each layer holding a *sparse* map of per-channel settings (only channels the layer actually configures) instead of index-aligned dense lists. Persist a `schema_version` + `blender_version` pair per tree from day one. All node references by name-string via one shared helper.

**Compilation strategy.** Treat the node graph as a *build artifact* compiled from the model — never the source of truth, never user-edited. Concretely:

- A pure function `spec = compile_layer(layer_state)` produces a declarative description (nodes, params, links, interface sockets) — trivially unit-testable without `bpy`.
- A small *reconciler* applies a spec to a `ShaderNodeTree`: diff nodes by role-name, create/remove/re-param/re-link only deltas. Idempotent, self-healing (Ucupaint's `check_*` benefit) but written once, not per-feature. Zero-delta application must be zero mutation (EEVEE recompile avoidance).
- Rebuild granularity: layer edits recompile that layer's tree only; stack edits (reorder/add/remove/blend-into-chain changes) recompile only the root chain wiring. The root chain should be one node group *per channel* with a fixed narrow interface (color+alpha, height handled once at chain end), so socket counts stay O(1) per layer instead of O(channels × 5).
- Debounce graph application: coalesce model edits and apply the reconciled delta on a short timer (the exact mitigation users asked Ucupaint for in issue #122 and never got). The model/artifact split above is what makes this trivial; Ucupaint can't do it because its update callbacks mutate the graph directly.

**Channels.** Fixed narrow inter-layer protocol; per-channel override sources and channel-gated shared masks like Ucupaint (§3 semantics are right, the plumbing isn't). Normal/height: single conversion at chain end; smooth-bump-style refinement only as a bake option.

**Painting.** Accept Blender's constraint: one canvas image at a time, selection-driven (their `set_active_paint_slot_entity` logic is the pattern). Don't attempt multi-channel strokes in v1; get shared-source + shared-mask semantics instead.

**Baking.** Adopt their emit-bake + book/restore skeleton and trap checklist (§4) wholesale; keep denoise/AA as post passes; UDIM later (the tile-1001 swap hack is a maintenance bomb — check whether Blender 5.x fixed per-tile pixel access before committing).

**Versioning.** Ship the migration runner and lib-revision mechanism in v0. Gate every migration on (schema_version, blender_version), like `versioning.py` does.

**Risk read:** the hard parts are not the layer stack (a weekend of node-graph generation) but (a) colorspace policy — decide blending space up front, it's their most-regretted decision; (b) EEVEE recompile latency on big stacks — mitigated by narrow interfaces and zero-delta reconciliation; (c) Blender API churn — budget a compat layer module from the start; their 691 version gates say one point release per year will break something; (d) bake correctness across GPU/UDIM/multi-object — copy their workarounds rather than rediscovering them.

## 9. Post-implementation comparison with Impasto

**Updated 2026-08-14.** The architecture study above examined Ucupaint 2.4.8
while Impasto was still being designed. Ucupaint 2.4.9 is now the current
release, and Impasto 0.15.24 has implemented a materially different painting
architecture from the conservative feasibility sketch in §8.

The projects overlap as Blender layer-stack tools, but optimize for different
problems:

- **Ucupaint** is the broader and more mature system for constructing,
  transforming, masking, baking, and exporting complex layered materials.
- **Impasto** is a narrower, material-oriented system built around responsive,
  simultaneous painting into distinct PBR channel canvases.

### The important meaning of “multi-channel”

Ucupaint can make one layer affect multiple material channels. Its primary
painted image is a shared layer source; per-channel overrides can reinterpret
that source or supply distinct fixed values, images, or procedural sources.
This provides useful multi-channel *material semantics*, especially because
the channels can share masks and transforms.

Painting itself remains single-image: Ucupaint assigns exactly one Blender
Image as the active Texture Paint canvas. It does not write independently
chosen Base Color, Roughness, Metallic, Normal, Height, and other values into
separate channel images during one brush stroke.

Impasto does. A Paint layer owns a separate image per registered channel. Its
GPU engine writes a distinct payload to every enabled target canvas in one
stroke and records them as one multi-channel undo transaction. This is
Impasto's central reason to exist: the user paints a material rather than
painting one texture channel at a time.

### Capability comparison

| Area | Ucupaint | Impasto |
| --- | --- | --- |
| Painting engine | Blender native Texture Paint with one active Image | Custom GPU-resident multi-channel engine; native single-channel editing remains available |
| Channels | Arbitrary user-defined RGB, Value, and Normal channels | Fixed Principled-PBR-oriented channel registry |
| Layer sources | Image, color, vertex color, procedural, background, AO, edge detection, and others | Paint, Fill, and pass-through Group layers |
| Masks | Mature image, procedural, color-ID, AO, and other masks with per-channel gating | Paintable image masks with visibility, inversion, opacity, and per-channel scope |
| Blending and effects | Broad per-channel blend modes, modifiers, and transitions | A smaller set of material-focused composition semantics |
| Normal/displacement | Bump, normal, displacement, vector displacement, parallax, and smooth bump | Tangent Normal and Height painting with bottom-up RNM normal composition |
| Texture layouts | UDIM and image-atlas support | One ordinary image per channel binding; UDIM and mixed-UV flattening are not supported |
| Baking | Extensive channel, layer, mask, AO, multires, other-object, and vertex-color baking | Stack flattening to channel images; high-to-low normal baking is handled by Kiln |
| Compatibility and distribution | Long Blender-version history, migrations, GitHub releases, and Blender Extensions | Blender 5.1 target; source-folder installation while release packaging is prepared |

Ucupaint therefore remains substantially ahead in procedural sources, UDIMs,
image atlases, arbitrary channels, mapping modes, modifiers, displacement,
baking breadth, compatibility, and production hardening. Impasto should not be
described as a general replacement for it.

### Performance claims and limits

Calling Ucupaint “CPU-resident” is imprecise. It delegates painting to
Blender's native Texture Paint implementation, which may use GPU resources
internally, while Blender Images remain the authoritative canvases. The
defensible distinction is that Ucupaint does not maintain a private set of
resident channel textures or a multi-render-target painting session.

Consequently, do not claim that Ucupaint is categorically slow at 4K without
an equivalent benchmark. It inherits Blender Texture Paint behavior, including
single-image targeting and Image update/undo costs, but actual performance
depends on the Blender version, brush, mesh, layer graph, and hardware.

Impasto has measured 4K production runs, including a five-channel session on a
Quadro RTX 5000 Max-Q sustaining 272–523 input dabs/s with approximately
0.0045–0.0063 ms GPU command-submission time per dab. Those figures describe
that specific workload and hardware, not universal throughput. Its performance
tradeoffs are substantial VRAM use, GPU-session startup, explicit Image
synchronization delays, and limited backend qualification.

### Positioning

The accurate public summary is:

> Ucupaint is currently the stronger general-purpose Blender layer-texturing
> system. Impasto focuses on a different interaction: painting distinct PBR
> channel canvases simultaneously through a GPU-resident material brush.

Current upstream references:

- [Ucupaint repository](https://github.com/ucupumar/ucupaint)
- [Ucupaint layer-channel documentation](https://ucupumar.github.io/ucupaint-wiki/01.03.layer-channel/)
- [Ucupaint layer documentation](https://ucupumar.github.io/ucupaint-wiki/01.02.layer/)
