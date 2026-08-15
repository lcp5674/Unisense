"""构建 DP 任务血缘交互式视图（自包含 HTML + 精简数据 JSON）。

读取 scripts/analyze_dp_lineage.py 生成的 lineage_out/lineage.json，
生成：
  lineage_out/dp_lineage_view_data.json   —— 精简数据（含预计算分层坐标 + 连通分量）
  lineage_out/dp_lineage_view.html        —— 自包含交互式血缘图（vis-network）

用法：
  backend/.venv/bin/python scripts/build_dp_lineage_view.py [--embed]
  --embed 把数据内嵌进 HTML（单文件，双击即可打开；文件较大）
  默认不 embed，HTML 通过 fetch('dp_lineage_view_data.json') 加载（需与数据同目录部署）
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from pathlib import Path

OUT_DIR = Path("/System/Volumes/Data/data/GitCode/Unisense/lineage_out")
SRC_JSON = OUT_DIR / "lineage.json"
VIEW_DATA = OUT_DIR / "dp_lineage_view_data.json"
VIEW_HTML = OUT_DIR / "dp_lineage_view.html"

# 分层 x 坐标（越大越靠右 = 越下游/越应用层）
LAYER_X = {
    "源端": 0,
    "ODS": 200,
    "DWD": 400,
    "DW": 600,
    "DWS": 800,
    "ADS": 1000,
    "临时": 1200,
    "其他": 1400,
    "未知": 1600,
}
# 层级颜色（图例 + 节点配色）
LAYER_COLOR = {
    "源端": "#9e9e9e",
    "ODS": "#4caf50",
    "DWD": "#2196f3",
    "DW": "#3f51b5",
    "DWS": "#9c27b0",
    "ADS": "#f44336",
    "临时": "#ff9800",
    "其他": "#607d8b",
    "未知": "#bdbdbd",
}
SQL_KIND_COLOR = {
    "insert": "#2196f3",
    "ctas": "#4caf50",
    "regex": "#ff9800",
    "select": "#9c27b0",
}


def build_data() -> dict:
    raw = json.loads(SRC_JSON.read_text(encoding="utf-8"))
    nodes_raw = raw["nodes"]
    edges_raw = raw["edges"]

    # 预计算分层坐标
    nodes = []
    for n in nodes_raw:
        x = LAYER_X.get(n.get("layer", "未知"), 1400)
        # y 在层内按表名 hash 分散，避免重叠
        y = (hash(n["id"]) % 2000) - 1000
        nodes.append({
            "id": n["id"],
            "label": n["id"],
            "layer": n.get("layer", "未知"),
            "db": n.get("db", ""),
            "director": n.get("director", ""),
            "cycle": n.get("cycle", ""),
            "frequence": n.get("frequence", ""),
            "create_type": n.get("create_type", ""),
            "is_task_output": n.get("is_task_output", False),
            "level_id": n.get("level_id", ""),
            "domain_id": n.get("domain_id", ""),
            "x": x,
            "y": y,
        })

    # 精简边（截断 SQL/任务名）
    edges = []
    for e in edges_raw:
        edges.append({
            "source": e["source"],
            "target": e["target"],
            "task_name": e.get("task_name", "")[:80],
            "sql_kind": e.get("sql_kind", ""),
            "sql": e.get("sql_snippet", "")[:200],
        })

    # 无向连通分量（用于聚焦子图）
    adj = defaultdict(set)
    for e in edges:
        adj[e["source"]].add(e["target"])
        adj[e["target"]].add(e["source"])
    visited = set()
    comps: list[list[str]] = []
    for nid in nodes:
        if nid["id"] in visited:
            continue
        comp = []
        dq = deque([nid["id"]])
        visited.add(nid["id"])
        while dq:
            cur = dq.popleft()
            comp.append(cur)
            for nb in adj.get(cur, ()):
                if nb not in visited:
                    visited.add(nb)
                    dq.append(nb)
        comps.append(comp)
    comps.sort(key=len, reverse=True)
    comp_meta = [
        {"index": i, "size": len(c), "nodes": c}
        for i, c in enumerate(comps)
    ]

    return {
        "meta": {
            "source": raw["meta"].get("source_file", ""),
            "total_tables": len(nodes),
            "total_edges": len(edges),
            "components": len(comp_meta),
            "layers": sorted(set(n["layer"] for n in nodes), key=lambda l: LAYER_X.get(l, 9999)),
        },
        "nodes": nodes,
        "edges": edges,
        "components": comp_meta,
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DP 任务血缘视图</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
  :root { --bg:#f5f6fa; --panel:#fff; --border:#e3e6ee; --text:#1f2430; --muted:#7a8194; }
  * { box-sizing: border-box; }
  html, body { margin:0; height:100%; font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif; background:var(--bg); color:var(--text); }
  #app { display:flex; flex-direction:column; height:100vh; }
  header { padding:10px 16px; background:var(--panel); border-bottom:1px solid var(--border); display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
  header h1 { font-size:15px; margin:0 8px 0 0; font-weight:600; }
  .tool { display:flex; align-items:center; gap:6px; }
  .tool label { font-size:12px; color:var(--muted); }
  input[type=text], select { height:28px; border:1px solid var(--border); border-radius:6px; padding:0 8px; font-size:12px; background:#fff; }
  input[type=text] { width:220px; }
  select { max-width:170px; }
  button { height:28px; border:1px solid var(--border); border-radius:6px; background:#fff; cursor:pointer; font-size:12px; padding:0 12px; }
  button:hover { border-color:#4a90d9; color:#4a90d9; }
  button.primary { background:#4a90d9; color:#fff; border-color:#4a90d9; }
  #stats { font-size:12px; color:var(--muted); margin-left:auto; }
  #main { flex:1; display:flex; min-height:0; position:relative; }
  #network { flex:1; background:#fff; }
  #detail { width:320px; background:var(--panel); border-left:1px solid var(--border); padding:14px; overflow-y:auto; font-size:13px; }
  #detail h2 { font-size:14px; margin:0 0 10px; }
  #detail .row { display:flex; justify-content:space-between; padding:5px 0; border-bottom:1px dashed var(--border); }
  #detail .row .k { color:var(--muted); }
  #detail h3 { font-size:13px; margin:14px 0 6px; color:#333; }
  #detail ul { margin:0; padding-left:16px; }
  #detail li { margin:3px 0; word-break:break-all; }
  #detail .sql { background:#f0f2f7; border-radius:6px; padding:8px; font-family:ui-monospace,Menlo,monospace; font-size:11px; color:#444; max-height:180px; overflow:auto; white-space:pre-wrap; }
  #legend { position:absolute; left:12px; bottom:12px; background:rgba(255,255,255,.95); border:1px solid var(--border); border-radius:8px; padding:10px 12px; font-size:12px; box-shadow:0 2px 8px rgba(0,0,0,.06); max-width:230px; z-index:2; }
  #legend .l { display:flex; align-items:center; gap:6px; margin:3px 0; }
  #legend .dot { width:12px; height:12px; border-radius:50%; flex:none; }
  #legend .dot.rect { border-radius:3px; }
  #empty { position:absolute; inset:0; display:none; align-items:center; justify-content:center; color:var(--muted); font-size:14px; z-index:2; pointer-events:none; }
  .pill { display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px; color:#fff; }
  .muted { color:var(--muted); }
  @media (max-width:900px){ #detail { display:none; } }
</style>
</head>
<body>
<div id="app">
  <header>
    <h1>DP 任务血缘视图</h1>
    <div class="tool">
      <input type="text" id="search" placeholder="搜索表名（回车定位，可输入 库.表）">
    </div>
    <div class="tool">
      <label>库</label>
      <select id="dbFilter"></select>
    </div>
    <div class="tool">
      <label>分层</label>
      <select id="layerFilter"></select>
    </div>
    <div class="tool">
      <label>布局</label>
      <select id="layoutSel">
        <option value="hierarchy" selected>分层（预计算）</option>
        <option value="force">力导向</option>
      </select>
    </div>
    <div class="tool">
      <label>分量</label>
      <select id="compSel"></select>
    </div>
    <button id="resetBtn">重置视图</button>
    <button id="focusBtn" class="primary">聚焦当前选择</button>
    <span id="stats"></span>
  </header>
  <div id="main">
    <div id="network"></div>
    <div id="legend">
      <b>分层</b>
      <div id="layerLegend"></div>
      <b style="margin-top:6px;display:block">边类型</b>
      <div id="edgeLegend"></div>
    </div>
    <div id="empty">当前筛选下没有可展示的图，请调整筛选条件或选择连通分量。</div>
    <div id="detail">
      <h2>节点详情</h2>
      <div id="detailBody"><div class="muted">点击图上的表节点查看详情；悬停边查看任务与 SQL。</div></div>
    </div>
  </div>
</div>
<script>
__DATA_PLACEHOLDER__
</script>
<script>
const DATA = typeof __EMBEDDED__ !== "undefined" ? __EMBEDDED__ : null;

let nodes = [], edges = [], compMeta = [];
const layerColors = __LAYER_COLORS__;
const sqlColors = __SQL_COLORS__;

async function loadData() {
  if (DATA) { init(DATA); return; }
  const res = await fetch("dp_lineage_view_data.json");
  if (!res.ok) { document.getElementById("empty").style.display = "flex"; document.getElementById("empty").textContent = "无法加载数据文件（请确保 dp_lineage_view_data.json 与本 HTML 同目录）"; return; }
  init(await res.json());
}

function init(d) {
  nodes = d.nodes; edges = d.edges; compMeta = d.components;
  window.__nodeIndex = new Map(nodes.map(n => [n.id, n]));
  window.__edgesByNode = {};
  for (const e of edges) {
    (window.__edgesByNode[e.source] = window.__edgesByNode[e.source] || []).push(e);
    (window.__edgesByNode[e.target] = window.__edgesByNode[e.target] || []).push(e);
  }
  document.getElementById("stats").textContent = `${d.meta.total_tables} 表 · ${d.meta.total_edges} 边 · ${d.meta.components} 个连通分量`;
  // 图例
  const ll = document.getElementById("layerLegend");
  ll.innerHTML = "";
  for (const L of d.meta.layers) {
    const div = document.createElement("div"); div.className = "l";
    div.innerHTML = `<span class="dot" style="background:${layerColors[L] || '#bbb'}"></span>${L}`;
    ll.appendChild(div);
  }
  const el = document.getElementById("edgeLegend");
  el.innerHTML = "";
  for (const [k, c] of Object.entries(sqlColors)) {
    const div = document.createElement("div"); div.className = "l";
    div.innerHTML = `<span class="dot" style="background:${c}"></span>${k}`;
    el.appendChild(div);
  }
  // 库筛选
  const dbs = [...new Set(nodes.map(n => n.db).filter(Boolean))].sort();
  document.getElementById("dbFilter").innerHTML = `<option value="">全部库</option>` + dbs.map(dd => `<option value="${dd}">${dd}</option>`).join("");
  // 分层筛选
  document.getElementById("layerFilter").innerHTML = `<option value="">全部分层</option>` + d.meta.layers.map(l => `<option value="${l}">${l}</option>`).join("");
  // 连通分量（前 12 大）
  const top = compMeta.slice(0, 12);
  document.getElementById("compSel").innerHTML = `<option value="">全部</option>` + top.map((c, i) => `<option value="${c.index}">第${i + 1}大分量（${c.size} 节点）</option>`).join("");
  render();
}

function currentFilter() {
  return {
    db: document.getElementById("dbFilter").value,
    layer: document.getElementById("layerFilter").value,
    compIdx: document.getElementById("compSel").value,
  };
}

function filteredNodeIds() {
  const { db, layer, compIdx } = currentFilter();
  let ids = new Set();
  if (compIdx !== "") {
    const comp = compMeta.find(c => String(c.index) === String(compIdx));
    if (comp) ids = new Set(comp.nodes); else return ids;
  } else {
    for (const n of nodes) ids.add(n.id);
  }
  const out = new Set();
  for (const n of nodes) {
    if (!ids.has(n.id)) continue;
    if (db && n.db !== db) continue;
    if (layer && n.layer !== layer) continue;
    out.add(n.id);
  }
  return out;
}

function buildVisData(ids) {
  const vsNodes = nodes.filter(n => ids.has(n.id)).map(n => ({
    id: n.id, label: n.id,
    color: { background: layerColors[n.layer] || "#bbb", border: "#fff", highlight: { background: "#ffd54f", border: "#ff8f00" } },
    shape: n.is_task_output ? "box" : "dot",
    size: n.is_task_output ? 18 : 12,
    font: { size: 12, face: "PingFang SC" },
    x: n.x, y: n.y,
    title: `${n.id}\n分层:${n.layer} 责任人:${n.director || "-"} 周期:${n.cycle || "-"}`,
  }));
  const vsEdges = edges.filter(e => ids.has(e.source) && ids.has(e.target)).map(e => ({
    from: e.source, to: e.target, arrows: "to",
    color: { color: sqlColors[e.sql_kind] || "#90a4ae", highlight: "#e91e63", opacity: 0.75 },
    title: `任务: ${e.task_name}\n类型: ${e.sql_kind}\n${e.sql}`,
    width: 1,
  }));
  return { vsNodes, vsEdges };
}

function render(label) {
  const ids = filteredNodeIds();
  const { vsNodes, vsEdges } = buildVisData(ids);
  const opts = {
    nodes: { borderWidth: 1 },
    edges: { smooth: { type: "continuous", roundness: 0.2 } },
    physics: document.getElementById("layoutSel").value === "force"
      ? { enabled: true, stabilization: { iterations: 150 }, barnesHut: { gravitationalConstant: -2000, springLength: 120, springConstant: 0.03 } }
      : { enabled: false },
    interaction: { hover: true, tooltipDelay: 120, navigationButtons: true, keyboard: true },
  };
  const container = document.getElementById("network");
  if (window.__net) window.__net.destroy();
  window.__net = new vis.Network(container, { nodes: new vis.DataSet(vsNodes), edges: new vis.DataSet(vsEdges) }, opts);
  window.__net.on("click", params => { if (params.nodes.length) { onNodeClick(params.nodes[0]); } });
  window.__net.on("doubleClick", params => { if (params.nodes.length) { onNodeDoubleClick(params.nodes[0]); } });
  document.getElementById("empty").style.display = vsNodes.length ? "none" : "flex";
  document.getElementById("stats").textContent = label || `${vsNodes.length} 表 · ${vsEdges.length} 边（当前视图）`;
}

// ---- 邻域 ----
function neighbors(id, depth) {
  const seen = new Set([id]);
  let frontier = [id];
  for (let d = 0; d < depth; d++) {
    const next = [];
    for (const f of frontier) {
      for (const e of (window.__edgesByNode[f] || [])) {
        const o = e.source === f ? e.target : e.source;
        if (!seen.has(o)) { seen.add(o); next.push(o); }
      }
    }
    frontier = next;
  }
  return seen;
}

function onNodeClick(id) {
  const ns = neighbors(id, 1);
  try { window.__net.selectNodes(Array.from(ns), true); } catch (e) { /* ignore */ }
  showDetail(id);
}

function onNodeDoubleClick(id) {
  const ns = neighbors(id, 2);
  const { vsNodes, vsEdges } = buildVisData(ns);
  window.__net.setData({ nodes: new vis.DataSet(vsNodes), edges: new vis.DataSet(vsEdges) });
  window.__net.fit({ animation: true });
  document.getElementById("stats").textContent = `${vsNodes.length} 表 · ${vsEdges.length} 边（${id} 的 2 跳邻域）`;
}

function showDetail(id) {
  const n = window.__nodeIndex.get(id);
  if (!n) return;
  const up = [], down = [];
  for (const e of (window.__edgesByNode[id] || [])) {
    if (e.target === id) up.push(e); else down.push(e);
  }
  document.getElementById("detailBody").innerHTML = `
    <div class="row"><span class="k">表名</span><span>${n.id}</span></div>
    <div class="row"><span class="k">分层</span><span class="pill" style="background:${layerColors[n.layer] || '#bbb'}">${n.layer}</span></div>
    <div class="row"><span class="k">库</span><span>${n.db || "-"}</span></div>
    <div class="row"><span class="k">责任人</span><span>${n.director || "-"}</span></div>
    <div class="row"><span class="k">调度周期</span><span>${n.cycle || "-"}</span></div>
    <div class="row"><span class="k">加工频度</span><span>${n.frequence || "-"}</span></div>
    <div class="row"><span class="k">加工方式</span><span>${n.create_type || "-"}</span></div>
    <div class="row"><span class="k">层级ID</span><span>${n.level_id || "-"}</span></div>
    <div class="row"><span class="k">域ID</span><span>${n.domain_id || "-"}</span></div>
    <div class="row"><span class="k">任务输出表</span><span>${n.is_task_output ? "是" : "否"}</span></div>
    <h3>上游（${up.length}）</h3>
    <ul>${up.length ? up.slice(0, 12).map(e => `<li>${e.source} <span class="muted">(${e.sql_kind})</span></li>`).join("") : "<li class='muted'>无</li>"}</ul>
    <h3>下游（${down.length}）</h3>
    <ul>${down.length ? down.slice(0, 12).map(e => `<li>${e.target} <span class="muted">(${e.sql_kind})</span></li>`).join("") : "<li class='muted'>无</li>"}</ul>
    <h3>生成 SQL 示例</h3>
    ${down.length ? `<div class="sql">${escapeHtml(down[0].sql)}</div>` : "<div class='muted'>无</div>"}
  `;
}

function escapeHtml(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

// ---- 搜索定位 ----
document.getElementById("search").addEventListener("keydown", ev => {
  if (ev.key !== "Enter") return;
  const q = document.getElementById("search").value.trim().toLowerCase();
  if (!q) return;
  const ids = filteredNodeIds();
  let hit = null;
  for (const n of nodes) if (ids.has(n.id) && n.id.toLowerCase().includes(q)) { hit = n.id; break; }
  if (!hit) {
    for (const n of nodes) if (n.id.toLowerCase().includes(q)) { hit = n.id; break; }
    if (hit) { alert(`表 ${hit} 不在当前筛选视图内，请调整库/分层/分量筛选后再试`); return; }
    alert("未找到匹配的表"); return;
  }
  window.__net.focus(hit, { scale: 0.8, animation: true });
  onNodeClick(hit);
});

// ---- 工具条 ----
document.getElementById("focusBtn").addEventListener("click", () => render());
document.getElementById("resetBtn").addEventListener("click", () => {
  document.getElementById("dbFilter").value = "";
  document.getElementById("layerFilter").value = "";
  document.getElementById("compSel").value = "";
  document.getElementById("layoutSel").value = "hierarchy";
  render();
});
["dbFilter", "layerFilter", "compSel", "layoutSel"].forEach(id => document.getElementById(id).addEventListener("change", () => render()));

loadData();
</script>
</body>
</html>
"""


