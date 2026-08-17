import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Alert, Button, Card, Empty, Select, Spin } from "antd";
import { ArrowLeftOutlined, SwapOutlined } from "@ant-design/icons";
import { compareMetricsMatrix, listMetrics } from "../api";
import type { MetricCompareMatrixResult, MetricResponse } from "../types";
import { MetricCompareMatrixTable } from "../components/MetricCompareMatrixTable";

const MAX_COMPARE = 6;
const MIN_COMPARE = 2;

/** 从 URL 解析待对比指标：优先 codes=a,b,c（多选），兼容旧入口 a/b（两两） */
function parseCodesFromSearch(searchParams: URLSearchParams): string[] {
  const codes = searchParams.get("codes");
  if (codes) {
    return codes
      .split(",")
      .map((c) => c.trim())
      .filter(Boolean);
  }
  const a = searchParams.get("a");
  const b = searchParams.get("b");
  return a && b ? [a, b] : [];
}

export function MetricCompare() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const codesFromUrl = useMemo(() => parseCodesFromSearch(searchParams), [searchParams]);

  const [candidates, setCandidates] = useState<MetricResponse[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [searching, setSearching] = useState(false);
  const [result, setResult] = useState<MetricCompareMatrixResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 加载选择器候选（默认前 100 条；搜索时按关键词拉取）
  useEffect(() => {
    let alive = true;
    setSearching(true);
    listMetrics({ page: 1, page_size: 100 })
      .then((res) => {
        if (alive) setCandidates(res.items ?? []);
      })
      .catch(() => {
        if (alive) setCandidates([]);
      })
      .finally(() => {
        if (alive) setSearching(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  // URL codes 就绪后初始化已选并自动对比
  useEffect(() => {
    if (codesFromUrl.length >= MIN_COMPARE) {
      setSelected(codesFromUrl);
      let alive = true;
      setLoading(true);
      setError(null);
      setResult(null);
      compareMetricsMatrix(codesFromUrl)
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
    }
    return undefined;
  }, [codesFromUrl]);

  // 统一返回上一入口：优先回退浏览器历史，无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  // 手动调整选择 → 同步 URL（replace，可分享），URL 变更驱动重跑对比
  function handleSelectChange(codes: string[]) {
    setSelected(codes);
    if (codes.length >= MIN_COMPARE) {
      setSearchParams({ codes: codes.join(",") }, { replace: true });
    } else if (codes.length === 0) {
      setSearchParams({}, { replace: true });
      setResult(null);
    }
  }

  function handleSearch(keyword: string) {
    setSearching(true);
    listMetrics({ page: 1, page_size: 100, keyword: keyword || undefined })
      .then((res) => setCandidates(res.items ?? []))
      .catch(() => setCandidates([]))
      .finally(() => setSearching(false));
  }

  const nameByCode = useMemo(() => {
    const m = new Map<string, string>();
    candidates.forEach((c) => m.set(c.metric_code, c.name));
    return m;
  }, [candidates]);

  const options = candidates.map((c) => ({
    value: c.metric_code,
    label: `${c.metric_code} · ${c.name}`,
  }));

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
            勾选 {MIN_COMPARE}~{MAX_COMPARE} 个指标矩阵对比 · 每行一个字段、每列一个指标，行级汇总差异
          </p>
        </div>
      </div>

      <Card size="small" style={{ marginBottom: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <SwapOutlined style={{ color: "var(--muted)" }} />
          <Select
            mode="multiple"
            style={{ flex: 1, minWidth: 320, maxWidth: 720 }}
            placeholder={`选择 ${MIN_COMPARE}~${MAX_COMPARE} 个指标（可搜索）`}
            value={selected}
            onChange={handleSelectChange}
            onSearch={handleSearch}
            options={options}
            loading={searching}
            showSearch
            optionFilterProp="label"
            maxTagCount={6}
            maxTagTextLength={24}
            filterOption={false}
          />
          {selected.length < MIN_COMPARE && selected.length > 0 && (
            <span className="muted" style={{ fontSize: 12 }}>
              至少选择 {MIN_COMPARE} 个指标
            </span>
          )}
        </div>
        {selected.length >= MIN_COMPARE && (
          <div style={{ marginTop: 8, display: "flex", gap: 6, flexWrap: "wrap" }}>
            {selected.map((code) => (
              <span
                key={code}
                className="mono"
                style={{
                  fontSize: 12,
                  padding: "2px 8px",
                  borderRadius: 6,
                  background: "rgba(47,84,235,0.08)",
                  color: "#2f54eb",
                }}
              >
                {code}
                {nameByCode.get(code) ? ` · ${nameByCode.get(code)}` : ""}
              </span>
            ))}
          </div>
        )}
      </Card>

      {loading ? (
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin />
        </div>
      ) : error ? (
        <Card>
          <Alert type="error" showIcon message={error} />
        </Card>
      ) : result ? (
        <MetricCompareMatrixTable result={result} />
      ) : selected.length < MIN_COMPARE ? (
        <Card>
          <Empty description="请从上方选择至少 2 个指标进行对比（也可从指标目录勾选 2~6 个后点「对比所选」跳转）" />
        </Card>
      ) : null}
    </div>
  );
}
