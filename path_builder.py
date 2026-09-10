"""
Path Builder for the EXR Plate pack.

Browse to a folder on the machine ComfyUI is running on, then let the node
work out the version. No show/shot/task fields — one browsed folder plus a
name, so pasting a full path somewhere can't wedge it.

  version_mode:
    auto_next   -> highest version on disk + 1   (writing a new version)
    auto_latest -> highest version on disk       (reading the current one)
    manual      -> the `version` widget, verbatim

The version resolves at execute time, not when you set the widget, so
queueing twice writes v004 then v005 rather than overwriting.

Outputs `path` for Save Plate / Load EXR Plate, plus `folder` and `version`.
"""

import os
import re
import string

TRANSFORM_SAFE = True  # nothing here touches pixels; kept import-light


# ---------------------------------------------------------------------------
# Version scanning
# ---------------------------------------------------------------------------

# matches v3, v003, _v012, plate.v7 — but not "rev2" or "v2x"
_VERSION_RE = re.compile(r"(?:^|[^a-zA-Z0-9])v(\d+)(?:$|[^0-9])", re.IGNORECASE)

FRAME_TOKENS = ["####", "########", "%04d", "%08d", "none"]

EXTENSIONS = ["exr", "tif", "png", "jpg"]


def scan_version(folder, name, style):
    """Highest existing version under `folder`. 0 if there is none.

    style == "folder"   -> looks at subdirectory names (v001/, v002/)
    style == "filename" -> looks at files starting with `name` (plate_v001.####.exr)
    """
    if not folder or not os.path.isdir(folder):
        return 0

    highest = 0
    prefix = (name or "").lower()
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    continue
                if style == "folder":
                    if not is_dir:
                        continue
                    target = entry.name
                else:
                    if is_dir:
                        continue
                    target = entry.name
                    if prefix and not target.lower().startswith(prefix):
                        continue
                match = _VERSION_RE.search(target)
                if match:
                    highest = max(highest, int(match.group(1)))
    except OSError:
        return 0
    return highest


def resolve_version(folder, name, style, mode, manual):
    if mode == "manual":
        return int(manual)
    latest = scan_version(folder, name, style)
    if mode == "auto_next":
        return latest + 1
    return max(latest, 1)          # auto_latest


def build_path(folder, name, version, pad, style, frame_token, ext):
    """Assemble the final path. Pure string work — no disk access."""
    base = (folder or "").strip().replace("\\", "/").rstrip("/")
    name = (name or "plate").strip() or "plate"
    vstr = "v{}".format(str(int(version)).zfill(int(pad)))
    token = "" if frame_token == "none" else frame_token

    if style == "folder":
        out_folder = "{}/{}".format(base, vstr)
        stem = name
    else:
        out_folder = base
        stem = "{}_{}".format(name, vstr)

    filename = ("{}.{}.{}".format(stem, token, ext) if token
                else "{}.{}".format(stem, ext))
    return "{}/{}".format(out_folder, filename), out_folder


# ---------------------------------------------------------------------------
# Directory listing for the Browse dialog
# ---------------------------------------------------------------------------

def _drives():
    """Windows drive letters; posix just gets root."""
    if os.name != "nt":
        return ["/"]
    found = []
    for letter in string.ascii_uppercase:
        candidate = "{}:/".format(letter)
        if os.path.exists(candidate):
            found.append(candidate)
    return found


