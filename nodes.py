"""
ComfyUI EXR Plate nodes — linear EXR in/out for Nuke roundtrips,
with VHS-style loading controls (frame cap, skip, every-nth, resize, fps).

Nodes:
  - Load EXR Plate : sequence loader with custom_width/height, frame_load_cap,
                     skip_first_frames, select_every_nth, colorspace transform,
                     exposure, fps passthrough for video nodes.
  - Save EXR Plate : half/float EXR writer, resize on save, inverse transforms.
  - Linear <-> sRGB converters for mid-graph sandwiches.

Colorspace = pure transfer-function math (sRGB / Rec.709), fully invertible.
EXR I/O via OpenCV (OPENCV_IO_ENABLE_OPENEXR).
"""

import os
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

import glob
import hashlib
import json as _json
import re
import uuid as _uuid
import gc

try:
    from urllib.request import Request as _Request, urlopen as _urlopen
except ImportError:
    _Request = _urlopen = None

try:
    import folder_paths  # ComfyUI runtime
except ImportError:
    folder_paths = None

import cv2
import numpy as np
import torch


# ---------------------------------------------------------------------------
# Transfer functions (match Nuke's sRGB / rec709 colorspaces)
# ---------------------------------------------------------------------------

def linear_to_srgb(x):
    x = np.ascontiguousarray(x, dtype=np.float32)
    lo = x <= 0.0031308
    out = np.clip(x, 0.0, None)
    np.power(out, 1.0 / 2.4, out=out)
    out *= 1.055
    out -= 0.055
    np.copyto(out, x * 12.92, where=lo)
    return out


def srgb_to_linear(x):
    x = np.ascontiguousarray(x, dtype=np.float32)
    lo = x <= 0.04045
    out = np.clip(x, 0.0, None)
    out += 0.055
    out /= 1.055
    np.power(out, 2.4, out=out)
    np.copyto(out, x / 12.92, where=lo)
    return out


def linear_to_rec709(x):
    x = np.ascontiguousarray(x, dtype=np.float32)
    lo = x < 0.018
    out = np.clip(x, 0.0, None)
    np.power(out, 0.45, out=out)
    out *= 1.099
    out -= 0.099
    np.copyto(out, x * 4.5, where=lo)
    return out


def rec709_to_linear(x):
    x = np.ascontiguousarray(x, dtype=np.float32)
    lo = x < 0.081
    out = np.clip(x, 0.0, None)
    out += 0.099
    out /= 1.099
    np.power(out, 1.0 / 0.45, out=out)
    np.copyto(out, x / 4.5, where=lo)
    return out


def linear_to_reinhard(x):
    """Invertible HDR tonemap: maps [0, inf) into [0, 1). Values above 1
    (superwhites) survive the 0-1 window and are exactly recovered by the
    inverse — use this pair when the plate has highlights above 1."""
    x = np.clip(x, 0.0, None)
    return (x / (1.0 + x)).astype(np.float32)


def reinhard_to_linear(x):
    x = np.clip(x, 0.0, 0.999999)  # guard the asymptote
    return (x / (1.0 - x)).astype(np.float32)


TRANSFORMS = {
    "keep linear (no conversion)": None,
    "linear -> sRGB": linear_to_srgb,
    "sRGB -> linear": srgb_to_linear,
    "linear -> Rec.709": linear_to_rec709,
    "Rec.709 -> linear": rec709_to_linear,
    "linear -> Reinhard (HDR-safe)": linear_to_reinhard,
    "Reinhard -> linear (HDR-safe)": reinhard_to_linear,
}
TRANSFORM_NAMES = list(TRANSFORMS.keys())


def _apply_transform(rgb, name):
    fn = TRANSFORMS.get(name)
    return fn(rgb) if fn else rgb


# ---------------------------------------------------------------------------
# Path / sequence helpers  (plate.####.exr or plate.%04d.exr)
# ---------------------------------------------------------------------------

