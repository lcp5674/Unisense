// ai 性能基线（k6，对齐 gateways perf_baseline，TD §13）。
// ⚠️ D8 修正：基线必须带鉴权压真实业务端点，禁止仅压未鉴权的 /metrics（虚假绿灯）。
// 目标：问数（nl2sql）写路径并发延迟基线 P95 < 800ms，失败率 < 0.5%（对齐 gateways）。
// 注意：本基线仅验证鉴权与端点可达性（断言非 401/403），不依赖 nl2sql 业务实现（D12 标注四期）。
// 运行：
//   poetry run uvicorn app.main:app --host 127.0.0.1 --port 8000 &
//   AI_TOKEN=<JWT> k6 run backend/tests/perf/baseline_ai.js
//   未提供 AI_TOKEN 将直接报错退出（不再静默压 /metrics）。
import http from "k6/http";
import { check, sleep } from "k6";

const BASE = __ENV.BASE_URL || "http://localhost:8000";
const TOKEN = __ENV.AI_TOKEN;
if (!TOKEN) {
  throw new Error("缺少 AI_TOKEN 环境变量（JWT）。基线必须带鉴权压真实业务端点，否则为虚假绿灯。");
}

export const options = {
  vus: 10,
  duration: "30s",
  thresholds: {
    http_req_duration: ["p(95)<800"],
    http_req_failed: ["rate<0.005"],
  },
};

const BODY = JSON.stringify({ nl_query: "查询上月销售额", metric_scope: [], execute: false });

export default function () {
  const headers = { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" };
  const r = http.post(`${BASE}/api/v1/ai/nl2sql`, BODY, { headers });
  check(r, { "nl2sql 鉴权通过(非401/403)": (x) => x.status !== 401 && x.status !== 403 });
  sleep(0.2);
}
