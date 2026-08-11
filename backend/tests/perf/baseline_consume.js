// consume 性能基线（k6，对齐 gateways perf_baseline，TD §12.6 / §16）。
//
// 目标：验证消费只读端点（dry-run 口径校验 + 执行计划）在并发下的延迟基线
//       （P95 < 300ms，失败率 < 0.5%，对齐 consume perf_contract 阈值 300ms 与 gateways 0.5%）。
// 运行：
//   UNISENSE_DB_URL=... poetry run uvicorn app.main:app --host 127.0.0.1 --port 8001 &
//   CONSUME_APIKEY=perf_client:PerfClient@123 k6 run backend/tests/perf/baseline_consume.js
import http from "k6/http";
import { check, sleep } from "k6";

const BASE = __ENV.BASE_URL || "http://localhost:8001";
const APIKEY = __ENV.CONSUME_APIKEY || "perf_client:PerfClient@123";

export const options = {
  vus: 10,
  duration: "30s",
  thresholds: {
    http_req_duration: ["p(95)<300"],
    http_req_failed: ["rate<0.005"],
  },
};

export default function () {
  const headers = {
    "X-Api-Key": APIKEY,
    "Content-Type": "application/json",
  };
  const body = JSON.stringify({
    metric_code: "perf_metric_000",
    date_range: "2026-01~2026-03",
  });
  const r = http.post(`${BASE}/api/v1/consume/query/dry-run`, body, { headers });
  check(r, { "consume dry-run 200": (x) => x.status === 200 });
  sleep(0.2);
}
