import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Button, Card, Empty, Spin } from "antd";
import { compareMetrics } from "../api";
import type { MetricCompareResult } from "../types";
import { MetricCompareTable } from "../components/MetricCompareTable";

export function MetricCompare() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const codeA = searchParams.get("a") || "";
  const codeB = searchParams.get("b") || "";
  const [result, setResult] = useState<MetricCompareResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    if (!codeA || !codeB) {
      setError("请从指标目录勾选两个指标后对比");
      setLoading(false);
      return;
    }
    setLoading(true);
    compareMetrics(codeA, codeB)
      .then((res) => {
        if (alive) setResult(res);
      })
      .catch((err) => {
        if (alive) setError(err instanceof Error ? err.message : "对比失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [codeA, codeB]);

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Assets / Compare</div>
          <h2>指标对比</h2>
          <p>
            <span className="mono">{codeA}</span>
            <span style={{ margin: "0 8px" }}>↔</span>
            <span className="mono">{codeB}</span>
            <span style={{ margin: "0 8px" }}>·</span>
            关键字段并排 diff
          </p>
        </div>
        <Button type="link" onClick={() => navigate("/catalog")}>← 返回目录</Button>
      </div>

      {loading ? (
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin />
        </div>
      ) : error ? (
        <Card>
          <Empty description={error} />
        </Card>
      ) : result ? (
        <MetricCompareTable result={result} codeA={codeA} codeB={codeB} />
      ) : null}
    </div>
  );
}
