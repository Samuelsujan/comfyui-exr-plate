# Samuelsujan's EXR Plate — Linear EXR I/O for Nuke ↔ ComfyUI

Custom ComfyUI nodes plus a Nuke bridge for working with **linear EXR plates**
in AI-assisted VFX pipelines — without breaking your colorspace.

Built by a compositor, for compositors. Generative models want sRGB in the
0–1 range; your plates live in scene-linear EXR. This pack handles the
conversion both ways with pure, invertible transfer-function math, so what
comes back into Nuke merges cleanly over your original plate.

**Author:** Gunupudi Samuel Sujan Deep (Samuelsujan) — Nuke Compositor
**LinkedIn:** https://www.linkedin.com/in/gunupudi-samuel-sujandeep-a316bb225/

---

## What's included

### ComfyUI nodes (category: EXR Plate)

**Load EXR Plate (linear)**
- Single EXR, `####` / `%04d` sequences, or a bare folder of EXRs
- `first_frame` / `last_frame` range (0 = auto-detect all frames)
- VHS-style controls: `fps`, `custom_width` / `custom_height` (keep-aspect),
  `frame_load_cap`, `skip_first_frames`, `select_every_nth`
- Colorspace transforms: keep linear, linear ↔ sRGB, linear ↔ Rec.709
- Exposure in stops (applied in linear), HDR passthrough via `clamp_output`
- **Live plate preview inside the node** — updates the moment the path,
  colorspace, or exposure changes, with sequence **playback** (▶ button,
  loops at the fps you set) and a resolution / frame-count readout
- "choose exr sequence to upload" button for drag-free loading
- Outputs: `image`, `alpha`, `frame_count`, `fps`, and a VHS-compatible
  `video_info`

**Save Plate (linear)**
- Formats: EXR (half/float, ZIP compressed, keeps HDR values above 1),
  TIFF 16/8-bit, PNG 16/8-bit, JPG (integer formats clamp to 0-1)
- Inverse transforms (e.g. sRGB → linear) so results land back in linear
  for Nuke — no LUT guessing
- Optional resize on save, alpha re-embed from a MASK input

**Path Builder (versioned)**
- **Browse** button that walks the machine ComfyUI is running on — drive
  letters included, so `D:/disk/city/...` is two clicks, not a paste
- No show/shot/task fields: one browsed folder, an optional `subpath`, and a
  name. Nothing to wedge by pasting a full path into the wrong box
- Version resolves **at execute time**, not when you set the widget:
  `auto_next` takes the highest `v###` on disk + 1, `auto_latest` reads the
  current one, `manual` uses the widget verbatim. Queue twice and you get
  v004 then v005 — no silent overwrite
- `version_style`: `folder` → `.../v003/plate.####.exr`, or
  `filename` → `.../plate_v003.####.exr`
- Live readout on the node showing the exact path the next run will write
- Outputs `path` (wire into Save Plate or Load EXR Plate), `folder`, and
  `version` as an INT

Note: Save Plate always sets the extension from its own `format` dropdown,
so the builder's `ext` matters for Load and for reading the path, not for
what Save writes.

**Linear → sRGB / sRGB → Linear**
- Standalone converters for sandwiching a generative model mid-graph

### Nuke bridge (`nuke_comfy_bridge.py`)

- **F9 — Send Plate to Load EXR Plate:** select a Read in Nuke, press F9,
  and the plate appears in the Load EXR Plate node on your open ComfyUI
  canvas — path and frame range filled, preview showing. Non-Read
  selections are baked to a temp linear EXR first (with confirmation).
- **ComfyUI → Nuke push:** enable `send_to_nuke` on Save Plate and the
  finished render appears in Nuke as a Read node the moment the graph
  completes (the bridge auto-starts a listener on port 8189 when Nuke
  loads).
- **F10 — Run Workflow on Selected (full bridge):** bakes the selected node
  to linear EXR, injects it into a ComfyUI workflow of your choice, submits
  it over the HTTP API, waits with a cancellable progress bar, and imports
  the result as a linear Read node next to your selection.
