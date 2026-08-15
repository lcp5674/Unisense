import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Button, Card, Empty, Spin } from "antd";
import { ArrowLeftOutlined } from "@ant-design/icons";
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

  // 统一返回上一入口：优先回退浏览器历史（指标目录勾选对比等入口），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

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
          <Button
            type="link"
            icon={<ArrowLeftOutlined />}
            onClick={handleBack}
            style={{ padding: 0, marginBottom: 4 }}
          >
            返回
          </Button>
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
