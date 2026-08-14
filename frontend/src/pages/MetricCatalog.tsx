import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Table, Input, Select, Button, Space, Tag, message, Tooltip } from "antd";
import {
  SearchOutlined,
  ColumnWidthOutlined,
  PlusCircleOutlined,
  FileTextOutlined,
  DownloadOutlined,
} from "@ant-design/icons";
import { fetchDashboard, listMetrics, UnisenseApiError } from "../api";
import type { MetricResponse } from "../types";
import { useTracking } from "../hooks/useTracking";
import { METRIC_TYPE_LABEL, METRIC_TIER_LABEL } from "../utils/enums";

const STATUS_COLOR: Record<string, string> = {
  DRAFT: "default",
  EXPERIMENTAL: "processing",
  REVIEW: "warning",
  PUBLISHED: "success",
  DEPRECATED: "error",
};

const STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  EXPERIMENTAL: "实验",
  REVIEW: "审核",
  PUBLISHED: "已发布",
  DEPRECATED: "已废弃",
};

const TIER_OPTIONS = ["T1", "T2", "T3"].map((v) => ({ value: v, label: METRIC_TIER_LABEL[v] ?? v }));
const SORT_OPTIONS = [
  { value: "updated_at", label: "按更新时间" },
  { value: "created_at", label: "按创建时间" },
  { value: "version", label: "按版本号" },
  { value: "metric_code", label: "按编码" },
];