- **Versioned output:** F10's panel has a *Versioned output* checkbox (on by
  default). Job folders go to `<out dir>/<node name>/v001/`, `v002/`, ...
  using the same `v###` rule as the Path Builder node, instead of a
  timestamp nobody can read.
- **Path Builder aware:** if the workflow's Save path is *wired* from a Path
  Builder rather than typed in, the bridge points the builder at the job
  folder and lets it pick the version, then imports whatever actually landed
  — it doesn't overwrite the link and break the graph.

---

## Installation

### 1. ComfyUI nodes

Copy the `comfyui-exr-plate` folder into your ComfyUI custom nodes
directory:

```
ComfyUI/custom_nodes/comfyui-exr-plate/
    __init__.py
    nodes.py
    path_builder.py
    pyproject.toml
    requirements.txt
    nuke_comfy_bridge.py          <- copy this one to ~/.nuke/
    menu_snippet.py               <- paste into ~/.nuke/menu.py
    passthrough_api_format.json
    versioned_save_api_format.json
    web/exr_plate.js
    web/exr_plate_pathbuilder.js
```

`web/` has to stay a subfolder — `WEB_DIRECTORY = "./web"` in `__init__.py`
is what makes ComfyUI serve the JS. Everything else sits flat.

If OpenCV isn't already in your ComfyUI Python environment:

```
pip install -r requirements.txt
```

Restart ComfyUI and hard-refresh the browser (**Ctrl+Shift+R**). The nodes
appear under **Add Node → EXR Plate**.

### 2. Nuke bridge

1. Copy `nuke_comfy_bridge.py` to your `.nuke` folder
   (Windows: `C:\Users\<you>\.nuke\`, Linux/macOS: `~/.nuke/`).
2. Add the contents of `menu_snippet.py` to your `.nuke/menu.py`
   (create `menu.py` if it doesn't exist).
3. Restart Nuke. A **Samuelsujan** menu appears with both commands.

---

## Quick start

1. Start ComfyUI and open it in your browser.
2. In Nuke, select the Read node of your EXR plate and press **F9**.
3. Switch to the browser — your plate is in the Load EXR Plate node.
   Press ▶ on the preview to loop the sequence.
4. Build your graph. Feeding a diffusion / video model? Set the loader's
   colorspace to **linear → sRGB** (or use the standalone converters), and
   set Save EXR Plate to **sRGB → linear**.
5. Read the saved sequence back in Nuke — it's linear, and merges straight
   over your plate.

### Verifying the roundtrip

Run `passthrough_api_format.json` (Load → Save, nothing
between, both set to "keep linear", clamp off) on any plate, then in Nuke
Merge (minus) the result against the source and gain up. Black frame =
lossless linear pipeline.

---

## Requirements

- ComfyUI (local install — custom nodes don't run on hosted Comfy Cloud)
- Python packages: `opencv-python >= 4.6`, `numpy` (torch ships with ComfyUI)
- Nuke 13+ for the bridge (any recent Python-3 Nuke)
- The full bridge (F10) needs workflows exported via **Save (API Format)**
  (enable dev mode in ComfyUI settings)

## Troubleshooting

- **Nodes don't appear** — check the folder name has no extra nesting
  (`custom_nodes/comfyui-exr-plate/nodes.py`, not
  `custom_nodes/comfyui-exr-plate/comfyui-exr-plate/nodes.py`), then check
  the ComfyUI console for import errors (usually missing OpenCV).
- **No preview** — the preview needs the `web/` folder in place and a
  browser hard-refresh after install.
- **Browse dialog is empty / errors** — the browse endpoint runs
  server-side, so it lists the filesystem of the machine ComfyUI is on. On a
  remote or hosted instance you'll see that machine's disks, not yours.
- **Version doesn't bump** — check `version_mode` isn't on `manual`, and
  that the existing versions actually match `v###` (the scan ignores
  `rev2`, `version_2`, etc.).
- **F9 does nothing in the browser** — ComfyUI must be running and the tab
  open; check the host at the top of `nuke_comfy_bridge.py`
  (default `http://127.0.0.1:8188`).
- **Preview looks dark** — that's your plate in true linear; switch the
  colorspace widget to **linear → sRGB** to view it display-referred.

## License

MIT — see LICENSE. Use it on your shows, fork it, improve it.
