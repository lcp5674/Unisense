// Unisense MVP 端到端冒烟脚本（Node ≥18，自带 fetch）
// 覆盖 MVP 验收链路：登录 → 列指标 → 注册草稿 → PII复核(如需) → 发布 → 详情 → 收藏 → 血缘
// 用法（栈起后）：
//   node e2e/smoke.mjs
// 环境变量（可选）：API_BASE（默认 http://localhost:8000/api/v1）、SEMANTIC_API_KEY（默认 dev-semantic-key）
//   SMOKE_USER / SMOKE_PASSWORD（默认从后端 seed 读取，或传 admin/test）
//   注意：优先用 SMOKE_USER/SMOKE_PASSWORD，避免与系统内置的 USERNAME 环境变量冲突（macOS 默认设 USERNAME=$USER）。

const API = process.env.API_BASE || "http://localhost:8000/api/v1";
const KEY = process.env.SEMANTIC_API_KEY || "dev-semantic-key";
const USER = process.env.SMOKE_USER || process.env.USERNAME || "admin";
const PASS = process.env.SMOKE_PASSWORD || process.env.PASSWORD || "test";

let token = "";
function authHeaders() {
  return { "Content-Type": "application/json", "X-Api-Key": KEY, ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}
async function call(method, path, body) {
  const res = await fetch(`${API}${path}`, {
    method,
    headers: authHeaders(),
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(`[${method} ${path}] HTTP ${res.status}: ${json?.message || JSON.stringify(json)} (code=${json?.code})`);
  }
  return json.data;
}

const steps = [];
function ok(name) { steps.push(`✅ ${name}`); console.log(`✅ ${name}`); }
function fail(name, e) { steps.push(`❌ ${name}: ${e.message}`); console.error(`❌ ${name}: ${e.message}`); }

async function main() {
  // 1. 登录
  try {
    const login = await call("POST", "/auth/login", { username: USER, password: PASS });
    token = login.access_token;
    ok("登录成功");
  } catch (e) { fail("登录", e); return finish(); }

  // 2. 列表
  try {
    const list = await call("GET", "/metric-definitions?page=1&page_size=5");
    ok(`列指标成功（total=${list.total}）`);
  } catch (e) { fail("列指标", e); }

  // 3. 注册草稿
  const code = `mvp_smoke_${Date.now()}`;
  try {
    await call("POST", "/metric-definitions", {
      metric_code: code,
      name: "MVP冒烟指标",
      domain: "smoke",
      type: "atomic",
      granularity: "daily",
      unit: "次",
      aggregation: "SUM",
      time_semantics: "PERIOD",
      freshness: "T1",
      dw_layer: "DWS",
      metric_tier: "T2",
      definition_json: { expr: "sum(cnt)" },
      pii_flag: false,
    });
    ok(`注册草稿成功（${code}）`);
  } catch (e) { fail("注册草稿", e); return finish(); }

  // 4. 发布
  try {
    await call("POST", `/metric-definitions/${code}/publish`, { change_reason: "mvp冒烟发布" });
    ok("发布成功");
  } catch (e) { fail("发布", e); }

  // 5. 详情
  try {
    const m = await call("GET", `/metric-definitions/${code}`);
    ok(`详情成功（status=${m.status}, v${m.version}）`);
  } catch (e) { fail("详情", e); }

  // 6. 收藏
  try {
    await call("POST", "/consume/me/favorites", { metric_code: code });
    const favs = await call("GET", "/consume/me/favorites");
    if (favs.includes(code)) ok("收藏成功");
    else fail("收藏", new Error("收藏列表未包含该指标"));
    await call("DELETE", `/consume/me/favorites/${code}`);
  } catch (e) { fail("收藏", e); }

  // 7. 血缘（以指标为节点）
  try {
    const edges = await call("GET", `/lineage/impact?node=${code}&direction=downstream&max_hops=5`);
    ok(`血缘查询成功（edges=${edges.length}）`);
  } catch (e) { fail("血缘", e); }

  // 8. 冲突列表
  try {
    const conflicts = await call("GET", "/conflicts?page=1&page_size=5");
    ok(`冲突列表成功（total=${conflicts.total}）`);
  } catch (e) { fail("冲突列表", e); }

  finish();
}

function finish() {
  const passed = steps.filter((s) => s.startsWith("✅")).length;
  const failed = steps.filter((s) => s.startsWith("❌")).length;
  console.log(`\n=== MVP 冒烟结果：${passed} 通过 / ${failed} 失败 ===`);
  process.exit(failed > 0 ? 1 : 0);
}

main();
