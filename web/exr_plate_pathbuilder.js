import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE = "EXRPlatePathBuilder";

// ---------------------------------------------------------------------------
// Browse dialog
// ---------------------------------------------------------------------------

function injectCSS() {
  if (document.getElementById("exrplate-browse-css")) return;
  const s = document.createElement("style");
  s.id = "exrplate-browse-css";
  s.textContent = `
  .exrp-overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:10000;
    display:flex;align-items:center;justify-content:center;font-family:sans-serif}
  .exrp-modal{background:#282828;color:#ddd;width:660px;max-width:92vw;
    max-height:74vh;border-radius:6px;display:flex;flex-direction:column;
    box-shadow:0 8px 32px rgba(0,0,0,.6);border:1px solid #444}
  .exrp-head{padding:10px 12px;border-bottom:1px solid #444;font-size:13px}
  .exrp-cur{font-family:monospace;font-size:12px;color:#9ecbff;
    word-break:break-all;margin-top:6px;user-select:text}
  .exrp-drives{padding:6px 12px;border-bottom:1px solid #3a3a3a;display:flex;
    gap:6px;flex-wrap:wrap}
  .exrp-drive{background:#3a3a3a;border-radius:3px;padding:2px 9px;
    font-size:12px;cursor:pointer}
  .exrp-drive:hover{background:#4d4d4d}
  .exrp-list{overflow-y:auto;flex:1;padding:4px 0;min-height:240px}
  .exrp-row{padding:4px 14px;font-size:13px;cursor:pointer;display:flex;gap:8px}
  .exrp-row:hover{background:#3a3a3a}
  .exrp-row.file{color:#7d7d7d;cursor:default}
  .exrp-row.file:hover{background:transparent}
  .exrp-foot{padding:10px 12px;border-top:1px solid #444;display:flex;gap:8px;
    justify-content:flex-end}
  .exrp-btn{background:#4a4a4a;border:0;color:#eee;padding:6px 14px;
    border-radius:4px;cursor:pointer;font-size:13px}
  .exrp-btn.primary{background:#2d6cdf}
  .exrp-btn:hover{filter:brightness(1.25)}
  .exrp-err{color:#e88;font-size:12px;padding:6px 14px}`;
  document.head.appendChild(s);
}

async function listDir(path) {
  const r = await api.fetchApi(
    "/exrplate/browse?path=" + encodeURIComponent(path || ""));
  return r.json();
}

function openBrowser(startPath, onPick) {
  injectCSS();
  const overlay = document.createElement("div");
  overlay.className = "exrp-overlay";
  overlay.innerHTML = `
    <div class="exrp-modal">
      <div class="exrp-head">Choose a folder<div class="exrp-cur"></div></div>
      <div class="exrp-drives"></div>
      <div class="exrp-list"></div>
      <div class="exrp-foot">
        <button class="exrp-btn" data-act="cancel">Cancel</button>
        <button class="exrp-btn primary" data-act="use">Use this folder</button>
      </div>
    </div>`;

  const cur = overlay.querySelector(".exrp-cur");
  const drives = overlay.querySelector(".exrp-drives");
  const list = overlay.querySelector(".exrp-list");
  let path = startPath || "";

  const close = () => {
    document.removeEventListener("keydown", onKey);
    overlay.remove();
  };
  const onKey = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
  };

  async function render(p) {
    let data;
    try {
      data = await listDir(p);
    } catch (err) {
      list.innerHTML = "";
      const e = document.createElement("div");
      e.className = "exrp-err";
      e.textContent = "browse failed: " + err.message;
      list.appendChild(e);
      return;
    }
    path = data.path || "";
    cur.textContent = path || "(pick a drive)";
    list.innerHTML = "";
    drives.innerHTML = "";

    (data.drives || []).forEach((d) => {
      const el = document.createElement("span");
      el.className = "exrp-drive";
      el.textContent = d;
      el.onclick = () => render(d);
      drives.appendChild(el);
    });

    if (data.error) {
      const e = document.createElement("div");
      e.className = "exrp-err";
      e.textContent = data.error;
      list.appendChild(e);
    }

    if (data.parent) {
      const up = document.createElement("div");
      up.className = "exrp-row";
      up.innerHTML = "<span>\u2b06</span><span>..</span>";
      up.onclick = () => render(data.parent);
      list.appendChild(up);
    }

    (data.dirs || []).forEach((d) => {
      const row = document.createElement("div");
      row.className = "exrp-row";
      const icon = document.createElement("span");
      icon.textContent = "\u{1F4C1}";
      const label = document.createElement("span");
      label.textContent = d;
      row.append(icon, label);
      row.onclick = () => render(path.replace(/\/$/, "") + "/" + d);
      list.appendChild(row);
    });

    (data.files || []).forEach((f) => {
      const row = document.createElement("div");
      row.className = "exrp-row file";
      const icon = document.createElement("span");
      icon.textContent = "\u00b7";
      const label = document.createElement("span");
      label.textContent = f;
      row.append(icon, label);
      list.appendChild(row);
    });
    const shown = (data.files || []).length;
    if ((data.file_count || 0) > shown) {
      const more = document.createElement("div");
      more.className = "exrp-row file";
      more.textContent = "\u2026 " + (data.file_count - shown) + " more files";
      list.appendChild(more);
    }
  }

  overlay.onclick = (e) => { if (e.target === overlay) close(); };
  overlay.querySelector('[data-act="cancel"]').onclick = close;
  overlay.querySelector('[data-act="use"]').onclick = () => {
    onPick(path);
    close();
  };

  document.addEventListener("keydown", onKey);
  document.body.appendChild(overlay);
  render(path);
}