def list_dir(path):
    if not path:
        return {"path": "", "parent": None, "drives": _drives(),
                "dirs": [], "files": [], "file_count": 0, "error": None}

    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return {"path": path.replace("\\", "/"),
                "parent": os.path.dirname(path).replace("\\", "/"),
                "drives": _drives(), "dirs": [], "files": [],
                "file_count": 0, "error": "not a directory"}

    dirs, files = [], []
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                try:
                    (dirs if entry.is_dir() else files).append(entry.name)
                except OSError:
                    continue
    except PermissionError:
        return {"path": path.replace("\\", "/"),
                "parent": os.path.dirname(path).replace("\\", "/"),
                "drives": _drives(), "dirs": [], "files": [],
                "file_count": 0, "error": "permission denied"}

    dirs.sort(key=str.lower)
    files.sort(key=str.lower)

    parent = os.path.dirname(path.rstrip("/\\"))
    if parent == path or not parent:
        parent = None

    return {
        "path": path.replace("\\", "/"),
        "parent": parent.replace("\\", "/") if parent else None,
        "drives": _drives(),
        "dirs": dirs,
        "files": files[:60],        # orientation only — folders are what you pick
        "file_count": len(files),
        "error": None,
    }


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class EXRPlatePathBuilder:
    CATEGORY = "EXR Plate"
    RETURN_TYPES = ("STRING", "STRING", "INT")
    RETURN_NAMES = ("path", "folder", "version")
    FUNCTION = "build"
    DESCRIPTION = ("Browse to a folder and build a versioned output path for "
                   "Save Plate / Load EXR Plate.")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "folder": ("STRING", {
                    "default": "",
                    "tooltip": "Base folder. Use the Browse button — it walks "
                               "the machine ComfyUI is running on.",
                }),
                "name": ("STRING", {
                    "default": "plate",
                    "tooltip": "Filename stem, e.g. END_130_comp",
                }),
                "version_mode": (["auto_next", "auto_latest", "manual"], {
                    "tooltip": "auto_next = highest on disk + 1 (write). "
                               "auto_latest = highest on disk (read). "
                               "manual = use the version widget.",
                }),
                "version": ("INT", {
                    "default": 1, "min": 0, "max": 9999,
                    "tooltip": "Only used when version_mode is manual.",
                }),
                "version_pad": ("INT", {"default": 3, "min": 1, "max": 6}),
                "version_style": (["folder", "filename"], {
                    "tooltip": "folder -> .../v003/plate.####.exr\n"
                               "filename -> .../plate_v003.####.exr",
                }),
                "frame_token": (FRAME_TOKENS, {
                    "tooltip": "Sequence padding. 'none' for a single file.",
                }),
                "ext": (EXTENSIONS, {
                    "tooltip": "Save Plate overrides this from its own format "
                               "dropdown; it matters for Load and for reading "
                               "the path.",
                }),
                "create_dirs": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Make the folder now. Save Plate creates it "
                               "anyway — turn this on if a Load node or "
                               "another tool needs it to exist first.",
                }),
            },
            "optional": {
                "subpath": ("STRING", {
                    "default": "",
                    "tooltip": "Optional folder(s) under the browsed one, "
                               "e.g. comp/renders",
                }),
            },
        }

    @classmethod
    def IS_CHANGED(cls, folder, name, version_mode, version, version_pad,
                   version_style, frame_token, ext, create_dirs, subpath=""):
        # auto modes depend on what's on disk right now — never cache them,
        # or a second queue would silently rewrite the same version
        if version_mode.startswith("auto"):
            return float("nan")
        return "{}|{}|{}|{}|{}|{}|{}|{}".format(
            folder, subpath, name, version, version_pad, version_style,
            frame_token, ext)

    def build(self, folder, name, version_mode, version, version_pad,
              version_style, frame_token, ext, create_dirs, subpath=""):

        base = (folder or "").strip().replace("\\", "/").rstrip("/")
        if not base:
            raise ValueError("Path Builder: no folder set — press Browse.")

        sub = (subpath or "").strip().replace("\\", "/").strip("/")
        if sub:
            base = "{}/{}".format(base, sub)

        resolved = resolve_version(base, name, version_style,
                                   version_mode, version)
        full_path, out_folder = build_path(
            base, name, resolved, version_pad, version_style, frame_token, ext)

        if create_dirs:
            os.makedirs(out_folder, exist_ok=True)

        print("[EXR Plate] path builder -> {}".format(full_path))

        # tell the node UI what actually resolved
        try:
            from server import PromptServer
            PromptServer.instance.send_sync("exrplate.resolved", {
                "path": full_path,
                "version": resolved,
            })
        except Exception:
            pass

        return (full_path, out_folder, resolved)


NODE_CLASS_MAPPINGS = {"EXRPlatePathBuilder": EXRPlatePathBuilder}
NODE_DISPLAY_NAME_MAPPINGS = {"EXRPlatePathBuilder": "Path Builder (versioned)"}


# ---------------------------------------------------------------------------
# Endpoints for the Browse dialog + live version readout
# ---------------------------------------------------------------------------

try:
    from server import PromptServer
    from aiohttp import web

    @PromptServer.instance.routes.get("/exrplate/browse")
    async def _exrplate_browse(request):
        return web.json_response(list_dir(request.query.get("path", "").strip()))

    @PromptServer.instance.routes.get("/exrplate/build_preview")
    async def _exrplate_build_preview(request):
        q = request.query
        folder = q.get("folder", "").strip().replace("\\", "/").rstrip("/")
        sub = q.get("subpath", "").strip().replace("\\", "/").strip("/")
        if sub:
            folder = "{}/{}".format(folder, sub)
        name = q.get("name", "plate")
        style = q.get("style", "folder")
        mode = q.get("mode", "auto_next")
        try:
            manual = int(q.get("version", "1"))
            pad = int(q.get("pad", "3"))
        except ValueError:
            manual, pad = 1, 3

        latest = scan_version(folder, name, style)
        resolved = resolve_version(folder, name, style, mode, manual)
        full_path, out_folder = build_path(
            folder, name, resolved, pad, style,
            q.get("frame_token", "####"), q.get("ext", "exr"))

        return web.json_response({
            "latest": latest,
            "resolved": resolved,
            "path": full_path,
            "folder": out_folder,
            "exists": os.path.isdir(out_folder),
        })

except ImportError:
    pass  # running outside ComfyUI
