import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Table, Input, Select, Button, Space, Tag, message, Tooltip, Descriptions } from "antd";
import {
  SearchOutlined,
  ColumnWidthOutlined,
  PlusCircleOutlined,
  FileTextOutlined,
  DownloadOutlined,
} from "@ant-design/icons";
import { fetchDashboard, listDomainTree, listMetrics, listUsers, UnisenseApiError } from "../api";
import type { MetricResponse, SubjectDomainTreeNode } from "../types";
import { useTracking } from "../hooks/useTracking";
import {
  AGGREGATION_LABEL,
  DW_LAYER_LABEL,
  FRESHNESS_LABEL,
  GRANULARITY_LABEL,
  METRIC_TYPE_LABEL,
  METRIC_TIER_LABEL,
  TIME_SEMANTICS_LABEL,
} from "../utils/enums";

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

// 递归展平主题域树 → code → 中文名 映射
function flattenDomains(nodes: SubjectDomainTreeNode[], acc: Map<string, string>) {
  for (const n of nodes) {
    acc.set(n.code, n.name);
    if (n.children?.length) flattenDomains(n.children, acc);
  }
}

// 口径摘要：聚合(字段) · 粒度 · 单位 —— 指标"怎么算的"浓缩成一行可扫读
function calibreSummary(r: MetricResponse): string {
  const agg = AGGREGATION_LABEL[r.aggregation] ?? r.aggregation;
  const gran = GRANULARITY_LABEL[r.granularity] ?? r.granularity;
  return `${agg} · ${gran} · ${r.unit}`;
}

// 展开行：完整口径定义 + 治理追溯（责任人/备份/提交人/审批人/创建时间）
function ExpandContent({
  r,
  userName,
  domainName,
}: {
  r: MetricResponse;
  userName: (id: number | null | undefined) => string;
  domainName: (code: string) => string;
}) {
  const def = r.definition_json ?? {};
  const expression = typeof def.expression === "string" ? def.expression : undefined;
  const definition = typeof def.definition === "string" ? def.definition : undefined;
  const dependencies = Array.isArray(def.dependencies) ? def.dependencies.map((d) => String(d)) : [];
  const rawSource = def.source_fields ?? def.source_columns;
  const sourceFields = Array.isArray(rawSource) ? rawSource.map((s) => String(s)) : rawSource ? [String(rawSource)] : [];
  const sourceTables = Array.isArray(def.source_tables)
    ? def.source_tables.map((s) => String(s))
    : def.source_tables
      ? [String(def.source_tables)]
      : [];
  const rawEtl = def.etl_sql ?? def.sql;
  const etlSql = rawEtl == null ? "" : String(rawEtl);

  return (
    <div style={{ padding: "4px 8px" }}>
      <Descriptions column={2} size="small" bordered style={{ marginBottom: 12 }}>
        <Descriptions.Item label="业务域">{domainName(r.domain)}</Descriptions.Item>
        <Descriptions.Item label="指标类型">{METRIC_TYPE_LABEL[r.type] ?? r.type}</Descriptions.Item>
        <Descriptions.Item label="责任人">{userName(r.owner_id)}</Descriptions.Item>
        <Descriptions.Item label="备份责任人">{userName(r.backup_owner_id)}</Descriptions.Item>
        <Descriptions.Item label="提交人">{userName(r.submitted_by)}</Descriptions.Item>
        <Descriptions.Item label="审批人">{userName(r.approver_id)}</Descriptions.Item>
        <Descriptions.Item label="创建时间">
          <span className="mono" style={{ fontSize: 12 }}>{r.created_at}</span>
        </Descriptions.Item>
        <Descriptions.Item label="数据分层">{DW_LAYER_LABEL[r.dw_layer] ?? r.dw_layer}</Descriptions.Item>
        <Descriptions.Item label="更新时效">{FRESHNESS_LABEL[r.freshness] ?? r.freshness}</Descriptions.Item>
        <Descriptions.Item label="时间语义">{TIME_SEMANTICS_LABEL[r.time_semantics] ?? r.time_semantics}</Descriptions.Item>
      </Descriptions>
      {definition && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">指标定义：</span>
          {definition}
        </p>
      )}
      {expression && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">计算口径：</span>
          <code className="mono">{expression}</code>
        </p>
      )}
      {sourceTables.length > 0 && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">关联数据表：</span>
          {sourceTables.map((t) => (
            <Tag key={t} className="mono">{t}</Tag>
          ))}
        </p>
      )}
      {dependencies.length > 0 && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">依赖指标：</span>
          {dependencies.map((d) => (
            <Tag key={d}>{d}</Tag>
          ))}
        </p>
      )}
      {sourceFields.length > 0 && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">来源字段：</span>
          {sourceFields.map((s) => (
            <Tag key={s}>{s}</Tag>
          ))}
        </p>
      )}
      {etlSql && (
        <pre
          style={{
            background: "var(--paper)",
            padding: 8,
            borderRadius: 4,
            margin: "0 0 8px",
            fontSize: 12,
            overflow: "auto",
            maxHeight: 200,
          }}
        >
          {etlSql}
        </pre>
      )}
      <details>
        <summary className="muted" style={{ cursor: "pointer" }}>完整口径 JSON</summary>
        <pre
          style={{
            background: "var(--paper)",
            padding: 8,
            borderRadius: 4,
            margin: "8px 0 0",
            fontSize: 12,
            overflow: "auto",
            maxHeight: 240,
          }}
        >
          {JSON.stringify(def, null, 2)}
        </pre>
      </details>
    </div>
  );
}

