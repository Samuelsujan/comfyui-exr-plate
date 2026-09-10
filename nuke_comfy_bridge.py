"""
Samuelsujan's Nuke -> ComfyUI bridge.
Author: Gunupudi Samuel Sujan Deep (Samuelsujan)

Bakes the selected node to a linear EXR sequence, injects the paths into a
ComfyUI workflow (API format) that uses the comfyui-exr-plate nodes, submits
it over the ComfyUI HTTP API, waits for completion, and imports the result
back as a Read node.

Requires on the ComfyUI side:
  - ComfyUI running locally (default http://127.0.0.1:8188)
  - the comfyui-exr-plate custom node pack (LoadEXRPlate / SaveEXRPlate)
  - a workflow saved in **API format** (Comfy: enable dev mode ->
    "Save (API Format)") containing at least one LoadEXRPlate node and
    one SaveEXRPlate node. The bridge overwrites their path/frame inputs.

Install:
  1. Copy this file to ~/.nuke/nuke_comfy_bridge.py
  2. In ~/.nuke/menu.py add:

       import nuke_comfy_bridge
       nuke.menu("Nuke").addCommand(
           "Samuelsujan/Send Plate to Load EXR Plate",
           "nuke_comfy_bridge.send_plate()", "F9")
       nuke.menu("Nuke").addCommand(
           "Samuelsujan/Run Workflow on Selected (full bridge)",
           "nuke_comfy_bridge.run_bridge()", "F10")

  3. Select a node, press F9.
"""

import datetime
import json
import os
import re
import tempfile
import threading
import time
import uuid

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
except ImportError:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

try:  # Nuke 13+ (Python 3)
    from urllib.request import Request, urlopen
    from urllib.error import URLError
except ImportError:  # Python 2 fallback
    from urllib2 import Request, urlopen, URLError

import nuke

DEFAULT_HOST = "http://127.0.0.1:8188"
POLL_INTERVAL = 1.0   # seconds
TIMEOUT = 3600        # give up after an hour

# remembered between runs (per Nuke session)
_LAST = {
    "workflow": "",
    "host": DEFAULT_HOST,
    "out_dir": "",
}


# ---------------------------------------------------------------------------
# Bake selected node to linear EXR
# ---------------------------------------------------------------------------

def _bake_to_exr(node, out_dir, first, last):
    """Render `node` to <out_dir>/bake.%04d.exr as 16-bit half, linear."""
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    path = os.path.join(out_dir, "bake.%04d.exr").replace("\\", "/")

    write = nuke.nodes.Write(inputs=[node])
    try:
        write["file"].setValue(path)
        write["file_type"].setValue("exr")
        write["channels"].setValue("rgba")
        try:
            write["datatype"].setValue("16 bit half")
            write["compression"].setValue("Zip (1 scanline)")
        except Exception:
            pass
        try:
            # EXR out of Nuke is linear by default; pin it explicitly
            write["colorspace"].setValue("linear")
        except Exception:
            pass  # OCIO configs name it differently; default is still linear for EXR

        nuke.execute(write, first, last, 1)
    finally:
        nuke.delete(write)

    return path


# ---------------------------------------------------------------------------
# Versioning (same rules as the Path Builder node)
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"(?:^|[^a-zA-Z0-9])v(\d+)(?:$|[^0-9])", re.IGNORECASE)
_SEQ_RE = re.compile(r"^(.*?)(\d+)(\.[A-Za-z0-9]+)$")


def _next_version(folder, pad=3):
    """v001, v002, ... — highest existing version folder + 1."""
    highest = 0
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            if not os.path.isdir(os.path.join(folder, name)):
                continue
            m = _VERSION_RE.search(name)
            if m:
                highest = max(highest, int(m.group(1)))
    return "v{}".format(str(highest + 1).zfill(pad))


def _find_sequence(root_dir):
    """Most recently written sequence under root_dir.

    Used when the workflow's Save path comes from a Path Builder node — the
    bridge doesn't know the version it picked, so it finds what landed.
    Returns (printf_pattern, first, last) or None.
    """
    best = None
    for dirpath, _dirs, files in os.walk(root_dir):
        groups = {}
        for f in files:
            m = _SEQ_RE.match(f)
            if not m:
                continue
            stem, num, ext = m.groups()
            groups.setdefault((stem, len(num), ext), []).append(int(num))
        for (stem, pad, ext), nums in groups.items():
            nums.sort()
            pattern = os.path.join(
                dirpath, "{}%0{}d{}".format(stem, pad, ext)).replace("\\", "/")
            try:
                mtime = os.path.getmtime(pattern % nums[-1])
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, pattern, nums[0], nums[-1])
    return None if best is None else (best[1], best[2], best[3])