_HASH_RE = re.compile(r"(#+)")
_PRINTF_RE = re.compile(r"%0?(\d+)d")


def _normalize_pattern(path):
    m = _HASH_RE.search(path)
    if m:
        pad = len(m.group(1))
        return _HASH_RE.sub("%0{}d".format(pad), path, count=1), pad
    m = _PRINTF_RE.search(path)
    if m:
        return path, int(m.group(1))
    return None, 0


def _resolve_frames(path, first_frame, last_frame):
    # relative path -> resolve against ComfyUI's input directory (uploads)
    if path and not os.path.isabs(path) and folder_paths is not None:
        path = os.path.join(folder_paths.get_input_directory(), path)

    # bare folder -> load every .exr inside, sorted
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.exr")))
        if not files:
            raise FileNotFoundError("No .exr files in folder: {}".format(path))
        return files

    pattern, _pad = _normalize_pattern(path)

    if pattern is None:
        if not os.path.isfile(path):
            raise FileNotFoundError("EXR not found: {}".format(path))
        return [path]

    if last_frame >= first_frame and last_frame > 0:
        files = [pattern % f for f in range(first_frame, last_frame + 1)]
        missing = [f for f in files if not os.path.isfile(f)]
        if missing:
            raise FileNotFoundError(
                "Missing {} frame(s), first: {}".format(len(missing), missing[0]))
        return files

    glob_pat = _PRINTF_RE.sub("*", pattern, count=1)
    files = sorted(glob.glob(glob_pat))
    if not files:
        raise FileNotFoundError("No frames match: {}".format(glob_pat))
    return files


def _read_exr(filepath):
    img = cv2.imread(filepath, cv2.IMREAD_UNCHANGED | cv2.IMREAD_ANYDEPTH)
    if img is None:
        raise IOError("OpenCV failed to read: {}".format(filepath))
    img = img.astype(np.float32)

    if img.ndim == 2:
        return np.stack([img] * 3, axis=-1), None
    if img.shape[2] == 3:
        return img[:, :, ::-1].copy(), None
    if img.shape[2] == 4:
        return img[:, :, 2::-1].copy(), img[:, :, 3].copy()
    raise IOError("Unsupported channel count {} in {}".format(
        img.shape[2], filepath))


def _target_size(src_w, src_h, custom_w, custom_h):
    """VHS-style: 0 = keep. One value set -> keep aspect."""
    if custom_w <= 0 and custom_h <= 0:
        return src_w, src_h
    if custom_w > 0 and custom_h > 0:
        return custom_w, custom_h
    if custom_w > 0:
        return custom_w, max(1, int(round(src_h * custom_w / src_w)))
    return max(1, int(round(src_w * custom_h / src_h))), custom_h


# ---------------------------------------------------------------------------
# Load EXR Plate
# ---------------------------------------------------------------------------