export function MetricCatalog() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { track } = useTracking();
  // URL 直达参数（?kw= / ?status=）作为初始筛选，避免「先查全量再过滤」的竞态覆盖
  const urlKw = searchParams.get("kw") ?? "";
  const urlStatus = searchParams.get("status") ?? "";
  const [items, setItems] = useState<MetricResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [keyword, setKeyword] = useState(urlKw);
  const [status, setStatus] = useState(urlStatus);
  const [domain, setDomain] = useState("");
  const [tier, setTier] = useState("");
  const [sortBy, setSortBy] = useState<"updated_at" | "created_at" | "version" | "metric_code" | "name">("updated_at");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");
  const [domainOptions, setDomainOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [selected, setSelected] = useState<MetricResponse[]>([]);
  const [loading, setLoading] = useState(false);
  // 用户 id → 显示名 映射（责任人/提交人/审批人中文名）
  const [userMap, setUserMap] = useState<Map<number, string>>(new Map());
  // 域 code → 中文名 映射
  const [domainMap, setDomainMap] = useState<Map<string, string>>(new Map());
  // 并发查询防竞态：只有最后一次发起的请求允许落地结果
  const loadSeq = useRef(0);

  // 域列表从真实 dashboard by_domain 聚合（不硬编码）
  useEffect(() => {
    fetchDashboard()
      .then((d) => setDomainOptions(Object.keys(d.by_domain ?? {}).map((v) => ({ value: v, label: v }))))
      .catch(() => setDomainOptions([]));
  }, []);

  // 责任人/审批人/提交人 中文名映射（真实 listUsers）
  useEffect(() => {
    listUsers()
      .then((u) => setUserMap(new Map(u.map((x) => [x.id, x.display_name || x.username]))))
      .catch(() => setUserMap(new Map()));
  }, []);

  // 业务域中文名映射（真实 listDomainTree，失败回退显示 code）
  useEffect(() => {
    listDomainTree()
      .then((tree) => {
        const m = new Map<string, string>();
        flattenDomains(tree, m);
        setDomainMap(m);
      })
      .catch(() => setDomainMap(new Map()));
  }, []);

  const userName = useMemo(
    () => (id: number | null | undefined) => (id == null ? "—" : (userMap.get(id) ?? `#${id}`)),
    [userMap],
  );
  const domainName = useMemo(
    () => (code: string) => (code ? (domainMap.get(code) ?? code) : "—"),
    [domainMap],
  );

  // 响应 URL 直达参数变化（全局搜索 / 生命周期信号条 SPA 内跳转）；初始值已由 useState 承接，
  // 此处仅同步「URL 出现新筛选值」的场景，并保留用户手动清空筛选的能力。
  useEffect(() => {
    if (urlKw && urlKw !== keyword) setKeyword(urlKw);
    if (urlStatus && urlStatus !== status) setStatus(urlStatus);
    if (urlKw || urlStatus) setPage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlKw, urlStatus]);

  async function load() {
    const seq = ++loadSeq.current;
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
      // 已有更新的请求发起，丢弃本次过时响应（防竞态覆盖）
      if (seq !== loadSeq.current) return;
      setItems(res.items);
      setTotal(res.total);
      setSelected([]);
    } catch (err) {
      if (seq !== loadSeq.current) return;
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败",
      );
    } finally {
      if (seq === loadSeq.current) setLoading(false);
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
    const header = [
      "metric_code", "name", "domain", "owner_id", "type", "status",
      "aggregation", "granularity", "unit", "dw_layer", "metric_tier",
      "pii_flag", "version", "created_at", "updated_at",
    ];
    const rows = items.map((m) =>
      [
        m.metric_code, m.name, m.domain, m.owner_id, m.type, m.status,
        m.aggregation, m.granularity, m.unit, m.dw_layer, m.metric_tier,
        m.pii_flag ? "PII" : "", m.version, m.created_at, m.updated_at,
      ]
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
    { title: "名称", dataIndex: "name", key: "name", ellipsis: true },
    {
      title: "业务域",
      dataIndex: "domain",
      key: "domain",
      width: 110,
      render: (v: string) => domainName(v),
    },
    {
      title: "责任人",
      key: "owner",
      width: 110,
      ellipsis: true,
      render: (_: unknown, r: MetricResponse) => userName(r.owner_id),
    },
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
    {
      title: "口径摘要",
      key: "calibre",
      width: 180,
      ellipsis: true,
      render: (_: unknown, r: MetricResponse) => {
        const def = r.definition_json ?? {};
        const expr = typeof def.expression === "string" ? def.expression : undefined;
        const text = calibreSummary(r);
        return expr ? (
          <Tooltip title={`计算口径：${expr}`}>
            <span style={{ fontSize: 12 }}>{text}</span>
          </Tooltip>
        ) : (
          <span style={{ fontSize: 12 }}>{text}</span>
        );
      },
    },
    {
      title: "分层",
      dataIndex: "dw_layer",
      key: "dw_layer",
      width: 110,
      render: (v: string) => DW_LAYER_LABEL[v] ?? v,
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
          <p>全量指标定义——按状态/域/分级/关键词检索；展开行查看口径与治理追溯，点击进入详情。</p>
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
        expandable={{
          expandedRowRender: (r) => <ExpandContent r={r} userName={userName} domainName={domainName} />,
        }}
        scroll={{ x: 1500 }}
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
