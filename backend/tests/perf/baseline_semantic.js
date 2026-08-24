// 语义领域性能基线（k6，对齐 gateways perf_baseline，TD §12.3 / §16）。
//
// 目标：验证指标语义核心只读端点（列表/详情/版本/健康度）+ 消费者仪表盘聚合
//       在 10 VU 并发下延迟基线 P95 < 800ms，失败率 < 0.5%（对齐 gateways 通用阈值）。
//       语义查询下推（OLAP pushdown）单独由 consume 基线覆盖，本基线聚焦语义元数据读路径。
//
// 运行（打运行中的 backend 容器，backend 映射宿主 8100）：
//   SEMANTIC_TOKEN=<JWT> k6 run backend/tests/perf/baseline_semantic.js
//   未提供 SEMANTIC_TOKEN 将直接报错退出（禁止压未鉴权端点，避免虚假绿灯）。
import http from "k6/http";
import { check, sleep } from "k6";

const BASE = __ENV.BASE_URL || "http://localhost:8100";
const TOKEN = __ENV.SEMANTIC_TOKEN;
if (!TOKEN) {
  throw new Error("缺少 SEMANTIC_TOKEN 环境变量（JWT）。基线必须带鉴权压真实业务端点，否则为虚假绿灯。");
}

// 轮询已发布的指标编码（live 库实测存在）
const METRIC_CODES = [
  "outp_e2e_fee_day",
  "outp_e2e_visit_day",
  "outp_e2e_register_day",
  "outp_e2e_piipatient_day",
  "outp_e2e_avgfee_day",
  "outp_e2e_conflicta_day",
  "outp_e2e_drugfee_day",
];

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
  const code = METRIC_CODES[__VU % METRIC_CODES.length];

  // 1. 指标语义定义列表（FR-06）
  const listRes = http.get(`${BASE}/api/v1/metric-definitions?page=1&page_size=20`, { headers });
  check(listRes, { "list 200": (r) => r.status === 200 });

  sleep(0.1);

  // 2. 指标语义定义详情（FR-06）
  const detailRes = http.get(`${BASE}/api/v1/metric-definitions/${code}`, { headers });
  check(detailRes, { "detail 200": (r) => r.status === 200 });

  sleep(0.1);

  // 3. 指标版本历史（FR-05）
  const verRes = http.get(`${BASE}/api/v1/metric-definitions/${code}/versions`, { headers });
  check(verRes, { "versions 200": (r) => r.status === 200 });

  sleep(0.1);

  // 4. 指标健康度评分（五维加权）
  const healthRes = http.get(`${BASE}/api/v1/metric-definitions/${code}/health`, { headers });
  check(healthRes, { "health 200": (r) => r.status === 200 });

  sleep(0.1);

  // 5. 消费者仪表盘（按域/Owner 聚合 + 全资产计数，单次聚合查询）
  const dashRes = http.get(`${BASE}/api/v1/semantics/dashboard`, { headers });
  check(dashRes, { "dashboard 200": (r) => r.status === 200 });

  sleep(0.2);
}