class LoadEXRPlate:
    CATEGORY = "EXR Plate"
    RETURN_TYPES = ("IMAGE", "MASK", "INT", "FLOAT", "VHS_VIDEOINFO")
    RETURN_NAMES = ("image", "alpha", "frame_count", "fps", "video_info")
    FUNCTION = "load"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "path": ("STRING", {
                    "default": "",
                    "tooltip": "Single .exr, or sequence with #### / %04d. "
                               "e.g. /plates/END_130.####.exr",
                }),
                "first_frame": ("INT", {"default": 1, "min": 0, "max": 999999}),
                "last_frame": ("INT", {
                    "default": 0, "min": 0, "max": 999999,
                    "tooltip": "0 = auto (glob all frames, or single file)",
                }),
                "fps": ("FLOAT", {
                    "default": 25.0, "min": 0.1, "max": 240.0, "step": 0.01,
                    "tooltip": "Passthrough only (EXRs carry no rate) — wire "
                               "the fps output into Video Combine etc.",
                }),
                "custom_width": ("INT", {
                    "default": 0, "min": 0, "max": 16384, "step": 8,
                    "tooltip": "0 = source width. Set one of width/height to "
                               "keep aspect.",
                }),
                "custom_height": ("INT", {
                    "default": 0, "min": 0, "max": 16384, "step": 8,
                    "tooltip": "0 = source height.",
                }),
                "frame_load_cap": ("INT", {
                    "default": 0, "min": 0, "max": 999999,
                    "tooltip": "Max frames to load after skip/nth. 0 = all.",
                }),
                "skip_first_frames": ("INT", {
                    "default": 0, "min": 0, "max": 999999,
                }),
                "select_every_nth": ("INT", {
                    "default": 1, "min": 1, "max": 1000,
                }),
                "colorspace": (TRANSFORM_NAMES,),
                "exposure": ("FLOAT", {
                    "default": 0.0, "min": -10.0, "max": 10.0, "step": 0.1,
                    "tooltip": "Stops, applied in linear before conversion",
                }),
                "clamp_output": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Clamp 0-1 after conversion (needed by most "
                               "models). Off = HDR passthrough.",
                }),
            }
        }

    def load(self, path, first_frame, last_frame, fps, custom_width,
             custom_height, frame_load_cap, skip_first_frames,
             select_every_nth, colorspace, exposure, clamp_output):
        files = _resolve_frames(path.strip(), first_frame, last_frame)

        # VHS-style frame selection
        files = files[skip_first_frames:]
        files = files[::max(1, select_every_nth)]
        if frame_load_cap > 0:
            files = files[:frame_load_cap]
        if not files:
            raise ValueError("Frame selection left 0 frames to load.")

        total_source_frames = len(_resolve_frames(
            path.strip(), first_frame, last_frame))
        gain = float(2.0 ** exposure)
        n = len(files)
        image_batch = None
        alpha_batch = None
        source_wh = None

        # Frames are written straight into one preallocated batch tensor.
        # Accumulating a list and stacking at the end doubles peak system RAM,
        # which is what kills long 2K+ plates.
        for i, f in enumerate(files):
            rgb, alpha = _read_exr(f)

            if exposure != 0.0:
                rgb *= gain

            h, w = rgb.shape[:2]
            if source_wh is None:
                source_wh = (w, h)
            tw, th = _target_size(w, h, custom_width, custom_height)
            if (tw, th) != (w, h):
                interp = cv2.INTER_AREA if tw < w else cv2.INTER_LANCZOS4
                rgb = cv2.resize(rgb, (tw, th), interpolation=interp)
                if alpha is not None:
                    alpha = cv2.resize(alpha, (tw, th), interpolation=interp)

            rgb = _apply_transform(rgb, colorspace)
            if clamp_output:
                np.clip(rgb, 0.0, 1.0, out=rgb)

            fh, fw = rgb.shape[:2]
            if image_batch is None:
                image_batch = torch.empty((n, fh, fw, 3), dtype=torch.float32)
                alpha_batch = torch.empty((n, fh, fw), dtype=torch.float32)
            elif (fh, fw) != tuple(image_batch.shape[1:3]):
                raise ValueError(
                    "Frame size mismatch: {} is {}x{}, expected {}x{}".format(
                        f, fw, fh,
                        int(image_batch.shape[2]), int(image_batch.shape[1])))

            image_batch[i] = torch.from_numpy(np.ascontiguousarray(rgb))
            if alpha is None:
                alpha_batch[i] = 1.0
            else:
                alpha_batch[i] = torch.from_numpy(
                    np.ascontiguousarray(np.clip(alpha, 0.0, 1.0)))

            del rgb, alpha
            if (i % 32) == 0:
                gc.collect()

        gc.collect()

        loaded_fps = float(fps) / max(1, select_every_nth)
        lh, lw = image_batch.shape[1:3]
        video_info = {
            "source_fps": float(fps),
            "source_frame_count": total_source_frames,
            "source_duration": total_source_frames / float(fps),
            "source_width": source_wh[0],
            "source_height": source_wh[1],
            "loaded_fps": loaded_fps,
            "loaded_frame_count": len(files),
            "loaded_duration": len(files) / loaded_fps,
            "loaded_width": int(lw),
            "loaded_height": int(lh),
        }

        return (image_batch, alpha_batch, len(files),
                float(fps), video_info)

    @classmethod
    def IS_CHANGED(cls, path, first_frame, last_frame, **kwargs):
        try:
            files = _resolve_frames(path.strip(), first_frame, last_frame)
            h = hashlib.sha256()
            for f in files:
                st = os.stat(f)
                h.update("{}:{}:{}".format(f, st.st_mtime, st.st_size).encode())
            return h.hexdigest()
        except Exception:
            return float("nan")