def main() -> None:
    embed = "--embed" in sys.argv
    data = build_data()
    VIEW_DATA.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    if embed:
        # 定义 __EMBEDDED__，DATA 判定自然走 embed 分支
        payload = f"const __EMBEDDED__ = {json.dumps(data, ensure_ascii=False)};"
        html = HTML_TEMPLATE.replace("__DATA_PLACEHOLDER__", payload)
    else:
        html = HTML_TEMPLATE.replace("__DATA_PLACEHOLDER__", "")
    html = html.replace("__LAYER_COLORS__", json.dumps(LAYER_COLOR, ensure_ascii=False))
    html = html.replace("__SQL_COLORS__", json.dumps(SQL_KIND_COLOR, ensure_ascii=False))
    VIEW_HTML.write_text(html, encoding="utf-8")

    print(f"数据文件: {VIEW_DATA} ({VIEW_DATA.stat().st_size/1024/1024:.2f} MB)")
    print(f"视图文件: {VIEW_HTML} ({VIEW_HTML.stat().st_size/1024/1024:.2f} MB, {'内嵌数据' if embed else 'fetch 数据'})")
    print(f"节点: {data['meta']['total_tables']}  边: {data['meta']['total_edges']}  连通分量: {data['meta']['components']}")


if __name__ == "__main__":
    main()