# ---------------------------------------------------------------------------
# Workflow injection
# ---------------------------------------------------------------------------

def _is_link(value):
    """API-format inputs are either a literal or [node_id, slot]."""
    return isinstance(value, list)


def _inject_paths(workflow, in_path, first, last, out_path):
    """Point every LoadEXRPlate / SaveEXRPlate node at our paths.

    A path input driven by a Path Builder node is a link, not a literal —
    overwriting it would break the graph. Those get redirected by setting the
    builder's `folder` instead, and the builder keeps doing the versioning.
    Returns True if any path was left to a builder.
    """
    n_load = n_save = 0
    out_dir = os.path.dirname(out_path).replace("\\", "/")
    in_dir = os.path.dirname(in_path).replace("\\", "/")
    builder_targets = {}   # node id -> folder to point it at

    for node_id, node in workflow.items():
        ct = node.get("class_type", "")
        inputs = node.setdefault("inputs", {})
        if ct == "LoadEXRPlate":
            src = inputs.get("path")
            if _is_link(src):
                builder_targets[str(src[0])] = in_dir
            else:
                inputs["path"] = in_path
            inputs["first_frame"] = first
            inputs["last_frame"] = last
            n_load += 1
        elif ct == "SaveEXRPlate":
            dst = inputs.get("path")
            if _is_link(dst):
                builder_targets[str(dst[0])] = out_dir
            else:
                inputs["path"] = out_path
            inputs["first_frame"] = first
            n_save += 1

    for node_id, folder in builder_targets.items():
        node = workflow.get(node_id) or workflow.get(int(node_id), None)
        if not node or node.get("class_type") != "EXRPlatePathBuilder":
            raise ValueError(
                "A path input is wired from node {} ({}), which the bridge "
                "can't redirect. Wire it from a Path Builder node, or set the "
                "path as a plain widget value.".format(
                    node_id, node.get("class_type", "?") if node else "missing"))
        node.setdefault("inputs", {})["folder"] = folder
        node["inputs"]["subpath"] = ""

    if not n_load or not n_save:
        raise ValueError(
            "Workflow needs at least one LoadEXRPlate and one SaveEXRPlate "
            "node (found {} / {}). Save it in API format.".format(n_load, n_save))

    return bool(builder_targets)


# ---------------------------------------------------------------------------
# ComfyUI HTTP API
# ---------------------------------------------------------------------------