# ---------------------------------------------------------------------------
# Save EXR Plate
# ---------------------------------------------------------------------------

class SaveEXRPlate:
    CATEGORY = "EXR Plate"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("written_path",)
    FUNCTION = "save"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "path": ("STRING", {
                    "default": "",
                    "tooltip": "Output path with #### / %04d for sequences. "
                               "e.g. /out/END_130_comp.####.exr",
                }),
                "first_frame": ("INT", {"default": 1, "min": 0, "max": 999999}),
                "custom_width": ("INT", {
                    "default": 0, "min": 0, "max": 16384, "step": 8,
                    "tooltip": "Resize on save. 0 = keep. One value set = "
                               "keep aspect.",
                }),
                "custom_height": ("INT", {
                    "default": 0, "min": 0, "max": 16384, "step": 8,
                }),
                "format": (["exr", "tiff (16-bit)", "tiff (8-bit)",
                            "png (16-bit)", "png (8-bit)", "jpg"], {
                    "tooltip": "EXR = float, keeps HDR values above 1. "
                               "tiff/png/jpg are integer formats: values are "
                               "clamped to 0-1 on write. jpg has no alpha.",
                }),
                "colorspace": (TRANSFORM_NAMES, {
                    "default": "sRGB -> linear",
                    "tooltip": "Inverse of whatever the load applied, so the "
                               "plate lands back in linear for Nuke.",
                }),
                "bit_depth": (["half (16-bit)", "float (32-bit)"], {
                    "tooltip": "EXR only; ignored for other formats.",
                }),
            },
            "optional": {
                "alpha": ("MASK",),
                "send_to_nuke": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "After writing, push the result to Nuke as a "
                               "Read node (needs the Samuelsujan bridge "
                               "listener running in Nuke).",
                }),
                "nuke_host": ("STRING", {
                    "default": "http://127.0.0.1:8189",
                    "tooltip": "Nuke bridge listener address.",
                }),
            },
        }

    FORMAT_MAP = {
        "exr": (".exr", "exr"),
        "tiff (16-bit)": (".tif", "u16"),
        "tiff (8-bit)": (".tif", "u8"),
        "png (16-bit)": (".png", "u16"),
        "png (8-bit)": (".png", "u8"),
        "jpg": (".jpg", "jpg"),
    }

    def save(self, images, path, first_frame, custom_width, custom_height,
             format, colorspace, bit_depth, alpha=None,
             send_to_nuke=False, nuke_host="http://127.0.0.1:8189"):
        path = path.strip().replace("\\", "/")
        ext, mode = self.FORMAT_MAP[format]
        batch = images.shape[0]

        # bare folder (existing dir, trailing slash, or no extension)
        # -> auto-name the files inside it
        if (os.path.isdir(path) or path.endswith("/")
                or os.path.splitext(path)[1] == ""):
            stem = "plate.####" + ext if batch > 1 else "plate" + ext
            path = os.path.join(path, stem).replace("\\", "/")
        else:
            base, _old_ext = os.path.splitext(path)
            path = base + ext  # extension always follows the format dropdown

        pattern, _pad = _normalize_pattern(path)

        # multiple frames but no padding -> insert it instead of failing
        if pattern is None and batch > 1:
            base, e = os.path.splitext(path)
            path = base + ".####" + e
            pattern, _pad = _normalize_pattern(path)

        out_dir = os.path.dirname(pattern or path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        if mode == "exr":
            exr_type = (cv2.IMWRITE_EXR_TYPE_HALF
                        if bit_depth.startswith("half")
                        else cv2.IMWRITE_EXR_TYPE_FLOAT)
            params = [cv2.IMWRITE_EXR_TYPE, exr_type]
            if hasattr(cv2, "IMWRITE_EXR_COMPRESSION"):
                params += [cv2.IMWRITE_EXR_COMPRESSION,
                           getattr(cv2, "IMWRITE_EXR_COMPRESSION_ZIP", 3)]
        elif mode == "jpg":
            params = [cv2.IMWRITE_JPEG_QUALITY, 95]
        else:
            params = []

        written = []
        for i in range(batch):
            rgb = images[i].cpu().numpy().astype(np.float32)
            rgb = _apply_transform(rgb, colorspace)

            a = None
            if alpha is not None and i < alpha.shape[0]:
                a = alpha[i].cpu().numpy().astype(np.float32)

            h, w = rgb.shape[:2]
            tw, th = _target_size(w, h, custom_width, custom_height)
            if (tw, th) != (w, h):
                interp = cv2.INTER_AREA if tw < w else cv2.INTER_LANCZOS4
                rgb = cv2.resize(rgb, (tw, th), interpolation=interp)
                if a is not None:
                    a = cv2.resize(a, (tw, th), interpolation=interp)

            out = rgb[:, :, ::-1]  # RGB -> BGR
            if mode != "jpg" and a is not None and a.shape == out.shape[:2]:
                out = np.dstack([out, a])

            if mode == "u16":
                out = (np.clip(out, 0.0, 1.0) * 65535.0 + 0.5).astype(np.uint16)
            elif mode in ("u8", "jpg"):
                out = (np.clip(out, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
            # exr stays float32

            fpath = (pattern % (first_frame + i)) if pattern else path
            if not cv2.imwrite(fpath, np.ascontiguousarray(out), params):
                raise IOError("Failed to write: {}".format(fpath))
            written.append(fpath)

        if send_to_nuke and _urlopen is not None:
            try:
                payload = _json.dumps({
                    "path": pattern or path,
                    "first_frame": first_frame,
                    "last_frame": first_frame + batch - 1,
                }).encode("utf-8")
                req = _Request(
                    nuke_host.rstrip("/") + "/from_comfy", data=payload,
                    headers={"Content-Type": "application/json"})
                _urlopen(req, timeout=5)
                print("[EXR Plate] result sent to Nuke")
            except Exception as exc:
                print("[EXR Plate] send to Nuke failed "
                      "(is Nuke open with the bridge loaded?):", exc)

        return (written[0] if len(written) == 1
                else "{} .. {} ({} frames)".format(
                    written[0], written[-1], len(written)),)


# ---------------------------------------------------------------------------
# Standalone converters
# ---------------------------------------------------------------------------

class _ConvertBase:
    CATEGORY = "EXR Plate"
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "convert"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"images": ("IMAGE",)}}


class LinearToSRGB(_ConvertBase):
    def convert(self, images):
        arr = images.cpu().numpy().astype(np.float32)
        return (torch.from_numpy(np.clip(linear_to_srgb(arr), 0.0, 1.0)),)


class SRGBToLinear(_ConvertBase):
    def convert(self, images):
        arr = images.cpu().numpy().astype(np.float32)
        return (torch.from_numpy(srgb_to_linear(arr)),)


NODE_CLASS_MAPPINGS = {
    "LoadEXRPlate": LoadEXRPlate,
    "SaveEXRPlate": SaveEXRPlate,
    "LinearToSRGB_Plate": LinearToSRGB,
    "SRGBToLinear_Plate": SRGBToLinear,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LoadEXRPlate": "Load EXR Plate (linear)",
    "SaveEXRPlate": "Save Plate (linear)",
    "LinearToSRGB_Plate": "Linear \u2192 sRGB",
    "SRGBToLinear_Plate": "sRGB \u2192 Linear",
}


# ---------------------------------------------------------------------------
# Live preview endpoint (serves first frame of `path` as sRGB PNG)
# ---------------------------------------------------------------------------

try:
    from server import PromptServer
    from aiohttp import web

    @PromptServer.instance.routes.post("/exrplate/from_nuke")
    async def _exrplate_from_nuke(request):
        """Nuke pushes a plate path here; we forward it to the live canvas."""
        try:
            data = await request.json()
            path = str(data.get("path", "")).strip()
            if not path:
                raise ValueError("empty path")
            PromptServer.instance.send_sync("exrplate.set_path", {
                "path": path,
                "first_frame": int(data.get("first_frame", 1)),
                "last_frame": int(data.get("last_frame", 0)),
                "width": int(data.get("width", 0)),
                "height": int(data.get("height", 0)),
                "fps": float(data.get("fps", 0) or 0),
                "colorspace": str(data.get("colorspace", "")),
            })
            return web.json_response({"ok": True})
        except Exception as exc:
            return web.Response(status=400, text=str(exc))

    @PromptServer.instance.routes.get("/exrplate/info")
    async def _exrplate_info(request):
        path = request.query.get("path", "").strip()
        try:
            files = _resolve_frames(path, 0, 0)
            rgb, _alpha = _read_exr(files[0])
            h, w = rgb.shape[:2]
            return web.json_response({
                "frame_count": len(files),
                "width": int(w),
                "height": int(h),
            })
        except Exception as exc:
            return web.Response(status=404, text=str(exc))

    @PromptServer.instance.routes.get("/exrplate/preview")
    async def _exrplate_preview(request):
        path = request.query.get("path", "").strip()
        try:
            idx = int(request.query.get("frame", "0"))
            req_w = int(request.query.get("w", "2048"))
            req_w = max(64, min(req_w, 4096))
            files = _resolve_frames(path, 0, 0)
            idx = max(0, min(idx, len(files) - 1))
            rgb, _alpha = _read_exr(files[idx])
            try:
                exposure = float(request.query.get("exposure", "0"))
            except ValueError:
                exposure = 0.0
            if exposure:
                rgb = rgb * float(2.0 ** exposure)
            cs = request.query.get("colorspace", "")
            fn = TRANSFORMS.get(cs, linear_to_srgb)
            if fn is not None:
                rgb = fn(rgb)
            rgb = np.clip(rgb, 0.0, 1.0)
            h, w = rgb.shape[:2]
            if w > req_w:
                rgb = cv2.resize(
                    rgb, (req_w, max(1, int(round(h * float(req_w) / w)))),
                    interpolation=cv2.INTER_AREA)
            # JPEG for animation frames (fast decode), PNG for the still
            as_jpg = request.query.get("fmt", "png") == "jpg"
            ext = ".jpg" if as_jpg else ".png"
            ok, buf = cv2.imencode(
                ext,
                np.ascontiguousarray(
                    (rgb[:, :, ::-1] * 255.0 + 0.5).astype(np.uint8)),
                [cv2.IMWRITE_JPEG_QUALITY, 90] if as_jpg else [])
            if not ok:
                raise IOError("encode failed")
            return web.Response(
                body=buf.tobytes(),
                content_type="image/jpeg" if as_jpg else "image/png")
        except Exception as exc:
            return web.Response(status=404, text=str(exc))
except ImportError:
    pass  # running outside ComfyUI
