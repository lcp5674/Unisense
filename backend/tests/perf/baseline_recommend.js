// recommend 性能基线（k6，对齐 gateways perf_baseline，TD §13）。
// ⚠️ D8 修正：基线必须带鉴权压真实业务端点，禁止仅压未鉴权的 /metrics（虚假绿灯）。
// 目标：读路径（推荐指标/术语）并发延迟基线 P95 < 800ms，失败率 < 0.5%（对齐 gateways）。
// 运行：
//   poetry run uvicorn app.main:app --host 127.0.0.1 --port 8000 &
//   RECOMMEND_TOKEN=<JWT> k6 run backend/tests/perf/baseline_recommend.js
//   未提供 RECOMMEND_TOKEN 将直接报错退出（不再静默压 /metrics）。
import http from "k6/http";
import { check, sleep } from "k6";

const BASE = __ENV.BASE_URL || "http://localhost:8000";
const TOKEN = __ENV.RECOMMEND_TOKEN;
if (!TOKEN) {
  throw new Error("缺少 RECOMMEND_TOKEN 环境变量（JWT）。基线必须带鉴权压真实业务端点，否则为虚假绿灯。");
}

export const options = {
  vus: 10,
  duration: "30s",
  thresholds: {
    http_req_duration: ["p(95)<800"],
    http_req_failed: ["rate<0.005"],
  },
};

export default function () {
  const headers = { Authorization: `Bearer ${TOKEN}` };
  const t = http.get(`${BASE}/api/v1/recommend/terms`, { headers });
  check(t, { "terms 200": (x) => x.status === 200 });
  const r = http.get(`${BASE}/api/v1/recommend/metrics?user_id=1`, { headers });
  check(r, { "metrics 200": (x) => x.status === 200 });
  sleep(0.2);
}