export function MetricCatalog() {
  const [items, setItems] = useState<MetricResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [keyword, setKeyword] = useState("");
  const [status, setStatus] = useState("");
  const [domain, setDomain] = useState("");
  const [tier, setTier] = useState("");
  const [sortBy, setSortBy] = useState<"updated_at" | "created_at" | "version" | "metric_code" | "name">("updated_at");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");
  const [domainOptions, setDomainOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [selected, setSelected] = useState<MetricResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { track } = useTracking();

  // 域列表从真实 dashboard by_domain 聚合（不硬编码）
  useEffect(() => {
    fetchDashboard()
      .then((d) => setDomainOptions(Object.keys(d.by_domain ?? {}).map((v) => ({ value: v, label: v }))))
      .catch(() => setDomainOptions([]));
  }, []);

  // 支持从全局搜索 / 生命周期信号条经 URL 直达（?kw= 或 ?status=）
  useEffect(() => {
    const kw = searchParams.get("kw");
    const st = searchParams.get("status");
    if (kw) {
      setKeyword(kw);
      setPage(1);
    }
    if (st) {
      setStatus(st);
      setPage(1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  async function load() {
    setLoading(true);
    try {
      const res = await listMetrics({
        keyword,
        status,
        domain: domain || undefined,
        metric_tier: tier || undefined,
        sort_by: sortBy,
        sort_order: sortOrder,
        page,
        page_size: pageSize,
      });
      setItems(res.items);
      setTotal(res.total);
      setSelected([]);
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败",
      );
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize, status, domain, tier, sortBy, sortOrder]);

  function handleSearch() {
    if (keyword) {
      track("metric_search", undefined, "metric", { keyword });
    }
    setPage(1);
    load();
  }

  // CSV 导出：对当前筛选结果（当前页）生成可审计清单
  function exportCsv() {
    const header = ["metric_code", "name", "domain", "type", "status", "metric_tier", "pii_flag", "version", "updated_at"];
    const rows = items.map((m) =>
      [m.metric_code, m.name, m.domain, m.type, m.status, m.metric_tier, m.pii_flag ? "PII" : "", m.version, m.updated_at]
        .map((c) => `"${String(c).replace(/"/g, '""')}"`)
        .join(","),
    );
    const blob = new Blob([[header.join(","), ...rows].join("\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `metric-catalog-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const columns = [
    {
      title: "编码",
      dataIndex: "metric_code",
      key: "metric_code",
      width: 190,
      render: (text: string) => (
        <Button type="link" style={{ padding: 0 }} onClick={(e) => { e.stopPropagation(); navigate(`/detail/${text}`); }}>
          {text}
        </Button>
      ),
    },
    { title: "名称", dataIndex: "name", key: "name" },
    { title: "域", dataIndex: "domain", key: "domain", width: 100 },
    { title: "类型", dataIndex: "type", key: "type", width: 90, render: (v: string) => METRIC_TYPE_LABEL[v] ?? v },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (s: string) => (
        <Tag color={STATUS_COLOR[s]}>{STATUS_LABEL[s] ?? s}</Tag>
      ),
    },
    { title: "分级", dataIndex: "metric_tier", key: "tier", width: 70, render: (v: string) => <Tag>{METRIC_TIER_LABEL[v] ?? v}</Tag> },
    {
      title: "治理徽章",
      key: "badges",
      width: 170,
      render: (_: unknown, r: MetricResponse) => (
        <Space size={4} wrap>
          {r.pii_flag && (
            <Tag color={r.compliance_reviewed ? "green" : "orange"}>{r.compliance_reviewed ? "PII 已复核" : "PII 待复核"}</Tag>
          )}
          {r.emergency_publish && <Tag color="volcano">紧急</Tag>}
          {r.pending_conflict && <Tag color="orange">冲突</Tag>}
          {r.gray_tenant_ids && r.gray_tenant_ids.length > 0 && <Tag color="purple">灰度</Tag>}
          {!r.pii_flag && !r.emergency_publish && !r.pending_conflict && !r.gray_tenant_ids && <span className="muted">—</span>}
        </Space>
      ),
    },
    {
      title: "版本",
      dataIndex: "version",
      key: "version",
      width: 70,
      render: (v: number) => `v${v}`,
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      key: "updated_at",
      width: 170,
      render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span>,
    },
  ];

  const hasFilter = Boolean(keyword || status || domain || tier);
  const emptyGuide = useMemo(
    () => (
      <div style={{ padding: "16px 0", textAlign: "center" }}>
        <p className="muted">{hasFilter ? "没有匹配的指标，试试放宽筛选条件" : "目录还是空的，创建第一个指标或从模板开始"}</p>
        <Space>
          <Button type="primary" icon={<PlusCircleOutlined />} onClick={() => navigate("/create")}>
            创建指标
          </Button>
          <Button icon={<FileTextOutlined />} onClick={() => navigate("/templates")}>
            从模板创建
          </Button>
        </Space>
      </div>
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [hasFilter],
  );

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Assets / Catalog</div>
          <h2>指标目录</h2>
          <p>全量指标定义——按状态/域/分级/关键词检索，点击进入详情。</p>
        </div>
        <Space wrap>
          <Tooltip title="将当前筛选结果导出为 CSV">
            <Button icon={<DownloadOutlined />} onClick={exportCsv} disabled={!items.length}>
              导出
            </Button>
          </Tooltip>
          <Button
            type="primary"
            icon={<ColumnWidthOutlined />}
            disabled={selected.length !== 2}
            onClick={() => selected.length === 2 && navigate(`/compare?a=${selected[0].metric_code}&b=${selected[1].metric_code}`)}
          >
            对比所选{selected.length === 2 ? `（${selected[0].metric_code} ↔ ${selected[1].metric_code}）` : ` (${selected.length}/2)`}
          </Button>
        </Space>
      </div>

      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          placeholder="搜索指标名/编码"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          onPressEnter={handleSearch}
          prefix={<SearchOutlined />}
          style={{ width: 220 }}
        />
        <Button type="primary" onClick={handleSearch} icon={<SearchOutlined />}>
          搜索
        </Button>
        <Select
          value={domain || undefined}
          onChange={(v) => {
            setDomain(v || "");
            setPage(1);
          }}
          style={{ width: 130 }}
          allowClear
          placeholder="全部域"
          options={domainOptions}
        />
        <Select
          value={status || undefined}
          onChange={(v) => {
            setStatus(v || "");
            setPage(1);
          }}
          style={{ width: 130 }}
          allowClear
          placeholder="全部状态"
          options={[
            { value: "DRAFT", label: "草稿" },
            { value: "EXPERIMENTAL", label: "实验" },
            { value: "REVIEW", label: "审核" },
            { value: "PUBLISHED", label: "已发布" },
            { value: "DEPRECATED", label: "已废弃" },
          ]}
        />
        <Select
          value={tier || undefined}
          onChange={(v) => {
            setTier(v || "");
            setPage(1);
          }}
          style={{ width: 110 }}
          allowClear
          placeholder="全部分级"
          options={TIER_OPTIONS}
        />
        <Select
          value={sortBy}
          onChange={setSortBy}
          style={{ width: 130 }}
          options={SORT_OPTIONS}
        />
        <Button
          size="small"
          type={sortOrder === "asc" ? "primary" : "default"}
          onClick={() => setSortOrder((o) => (o === "asc" ? "desc" : "asc"))}
        >
          {sortOrder === "asc" ? "升序 ↑" : "降序 ↓"}
        </Button>
        <span className="muted">共 {total} 条</span>
      </Space>

      <Table
        dataSource={items}
        columns={columns}
        rowKey="metric_code"
        loading={loading}
        rowSelection={{
          selectedRowKeys: selected.map((s) => s.metric_code),
          onChange: (_, rows) => setSelected(rows),
        }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: [10, 20, 50, 100],
          onChange: (p, ps) => { setPage(p); setPageSize(ps); },
          showTotal: (t) => `共 ${t} 条`,
        }}
        onRow={(record) => ({
          onClick: () => navigate(`/detail/${record.metric_code}`),
          style: { cursor: "pointer" },
        })}
        locale={{ emptyText: emptyGuide }}
      />
    </div>
  );
}