def _http_json(url, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = Request(url, data=data,
                  headers={"Content-Type": "application/json"})
    resp = urlopen(req, timeout=30)
    return json.loads(resp.read().decode("utf-8"))


def _submit(host, workflow):
    body = {"prompt": workflow, "client_id": "nuke-bridge-" + uuid.uuid4().hex}
    result = _http_json(host.rstrip("/") + "/prompt", body)
    if "prompt_id" not in result:
        raise RuntimeError("Unexpected ComfyUI response: {}".format(result))
    return result["prompt_id"]


def _wait(host, prompt_id):
    """Poll /history until the job lands. Returns the history entry."""
    task = nuke.ProgressTask("ComfyUI")
    task.setMessage("Waiting for ComfyUI...")
    start = time.time()
    try:
        while True:
            if task.isCancelled():
                raise RuntimeError("Cancelled by user (job may still be "
                                   "running in ComfyUI).")
            if time.time() - start > TIMEOUT:
                raise RuntimeError("Timed out after {}s.".format(TIMEOUT))

            hist = _http_json(host.rstrip("/") + "/history/" + prompt_id)
            entry = hist.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    msgs = status.get("messages", [])
                    detail = ""
                    for m in msgs:
                        if m and m[0] == "execution_error":
                            detail = m[1].get("exception_message", "")
                    raise RuntimeError("ComfyUI execution error: " + detail)
                if status.get("completed", True):
                    return entry

            elapsed = int(time.time() - start)
            task.setMessage("Rendering in ComfyUI... {}s".format(elapsed))
            task.setProgress(min(95, elapsed % 100))
            time.sleep(POLL_INTERVAL)
    finally:
        del task


# ---------------------------------------------------------------------------
# Result import
# ---------------------------------------------------------------------------

def _import_read(out_path, first, last, near_node=None):
    missing = [f for f in range(first, last + 1)
               if not os.path.isfile(out_path % f)]
    if missing:
        raise RuntimeError(
            "ComfyUI finished but {} output frame(s) missing, first: {}"
            .format(len(missing), out_path % missing[0]))

    read = nuke.createNode("Read", inpanel=False)
    read["file"].setValue(out_path.replace("\\", "/"))
    read["first"].setValue(first)
    read["last"].setValue(last)
    read["origfirst"].setValue(first)
    read["origlast"].setValue(last)
    try:
        read["colorspace"].setValue("linear")
    except Exception:
        pass
    if near_node is not None:
        read.setXYpos(near_node.xpos() + 120, near_node.ypos() + 60)
    read["label"].setValue("ComfyUI result\nSamuelsujan bridge")
    return read


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_bridge():
    try:
        node = nuke.selectedNode()
    except ValueError:
        nuke.message("Select a node to send first.")
        return

    root = nuke.root()
    p = nuke.Panel("Send to ComfyUI  -  Samuelsujan")
    p.addFilenameSearch("Workflow JSON (API format)", _LAST["workflow"])
    p.addSingleLineInput("ComfyUI host", _LAST["host"])
    p.addSingleLineInput("Frame range",
                         "{}-{}".format(int(root["first_frame"].value()),
                                        int(root["last_frame"].value())))
    p.addSingleLineInput("Output dir (blank = temp)", _LAST["out_dir"])
    p.addBooleanCheckBox("Versioned output (v001, v002...)",
                         _LAST.get("versioned", True))
    if not p.show():
        return

    workflow_path = p.value("Workflow JSON (API format)").strip()
    host = p.value("ComfyUI host").strip() or DEFAULT_HOST
    rng = p.value("Frame range").strip()
    out_dir = p.value("Output dir (blank = temp)").strip()
    versioned = bool(p.value("Versioned output (v001, v002...)"))

    try:
        first, last = [int(x) for x in rng.replace(" ", "").split("-")]
    except Exception:
        nuke.message("Frame range must look like: 1-55")
        return

    if not os.path.isfile(workflow_path):
        nuke.message("Workflow JSON not found:\n" + workflow_path)
        return
    with open(workflow_path) as fh:
        workflow = json.load(fh)
    if "nodes" in workflow and isinstance(workflow.get("nodes"), list):
        nuke.message("That's the editor/save format. Re-export with "
                     "'Save (API Format)' (enable dev mode in Comfy settings).")
        return

    _LAST.update(workflow=workflow_path, host=host, out_dir=out_dir,
                 versioned=versioned)

    base = out_dir or os.path.join(tempfile.gettempdir(), "nuke_comfy_bridge")
    if versioned:
        # <base>/<node>/v003/{in,out} — same v### convention as the
        # Path Builder node, so both routes land in one readable tree
        shot_dir = os.path.join(base, node.name())
        job_dir = os.path.join(shot_dir, _next_version(shot_dir))
    else:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        job_dir = os.path.join(base, "{}_{}".format(node.name(), stamp))
    in_dir = os.path.join(job_dir, "in")
    result_dir = os.path.join(job_dir, "out")
    if not os.path.isdir(result_dir):
        os.makedirs(result_dir)
    out_path = os.path.join(result_dir, "result.%04d.exr").replace("\\", "/")

    try:
        in_path = _bake_to_exr(node, in_dir, first, last)
        by_builder = _inject_paths(workflow, in_path, first, last, out_path)
        prompt_id = _submit(host, workflow)
        _wait(host, prompt_id)

        if by_builder:
            # the builder chose the version, so find what actually landed
            found = _find_sequence(result_dir)
            if not found:
                raise RuntimeError(
                    "ComfyUI finished but nothing was written under:\n"
                    + result_dir)
            pattern, r_first, r_last = found
            _import_read(pattern, r_first, r_last, near_node=node)
            nuke.message("Done - {} frames imported from:\n{}".format(
                r_last - r_first + 1, pattern))
        else:
            _import_read(out_path, first, last, near_node=node)
            nuke.message("Done - {} frames imported.".format(last - first + 1))
    except URLError:
        nuke.message("Can't reach ComfyUI at {}\nIs it running?".format(host))
    except Exception as exc:
        nuke.message("Bridge failed:\n{}".format(exc))


# ---------------------------------------------------------------------------
# Quick push: selected plate -> Load EXR Plate node on the live Comfy canvas
# ---------------------------------------------------------------------------

def send_plate():
    """Send the selected node's EXR path straight into the Load EXR Plate
    node in the open ComfyUI browser tab. Reads with an .exr file are sent
    as-is (no bake); anything else is baked to a temp linear EXR first."""
    try:
        node = nuke.selectedNode()
    except ValueError:
        nuke.message("Select a Read (or any node) first.")
        return

    host = (_LAST.get("host") or DEFAULT_HOST).rstrip("/")

    is_exr_read = (
        node.Class() == "Read"
        and node["file"].value().lower().endswith(".exr")
    )
    if is_exr_read:
        path = node["file"].value()
        first = int(node["first"].value())
        last = int(node["last"].value())
    else:
        root = nuke.root()
        first = int(root["first_frame"].value())
        last = int(root["last_frame"].value())
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        bake_dir = os.path.join(
            tempfile.gettempdir(), "nuke_comfy_bridge",
            "{}_{}".format(node.name(), stamp), "in")
        if not nuke.ask(
            "{} isn't an EXR Read.\nBake frames {}-{} to a temp EXR and "
            "send that?".format(node.name(), first, last)):
            return
        path = _bake_to_exr(node, bake_dir, first, last)

    # image resolution + project rate, so ComfyUI can fill the widgets
    try:
        fmt = node.format()
        width, height = int(fmt.width()), int(fmt.height())
    except Exception:
        try:
            root_fmt = nuke.root()["format"].value()
            width, height = int(root_fmt.width()), int(root_fmt.height())
        except Exception:
            width = height = 0
    # prefer the plate's own rate from metadata (input/frame_rate), since the
    # project rate is often different from the footage rate
    fps = None
    for key in ("input/frame_rate", "quicktime/fps", "exr/framesPerSecond"):
        try:
            val = node.metadata(key)
            if val:
                fps = round(float(val), 4)
                break
        except Exception:
            continue
    if not fps:
        try:
            fps = float(nuke.root()["fps"].value())
        except Exception:
            fps = 25.0

    # how Nuke is interpreting the file, so ComfyUI can pick the transform
    colorspace = ""
    try:
        colorspace = str(node["colorspace"].value())
    except Exception:
        if is_exr_read:
            colorspace = "linear"

    payload = json.dumps({
        "path": path,
        "first_frame": first,
        "last_frame": last,
        "width": width,
        "height": height,
        "fps": fps,
        "colorspace": colorspace,
    }).encode("utf-8")
    req = Request(host + "/exrplate/from_nuke", data=payload,
                  headers={"Content-Type": "application/json"})
    try:
        urlopen(req, timeout=10)
    except URLError:
        nuke.message("Can't reach ComfyUI at {}\nIs it running?".format(host))
        return
    except Exception as exc:
        nuke.message("Send failed:\n{}".format(exc))
        return
    nuke.message("Plate sent to ComfyUI:\n{}\nframes {}-{}".format(
        path, first, last))


# ---------------------------------------------------------------------------
# Listener: ComfyUI pushes finished renders here -> Read node appears in Nuke
# ---------------------------------------------------------------------------

LISTENER_PORT = 8189
_LISTENER = {"server": None}


def _comfy_result_read(path, first, last):
    """Runs on Nuke's main thread: create a Read for the pushed result."""
    read = nuke.createNode("Read", inpanel=False)
    read["file"].setValue(path.replace("\\", "/"))
    if "%0" in path or "#" in path:
        read["first"].setValue(first)
        read["last"].setValue(last)
        read["origfirst"].setValue(first)
        read["origlast"].setValue(last)
    if path.lower().endswith(".exr"):
        try:
            read["colorspace"].setValue("linear")
        except Exception:
            pass
    read["label"].setValue("from ComfyUI\nSamuelsujan bridge")


class _BridgeHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/from_comfy":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            path = str(data.get("path", "")).strip()
            first = int(data.get("first_frame", 1))
            last = int(data.get("last_frame", first))
            if not path:
                raise ValueError("empty path")
            nuke.executeInMainThread(
                _comfy_result_read, args=(path, first, last))
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            msg = str(exc).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def log_message(self, *args):
        pass  # keep the script editor quiet


def start_listener(port=LISTENER_PORT):
    """Start the ComfyUI -> Nuke listener (idempotent)."""
    if _LISTENER["server"] is not None:
        return
    try:
        server = HTTPServer(("127.0.0.1", port), _BridgeHandler)
    except OSError as exc:
        nuke.tprint("[Samuelsujan bridge] listener not started "
                    "(port {} busy?): {}".format(port, exc))
        return
    _LISTENER["server"] = server
    t = threading.Thread(target=server.serve_forever)
    t.daemon = True
    t.start()
    nuke.tprint("[Samuelsujan bridge] listening for ComfyUI results "
                "on 127.0.0.1:{}".format(port))


# auto-start when Nuke loads the bridge via menu.py
try:
    start_listener()
except Exception as _exc:
    nuke.tprint("[Samuelsujan bridge] listener failed: {}".format(_exc))
