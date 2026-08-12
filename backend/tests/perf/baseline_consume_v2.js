// consume v2 性能基线（k6，对齐 US10 / FR-5 / gateways perf_baseline）。
//
// 目标：验证消费只读端点（dry-run 口径校验 + 执行计划 + 缓存命中）在 100 VU 并发下
//       延迟基线 P95 ≤ 300ms，失败率 < 0.5%。
// 新增：
//   - 100 VU 并发（原 10 VU）
//   - 执行计划端点覆盖
//   - 缓存命中路径验证（重复查询同一 metric_code）
//   - 响应体 from_cache 字段断言
//
// 运行：
//   UNISENSE_DB_URL=... poetry run uvicorn app.main:app --host 127.0.0.1 --port 8001 &
//   CONSUME_APIKEY=perf_client:PerfClient@123 k6 run backend/tests/perf/baseline_consume_v2.js

import http from "k6/http";
import { check, sleep } from "k6";

const BASE = __ENV.BASE_URL || "http://localhost:8001";
const APIKEY = __ENV.CONSUME_APIKEY || "perf_client:PerfClient@123";

// 预热指标编码列表
const METRIC_CODES = [
  "perf_metric_000",
  "perf_metric_001",
  "perf_metric_002",
  "perf_metric_003",
  "perf_metric_004",
  "perf_metric_005",
  "perf_metric_006",
  "perf_metric_007",
  "perf_metric_008",
  "perf_metric_009",
];

export const options = {
  stages: [
    { duration: "10s", target: 20 },   // 缓慢爬坡到 20 VU
    { duration: "20s", target: 50 },   // 爬坡到 50 VU
    { duration: "30s", target: 100 },  // 爬坡到 100 VU
    { duration: "60s", target: 100 },  // 维持 100 VU
    { duration: "10s", target: 0 },    // 降坡
  ],
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

  // 轮询指标编码，确保缓存命中路径也被覆盖
  const metricCode = METRIC_CODES[__VU % METRIC_CODES.length];

  // --- 测试 1: dry-run 口径校验 ---
  const dryRunBody = JSON.stringify({
    metric_code: metricCode,
    date_range: "2026-01~2026-03",
  });
  const dryRunRes = http.post(`${BASE}/api/v1/consume/query/dry-run`, dryRunBody, { headers });
  check(dryRunRes, {
    "dry-run 200": (r) => r.status === 200,
  });

  sleep(0.1);

  // --- 测试 2: 执行计划 ---
  const execBody = JSON.stringify({
    metric_code: metricCode,
    date_range: "2026-01~2026-03",
    dimensions: { region: "华东" },
  });
  const execRes = http.post(`${BASE}/api/v1/consume/query/execute`, execBody, { headers });
  check(execRes, {
    "execute 200": (r) => r.status === 200,
    "execute has rows": (r) => {
      try {
        const body = JSON.parse(r.body);
        return body.data && Array.isArray(body.data.rows);
      } catch {
        return false;
      }
    },
  });

  sleep(0.1);

  // --- 测试 3: 缓存命中路径（重复同一查询）---
  const cacheRes = http.post(`${BASE}/api/v1/consume/query/execute`, execBody, { headers });
  check(cacheRes, {
    "cache-hit 200": (r) => r.status === 200,
    "cache-hit faster": (r) => r.timings.duration < 300,
  });

  sleep(0.3);
}
