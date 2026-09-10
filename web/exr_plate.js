import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const CREDIT = "Samuelsujan";
const CREDIT_URL =
  "https://www.linkedin.com/in/gunupudi-samuel-sujandeep-a316bb225/";
const EXR_NODES = new Set([
  "LoadEXRPlate",
  "SaveEXRPlate",
  "LinearToSRGB_Plate",
  "SRGBToLinear_Plate",
  "EXRPlatePathBuilder",
]);
const ANIM_W = 640;      // playback frame width (memory-friendly)
const MAX_ANIM_FRAMES = 300;

function previewUrl(path, frame, w, fmt, colorspace, exposure) {
  return api.apiURL(
    "/exrplate/preview?path=" + encodeURIComponent(path) +
    "&frame=" + frame + "&w=" + w + "&fmt=" + fmt +
    "&colorspace=" + encodeURIComponent(colorspace ?? "") +
    "&exposure=" + encodeURIComponent(exposure ?? 0) +
    "&t=" + Date.now()
  );
}

app.registerExtension({
  name: "exrplate.ui",
  setup() {
    // Nuke pushed a plate: fill (or create) a Load EXR Plate node live
    api.addEventListener("exrplate.set_path", ({ detail }) => {
      if (!detail?.path) return;
      let target = null;
      const sel = app.canvas?.selected_nodes || {};
      for (const id in sel) {
        if (sel[id]?.comfyClass === "LoadEXRPlate") { target = sel[id]; break; }
      }
      if (!target) {
        target = app.graph._nodes?.find(
          (n) => n.comfyClass === "LoadEXRPlate");
      }
      if (!target && window.LiteGraph) {
        target = window.LiteGraph.createNode("LoadEXRPlate");
        if (!target) return;
        app.graph.add(target);
        const ds = app.canvas.ds;
        target.pos = [
          -ds.offset[0] + app.canvas.canvas.width / ds.scale / 2 - 220,
          -ds.offset[1] + app.canvas.canvas.height / ds.scale / 2 - 250,
        ];
      }
      if (!target) return;
      const set = (name, val) => {
        const w = target.widgets?.find((w) => w.name === name);
        if (w) w.value = val;
      };
      set("first_frame", detail.first_frame ?? 1);
      set("last_frame", detail.last_frame ?? 0);
      if (detail.width > 0) set("custom_width", detail.width);
      if (detail.height > 0) set("custom_height", detail.height);
      if (detail.fps > 0) set("fps", Math.round(detail.fps * 100) / 100);

      // map Nuke's read colorspace to the loader transform.
      // linear file -> convert to sRGB for model input.
      // already display-encoded (sRGB / rec709 / gamma) -> pass through.
      if (detail.colorspace) {
        const cs = String(detail.colorspace).toLowerCase();
        let pick = null;
        if (cs.includes("linear") || cs.includes("acescg") ||
            cs.includes("aces") || cs.includes("scene")) {
          pick = "linear -> sRGB";
        } else if (cs.includes("srgb") || cs.includes("rec709") ||
                   cs.includes("rec.709") || cs.includes("gamma") ||
                   cs.includes("bt.1886") || cs.includes("sdr")) {
          pick = "keep linear (no conversion)";
        }
        if (pick) set("colorspace", pick);
      }
      const pw = target.widgets?.find((w) => w.name === "path");
      if (pw) {
        pw.value = detail.path;
        pw.callback?.(detail.path); // triggers preview + resolution readout
      }
      app.canvas?.selectNode?.(target);
      app.graph.setDirtyCanvas(true, true);
    });
  },
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (!EXR_NODES.has(nodeData.name)) return;

    // --- clickable credit ---
    const origDraw = nodeType.prototype.onDrawForeground;
    nodeType.prototype.onDrawForeground = function (ctx) {
      origDraw?.apply(this, arguments);
      if (this.flags?.collapsed) return;
      ctx.save();
      ctx.font = "italic 10px sans-serif";
      ctx.fillStyle = "rgba(140,170,220,0.9)";
      ctx.textAlign = "right";
      const x = this.size[0] - 8;
      const y = this.size[1] - 6;
      ctx.fillText(CREDIT, x, y);
      const tw = ctx.measureText(CREDIT).width;
      ctx.strokeStyle = "rgba(140,170,220,0.5)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x - tw, y + 2);
      ctx.lineTo(x, y + 2);
      ctx.stroke();
      this._creditRect = [x - tw - 2, y - 11, tw + 4, 15];
      if (this._plateInfo) {
        const pi = this._plateInfo;
        ctx.font = "11px sans-serif";
        ctx.fillStyle = "rgba(190,190,190,0.9)";
        ctx.textAlign = "center";
        ctx.fillText(
          pi.w + " x " + pi.h + "  \u00b7  " + pi.c +
            (pi.c === 1 ? " frame" : " frames"),
          this.size[0] / 2,
          this.size[1] - 6
        );
      }
      ctx.restore();
    };

    const origMouse = nodeType.prototype.onMouseDown;
    nodeType.prototype.onMouseDown = function (e, pos, canvas) {
      const r = this._creditRect;
      if (
        r && !this.flags?.collapsed &&
        pos[0] >= r[0] && pos[0] <= r[0] + r[2] &&
        pos[1] >= r[1] && pos[1] <= r[1] + r[3]
      ) {
        window.open(CREDIT_URL, "_blank", "noopener");
        return true;
      }
      // frame step buttons
      const hit = (r) =>
        r && !this.flags?.collapsed &&
        pos[0] >= r[0] && pos[0] <= r[0] + r[2] &&
        pos[1] >= r[1] && pos[1] <= r[1] + r[3];
      if (hit(this._prevBtnRect)) { this._exrStep?.(-1); return true; }
      if (hit(this._nextBtnRect)) { this._exrStep?.(1); return true; }

      // play/pause button on the loader preview
      const pb = this._playBtnRect;
      if (
        pb && !this.flags?.collapsed &&
        pos[0] >= pb[0] && pos[0] <= pb[0] + pb[2] &&
        pos[1] >= pb[1] && pos[1] <= pb[1] + pb[3]
      ) {
        this._exrTogglePlay?.();
        return true;
      }
      return origMouse?.apply(this, arguments);
    };

    if (nodeData.name !== "LoadEXRPlate") return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      onNodeCreated?.apply(this, arguments);
      const node = this;

      // ---- player state ----
      const staticImg = new Image();
      let staticLoaded = false;
      let frameCount = 1;
      let frames = [];
      let cached = 0;
      let caching = false;
      let playing = false;
      let stepped = false;   // showing a cached frame while paused
      let playIdx = 0;
      let timer = null;
      let session = 0; // invalidates caching when path changes

      let needFit = true; // grow the node once when a new plate first loads
      staticImg.onload = () => {
        staticLoaded = true;
        if (needFit) {
          node.setSize(node.computeSize());
          needFit = false;
        }
        app.graph.setDirtyCanvas(true, true);
      };
      staticImg.onerror = () => {
        staticLoaded = false;
        node.setSize(node.computeSize());
        app.graph.setDirtyCanvas(true, true);
      };

      const pathWidget = node.widgets?.find((w) => w.name === "path");
      const fpsWidget = node.widgets?.find((w) => w.name === "fps");
      const csWidget = node.widgets?.find((w) => w.name === "colorspace");
      const expWidget = node.widgets?.find((w) => w.name === "exposure");
      const curCS = () => csWidget?.value ?? "";
      const curExp = () => Number(expWidget?.value) || 0;

      function stop() {
        playing = false;
        if (timer) { clearInterval(timer); timer = null; }
        app.graph.setDirtyCanvas(true, false);
      }

      function start() {
        if (!frames.length) return;
        playing = true;
        const fps = Math.max(0.5, Number(fpsWidget?.value) || 25);
        if (timer) clearInterval(timer);
        timer = setInterval(() => {
          playIdx = (playIdx + 1) % frames.length;
          app.graph.setDirtyCanvas(true, false);
        }, 1000 / fps);
      }

      async function cacheFrames(autoplay = true) {
        const p = (pathWidget?.value || "").trim();
        if (!p || caching) return;
        const mySession = session;
        caching = true;
        frames = [];
        cached = 0;
        const n = Math.min(frameCount, MAX_ANIM_FRAMES);
        for (let i = 0; i < n; i++) {
          if (mySession !== session) { caching = false; return; }
          const im = new Image();
          im.src = previewUrl(p, i, ANIM_W, "jpg", curCS(), curExp());
          await new Promise((res) => {
            im.onload = res;
            im.onerror = res;
          });
          if (mySession !== session) { caching = false; return; }
          if (im.naturalWidth) { frames.push(im); cached++; }
          app.graph.setDirtyCanvas(true, false);
        }
        caching = false;
        if (frames.length && autoplay) start();
        app.graph.setDirtyCanvas(true, false);
      }

      node._exrStep = (delta) => {
        if (!frames.length) {
          if (!caching) cacheFrames(false); // cache without auto-playing
          return;
        }
        stop();
        stepped = true;
        playIdx = (playIdx + delta + frames.length) % frames.length;
        app.graph.setDirtyCanvas(true, false);
      };

      node._exrTogglePlay = () => {
        if (playing) { stop(); return; }
        if (frames.length) { start(); return; }
        cacheFrames();
      };

      const refresh = () => {
        session++;
        stop();
        needFit = true;
        node._plateInfo = null;
        frames = [];
        cached = 0;
        caching = false;
        playIdx = 0;
        stepped = false;
        staticLoaded = false;
        frameCount = 1;
        const p = (pathWidget?.value || "").trim();
        if (!p) return;
        fetch(api.apiURL(
          "/exrplate/info?path=" + encodeURIComponent(p) + "&t=" + Date.now()))
          .then((r) => (r.ok ? r.json() : null))
          .then((j) => {
            if (!j) return;
            if (j.frame_count) frameCount = j.frame_count;
            if (j.width && j.height) {
              node._plateInfo = { w: j.width, h: j.height, c: j.frame_count };
              app.graph.setDirtyCanvas(true, false);
            }
          })
          .catch(() => {});
        staticImg.src = previewUrl(p, 0, 2048, "png", curCS(), curExp());
      };

      if (pathWidget) {
        const cb = pathWidget.callback;
        pathWidget.callback = function () {
          const r = cb?.apply(this, arguments);
          refresh();
          return r;
        };
      }

      // colorspace / exposure changes redraw the preview instantly
      const refreshImage = () => {
        session++;
        const wasPlaying = playing;
        stop();
        frames = [];
        cached = 0;
        caching = false;
        playIdx = 0;
        const p = (pathWidget?.value || "").trim();
        if (!p) return;
        staticImg.src = previewUrl(p, 0, 2048, "png", curCS(), curExp());
        if (wasPlaying) cacheFrames(); // re-cache + resume with new look
      };
      for (const w of [csWidget, expWidget]) {
        if (!w) continue;
        const cb2 = w.callback;
        w.callback = function () {
          const r = cb2?.apply(this, arguments);
          refreshImage();
          return r;
        };
      }

      // ---- upload button ----
      const fileInput = document.createElement("input");
      fileInput.type = "file";
      fileInput.accept = ".exr";
      fileInput.multiple = true;
      fileInput.style.display = "none";
      document.body.appendChild(fileInput);

      fileInput.addEventListener("change", async () => {
        const files = Array.from(fileInput.files || []);
        if (!files.length) return;
        const sub = "exrplate_" + Date.now();
        try {
          for (const f of files) {
            const fd = new FormData();
            fd.append("image", f, f.name);
            fd.append("subfolder", sub);
            fd.append("type", "input");
            fd.append("overwrite", "true");
            const resp = await api.fetchApi("/upload/image", {
              method: "POST", body: fd });
            if (resp.status !== 200)
              throw new Error(f.name + ": " + (await resp.text()));
          }
          if (pathWidget) {
            pathWidget.value = sub;
            pathWidget.callback?.(sub);
          }
          refresh();
        } catch (err) {
          alert("EXR upload failed:\n" + err.message);
        } finally {
          fileInput.value = "";
        }
      });

      node.addWidget("button", "choose exr sequence to upload", null, () =>
        fileInput.click()
      ).serialize = false;

      // ---- canvas preview with play/pause overlay ----
      const preview = {
        name: "plate_preview",
        type: "exr_preview",
        serialize: false,
        draw(ctx, drawNode, width, y) {
          const img =
            (playing || stepped) && frames[playIdx]?.naturalWidth
              ? frames[playIdx]
              : staticLoaded ? staticImg : null;
          const BOTTOM = 20; // strip reserved for credit + resolution
          const avail = drawNode.size[1] - y - BOTTOM;
          if (!img || avail < 12) {
            drawNode._playBtnRect = null;
            drawNode._prevBtnRect = null;
            drawNode._nextBtnRect = null;
            return;
          }
          const ar = img.naturalHeight / img.naturalWidth;
          let w = width;
          let h = w * ar;
          if (h > avail) { h = avail; w = h / ar; }
          const px = (width - w) / 2;
          ctx.save();
          ctx.drawImage(img, px, y, w, h);

          // caching progress
          if (caching) {
            ctx.fillStyle = "rgba(0,0,0,0.6)";
            ctx.fillRect(px, y, w, 16);
            ctx.fillStyle = "#fff";
            ctx.font = "10px sans-serif";
            ctx.textAlign = "left";
            ctx.fillText(
              "caching " + cached + " / " +
              Math.min(frameCount, MAX_ANIM_FRAMES), px + 6, y + 11);
          }

          // transport: [prev] [play/pause] [next], bottom-left of the preview
          const bs = 22;
          const gap = 5;
          const by = y + h - bs - 6;
          const x0 = px + 6;
          const disc = (bx) => {
            ctx.fillStyle = "rgba(0,0,0,0.55)";
            ctx.beginPath();
            ctx.arc(bx + bs / 2, by + bs / 2, bs / 2, 0, Math.PI * 2);
            ctx.fill();
            ctx.fillStyle = "#fff";
          };
          const stepIcon = (bx, dir) => {
            const cx = bx + bs / 2;
            const cy = by + bs / 2;
            ctx.beginPath();
            ctx.moveTo(cx + dir * 4, cy - 5);
            ctx.lineTo(cx + dir * 4, cy + 5);
            ctx.lineTo(cx - dir * 3, cy);
            ctx.closePath();
            ctx.fill();
            ctx.fillRect(cx + dir * 4.5, cy - 5, 2, 10); // stop bar
          };

          // prev
          disc(x0);
          stepIcon(x0, -1);
          drawNode._prevBtnRect = [x0, by, bs, bs];

          // play / pause
          const bx = x0 + bs + gap;
          disc(bx);
          if (playing) {
            ctx.fillRect(bx + 6, by + 5, 3.5, 12);
            ctx.fillRect(bx + 12.5, by + 5, 3.5, 12);
          } else {
            ctx.beginPath();
            ctx.moveTo(bx + 8, by + 5);
            ctx.lineTo(bx + 17, by + 11);
            ctx.lineTo(bx + 8, by + 17);
            ctx.closePath();
            ctx.fill();
          }
          drawNode._playBtnRect = [bx, by, bs, bs];

          // next
          const nx = bx + bs + gap;
          disc(nx);
          stepIcon(nx, 1);
          drawNode._nextBtnRect = [nx, by, bs, bs];

          // frame counter while playing or stepping
          if ((playing || stepped) && frames.length) {
            ctx.font = "10px sans-serif";
            ctx.textAlign = "right";
            ctx.fillStyle = "rgba(255,255,255,0.85)";
            ctx.fillText(
              (playIdx + 1) + " / " + frames.length, px + w - 6, y + h - 8);
          }
          ctx.restore();
        },
        computeSize(width) {
          const img = staticLoaded ? staticImg : null;
          if (!img?.naturalWidth) return [width, 0];
          return [width, (width / img.naturalWidth) * img.naturalHeight + 26];
        },
      };
      node.addCustomWidget(preview);

      const onRemoved = node.onRemoved;
      node.onRemoved = function () {
        session++;
        stop();
        fileInput.remove();
        onRemoved?.apply(this, arguments);
      };

      const onConfigure = node.onConfigure;
      node.onConfigure = function () {
        onConfigure?.apply(this, arguments);
        setTimeout(refresh, 50);
      };
      setTimeout(refresh, 50);
    };
  },
});