// ---------------------------------------------------------------------------
// Node UI
// ---------------------------------------------------------------------------

app.registerExtension({
  name: "exrplate.pathbuilder",

  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      onNodeCreated?.apply(this, arguments);
      const node = this;

      const w = (n) => node.widgets?.find((x) => x.name === n);
      const folderW = w("folder");
      const subW = w("subpath");
      const nameW = w("name");
      const modeW = w("version_mode");
      const verW = w("version");
      const padW = w("version_pad");
      const styleW = w("version_style");
      const tokenW = w("frame_token");
      const extW = w("ext");

      const browseBtn = node.addWidget("button", "Browse\u2026", null, () => {
        openBrowser(folderW?.value || "", (picked) => {
          if (!folderW) return;
          folderW.value = picked;
          folderW.callback?.(picked);
          node.refreshResolved();
          app.graph.setDirtyCanvas(true, true);
        });
      });
      browseBtn.serialize = false;

      // read-only readout of the path the next run will actually use
      const readout = {
        name: "resolved",
        type: "exr_pathpreview",
        serialize: false,
        value: "",
        draw(ctx, drawNode, width, y) {
          const text = node._resolvedText || "press Browse to set a folder";
          ctx.save();
          ctx.font = "11px monospace";
          ctx.textAlign = "left";
          ctx.fillStyle = node._resolvedText
            ? "rgba(150,200,150,0.95)"
            : "rgba(160,160,160,0.7)";
          const pad = 12;
          const max = width - pad * 2;
          let shown = text;
          // trim from the left so the filename stays visible
          while (ctx.measureText(shown).width > max && shown.length > 4) {
            shown = "\u2026" + shown.slice(2);
          }
          ctx.fillText(shown, pad, y + 13);
          ctx.restore();
        },
        computeSize(width) {
          return [width, 20];
        },
      };
      node.addCustomWidget(readout);

      let inflight = null;
      node.refreshResolved = async () => {
        if (!folderW?.value) {
          node._resolvedText = "";
          app.graph.setDirtyCanvas(true, false);
          return;
        }
        const q = new URLSearchParams({
          folder: folderW.value,
          subpath: subW?.value || "",
          name: nameW?.value || "plate",
          style: styleW?.value || "folder",
          mode: modeW?.value || "auto_next",
          version: String(verW?.value ?? 1),
          pad: String(padW?.value ?? 3),
          frame_token: tokenW?.value || "####",
          ext: extW?.value || "exr",
        });
        const token = Symbol();
        inflight = token;
        try {
          const r = await api.fetchApi("/exrplate/build_preview?" + q);
          if (inflight !== token) return;   // a newer request won
          const j = await r.json();
          node._resolvedText = j.path;
          node._resolvedInfo = j;
          app.graph.setDirtyCanvas(true, false);
        } catch (err) {
          if (inflight !== token) return;
          node._resolvedText = "";
          app.graph.setDirtyCanvas(true, false);
        }
      };

      const refreshBtn = node.addWidget(
        "button", "Refresh version", null, () => node.refreshResolved());
      refreshBtn.serialize = false;

      // any widget change re-resolves the readout
      for (const widget of [folderW, subW, nameW, modeW, verW, padW,
                            styleW, tokenW, extW]) {
        if (!widget) continue;
        const orig = widget.callback;
        widget.callback = function () {
          const r = orig?.apply(this, arguments);
          node.refreshResolved();
          return r;
        };
      }

      const onConfigure = node.onConfigure;
      node.onConfigure = function () {
        onConfigure?.apply(this, arguments);
        setTimeout(() => node.refreshResolved(), 60);
      };
      setTimeout(() => node.refreshResolved(), 60);
    };
  },

  setup() {
    // after a run, show what the node actually resolved to
    api.addEventListener("exrplate.resolved", ({ detail }) => {
      if (!detail?.path) return;
      const nodes = app.graph._nodes?.filter((n) => n.comfyClass === NODE) || [];
      if (nodes.length !== 1) return;   // ambiguous with several builders
      nodes[0]._resolvedText = detail.path;
      app.graph.setDirtyCanvas(true, false);
    });
  },
});
