import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert, Card, Table, Tag, Input, Select, Button, Space, Tabs, Tooltip, Modal, Descriptions, message } from "antd";
import { DownloadOutlined, ReloadOutlined, SearchOutlined } from "@ant-design/icons";
import { exportAudit, listAudit, UnisenseApiError } from "../api";
import type { AuditEntry } from "../types";
import {
  AUDIT_ENTITY_TYPES,
  AUDIT_FIELD_LABEL,
  auditValueText,
  entityTypeLabel,
  auditActionLabel,
  cleanEntityId,
} from "../utils/auditI18n";
import { formatCnTime } from "../utils/timeCn";
import { usePermission } from "../hooks/usePermission";
import { usePersistentPageSize } from "../hooks/usePersistentPageSize";

// 实体类型筛选选项：集中常量对齐后端全部 entity_type（见 auditI18n.AUDIT_ENTITY_TYPES）
const AUDIT_ENTITY_OPTIONS = AUDIT_ENTITY_TYPES.map((v) => ({ value: v, label: entityTypeLabel(v) }));

// 合规报告辅助：导出动作判定（audit.export / pii_export 等一切含 export 的动作）
function isExportEntry(e: AuditEntry): boolean {
  return e.action.includes("export");
}

// 敏感数据访问按操作人聚合：谁访问过、访问次数、最近一次访问时间
function groupSensitiveAccess(items: AuditEntry[]): Array<{ actor: string; count: number; last_at: string }> {
  const map = new Map<string, { count: number; last_at: string }>();
  for (const e of items) {
    if (!e.pii_access) continue;
    const actor = e.actor_display ?? "未知用户";
    const prev = map.get(actor);
    if (!prev) {
      map.set(actor, { count: 1, last_at: e.created_at });
    } else {
      prev.count += 1;
      if (e.created_at > prev.last_at) prev.last_at = e.created_at;
    }
  }
  return Array.from(map.entries()).map(([actor, v]) => ({ actor, count: v.count, last_at: v.last_at }));
}

// 合规报告视图：敏感指标访问留痕（谁访问过）+ 审计导出记录，一键聚合（前端基于现有 audit API 数据）
const SENSITIVE_ACCESS_COLUMNS = [
  { title: "操作人", dataIndex: "actor", key: "actor" },
  { title: "访问次数", dataIndex: "count", key: "count", width: 110 },
  {
    title: "最近访问",
    dataIndex: "last_at",
    key: "last_at",
    render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span>,
  },
];

const EXPORT_RECORD_COLUMNS = [
  {
    title: "操作人",
    dataIndex: "actor_display",
    key: "actor",
    render: (v: string) => (
      <span className="mono" style={{ fontSize: 12 }}>{v ?? "未知用户"}</span>
    ),
  },
  {
    title: "导出内容",
    dataIndex: "entity_id",
    key: "entity",
    render: (v: string, r: AuditEntry) => (
      <span>
        <Tag>{entityTypeLabel(r.entity_type)}</Tag>
        <span className="mono" style={{ fontSize: 12, marginLeft: 4 }}>{cleanEntityId(v)}</span>
      </span>
    ),
  },
  {
    title: "操作时间",
    dataIndex: "created_at",
    key: "created",
    render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span>,
  },
];

function ComplianceReport({
  sensitiveAccess,
  exportRecords,
  loading,
  onRowClick,
}: {
  sensitiveAccess: AuditEntry[];
  exportRecords: AuditEntry[];
  loading: boolean;
  onRowClick?: (record: AuditEntry) => void;
}) {
  const accessRows = groupSensitiveAccess(sensitiveAccess);
  // 导出记录表格行可点击看详情（与操作日志同一弹窗）；聚合行（操作人）不可点击
  const exportRowProps = (record: AuditEntry) => ({
    onClick: () => onRowClick?.(record),
    style: { cursor: "pointer" as const },
  });
  return (
    <div>
      <Alert
        type="info"
        showIcon
        message="合规报告（一键聚合）"
        description="基于审计日志 API 最近 100 条检索结果在前端汇总：敏感指标访问留痕（谁访问过）+ 审计导出记录。需要全量明细请在上方「操作日志」Tab 筛选后导出 CSV/JSON。"
        style={{ marginBottom: 16 }}
      />
      <Card size="small" title={`敏感数据访问（${accessRows.length} 位操作人）`} style={{ marginBottom: 16 }}>
        <Table
          dataSource={accessRows}
          rowKey="actor"
          size="small"
          loading={loading}
          pagination={false}
          locale={{ emptyText: "暂无敏感数据访问记录" }}
          columns={SENSITIVE_ACCESS_COLUMNS}
        />
      </Card>
      <Card size="small" title={`审计导出记录（${exportRecords.length}）`}>
        <Table
          dataSource={exportRecords}
          rowKey="id"
          size="small"
          loading={loading}
          onRow={exportRowProps}
          pagination={false}
          locale={{ emptyText: "暂无导出记录" }}
          columns={EXPORT_RECORD_COLUMNS}
        />
      </Card>
    </div>
  );
}

export function AuditLog() {
  const { can } = usePermission();
  const [items, setItems] = useState<AuditEntry[]>([]);
  const [page, setPage] = useState(1);
  // F-1（第十一轮）：每页条数持久化（对齐 MetricCatalog/Dimensions 模式）
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.auditLog.pageSize", 20);
  const setPageSize = (ps: number) => onShowSizeChange(0, ps);
  // F2（审查修复）：支持深链 ?entity_type=xxx 作为初始筛选（SystemConfig「审计记录」
  // 从 LLM 配置页跳转后应直达该实体的审计，此前参数被丢弃落全量页）
  const [searchParams] = useSearchParams();
  const [entityType, setEntityType] = useState<string>(searchParams.get("entity_type") ?? "");
  const [actorKeyword, setActorKeyword] = useState("");
  const [traceId, setTraceId] = useState("");
  const [piiOnly, setPiiOnly] = useState<boolean | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  // 合规报告 Tab 状态：切换时拉取敏感访问 + 导出记录（现有 audit API，前端聚合）
  const [activeTab, setActiveTab] = useState("log");
  const [complianceLoading, setComplianceLoading] = useState(false);
  const [sensitiveAccess, setSensitiveAccess] = useState<AuditEntry[]>([]);
  const [exportRecords, setExportRecords] = useState<AuditEntry[]>([]);
  // 行点击详情弹窗：选中的审计条目（WORM 记录，行数据即完整详情，无需额外请求）
  const [selected, setSelected] = useState<AuditEntry | null>(null);

  // 合规报告聚合：敏感访问（pii_access=true）+ 导出动作（action 含 export），各取最近 100 条
  async function loadCompliance() {
    setComplianceLoading(true);
    try {
      const [pii, all] = await Promise.all([
        listAudit({ pii_access: true, page_size: 100 }).catch(() => ({ items: [] as AuditEntry[] })),
        listAudit({ page_size: 100 }).catch(() => ({ items: [] as AuditEntry[] })),
      ]);
      setSensitiveAccess(pii.items);
      setExportRecords(all.items.filter(isExportEntry));
    } finally {
      setComplianceLoading(false);
    }
  }

  async function load() {
    setLoading(true);
    try {
      const res = await listAudit({
        entity_type: entityType || undefined,
        actor_keyword: actorKeyword.trim() || undefined,
        trace_id: traceId.trim() || undefined,
        pii_access: piiOnly,
        page,
        page_size: pageSize,
      });
      setItems(res.items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  // 导出当前过滤条件下的审计（CSV，合规留档；导出动作本身落审计）
  async function handleExport() {
    setExporting(true);
    try {
      await exportAudit({
        entity_type: entityType || undefined,
        actor_keyword: actorKeyword.trim() || undefined,
        trace_id: traceId.trim() || undefined,
        pii_access: piiOnly,
        format: "csv",
        limit: 5000,
      });
      message.success("已导出 CSV（当前过滤条件下最多 5000 条）");
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "导出失败");
    } finally {
      setExporting(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize, entityType, piiOnly]);

  // 切到「合规报告」Tab 时加载聚合数据（懒加载，避免无关请求）
  useEffect(() => {
    if (activeTab === "compliance") {
      loadCompliance();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  const columns = [
    { title: "编号", dataIndex: "id", key: "id", width: 70 },
    {
      title: "操作者",
      dataIndex: "actor_id",
      key: "actor",
      width: 150,
      render: (_: number, r: AuditEntry) => (
        <span className="mono" style={{ fontSize: 12 }}>{r.actor_display ?? "未知用户"}</span>
      ),
    },
    {
      title: "操作内容",
      dataIndex: "action",
      key: "action",
      render: (v: string, r: AuditEntry) => (
        <Tooltip title={v}>
          {/* 优先展示后端 enrich 的业务中文描述（覆盖全部命名）；缺省回退前端字典 */}
          <span>{r.action_desc ?? auditActionLabel(v)}</span>
        </Tooltip>
      ),
    },
    {
      title: "操作对象",
      key: "entity",
      width: 200,
      render: (_: unknown, r: AuditEntry) => (
        <span>
          <Tag>{entityTypeLabel(r.entity_type)}</Tag>
          <span className="mono" style={{ fontSize: 12, marginLeft: 4 }}>
            {cleanEntityId(r.entity_id)}
          </span>
        </span>
      ),
    },
    {
      title: "操作详情",
      dataIndex: "detail_json",
      key: "detail",
      ellipsis: true,
      render: (v: Record<string, unknown> | null, _r: AuditEntry) =>
        v && Object.keys(v).length > 0 ? (
          <Tooltip
            title={
              <div>
                {Object.entries(v)
                  .filter(([, val]) => val !== null && val !== undefined)
                  .map(([k, val]) => (
                    <div key={k} style={{ marginBottom: 2 }}>
                      <span style={{ fontWeight: 500, marginRight: 6 }}>{AUDIT_FIELD_LABEL[k] ?? k}：</span>
                      <span>{auditValueText(val)}</span>
                    </div>
                  ))}
              </div>
            }
            overlayStyle={{ maxWidth: 400 }}
          >
            <span className="mono" style={{ fontSize: 12, color: "#888" }}>
              {Object.entries(v)
                .filter(([, val]) => val !== null && val !== undefined)
                .slice(0, 2)
                .map(([k, val]) => `${AUDIT_FIELD_LABEL[k] ?? k}:${auditValueText(val)}`)
                .join(" · ") + (Object.keys(v).length > 2 ? " …" : "")}
            </span>
          </Tooltip>
        ) : (
          <span className="muted">—</span>
        ),
    },
    {
      title: "来源地址",
      dataIndex: "ip",
      key: "ip",
      width: 130,
      render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v || "—"}</span>,
    },
    {
      title: "追踪编号",
      dataIndex: "trace_id",
      key: "trace",
      width: 150,
      render: (v: string) => (
        <span className="mono" style={{ fontSize: 12 }}>{v ? v.slice(0, 16) : "—"}</span>
      ),
    },
    {
      title: "敏感数据",
      dataIndex: "pii_access",
      key: "pii",
      width: 100,
      render: (v: boolean) => (v ? <Tag color="red">涉及敏感数据</Tag> : <Tag>非敏感</Tag>),
    },
    {
      title: "操作时间",
      dataIndex: "created_at",
      key: "created",
      width: 170,
      render: (v: string) => (
        <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span>
      ),
    },
  ];

  // 行点击打开详情弹窗（审计为 WORM 只读记录，直接展示行内完整数据）
  const openDetail = (record: AuditEntry) => setSelected(record);
  const rowClickProps = (record: AuditEntry) => ({
    onClick: () => openDetail(record),
    style: { cursor: "pointer" as const },
  });

  // 详情弹窗：基本信息 Descriptions + detail_json 全字段结构化展示
  function renderDetailModal() {
    const e = selected;
    if (!e) return null;
    const detailEntries = Object.entries(e.detail_json ?? {}).filter(
      ([, v]) => v !== null && v !== undefined,
    );
    return (
      <Modal
        open={!!selected}
        title="审计日志详情"
        width={720}
        onCancel={() => setSelected(null)}
        footer={
          <Button type="primary" onClick={() => setSelected(null)}>
            关闭
          </Button>
        }
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={e.action_desc ?? auditActionLabel(e.action)}
          description={`操作时间：${formatCnTime(e.created_at)}`}
        />
        <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
          <Descriptions.Item label="编号">{e.id}</Descriptions.Item>
          <Descriptions.Item label="操作者">{e.actor_display ?? "未知用户"}</Descriptions.Item>
          <Descriptions.Item label="操作对象">
            <Tag>{entityTypeLabel(e.entity_type)}</Tag>
            <span className="mono" style={{ fontSize: 12, marginLeft: 4 }}>{e.entity_id || "—"}</span>
          </Descriptions.Item>
          <Descriptions.Item label="来源地址">{e.ip || "—"}</Descriptions.Item>
          <Descriptions.Item label="追踪编号">
            <span className="mono" style={{ fontSize: 12 }}>{e.trace_id || "—"}</span>
          </Descriptions.Item>
          <Descriptions.Item label="敏感数据">
            {e.pii_access ? <Tag color="red">涉及敏感数据</Tag> : <Tag>非敏感</Tag>}
          </Descriptions.Item>
          <Descriptions.Item label="是否归档">{e.archived ? "是" : "否"}</Descriptions.Item>
          <Descriptions.Item label="原始动作码">
            <span className="mono" style={{ fontSize: 12 }}>{e.action}</span>
          </Descriptions.Item>
        </Descriptions>
        <div style={{ fontWeight: 500, marginBottom: 8 }}>操作详情</div>
        {detailEntries.length > 0 ? (
          <div style={{ maxHeight: 320, overflow: "auto" }}>
            <Descriptions column={1} bordered size="small">
              {detailEntries.map(([k, v]) => (
                <Descriptions.Item key={k} label={AUDIT_FIELD_LABEL[k] ?? k}>
                  <span className="mono" style={{ fontSize: 12, wordBreak: "break-all" }}>
                    {auditValueText(v)}
                  </span>
                </Descriptions.Item>
              ))}
            </Descriptions>
          </div>
        ) : (
          <span className="muted">无附加详情</span>
        )}
      </Modal>
    );
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">合规审计 / 操作留痕</div>
          <h2>审计日志</h2>
          <p>平台所有操作记录——指标创建/发布/废弃、权限变更、PII 访问等均可追溯，满足 GB/T 35273 等保要求。</p>
        </div>
      </div>

      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: "log",
            label: "操作日志",
            children: (
              <Card
                extra={
                  <Space>
                    {can("audit:export") && (
                      <Button icon={<DownloadOutlined />} onClick={handleExport} loading={exporting}>
                        导出 CSV
                      </Button>
                    )}
                    <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
                      刷新
                    </Button>
                  </Space>
                }
              >
        <Space style={{ marginBottom: 12 }} wrap>
          <Select
            allowClear
            placeholder="全部操作对象类型"
            style={{ width: 200 }}
            value={entityType || undefined}
            onChange={(v) => { setEntityType(v || ""); setPage(1); }}
            options={AUDIT_ENTITY_OPTIONS}
          />
          <Input
            allowClear
            placeholder="按操作人姓名搜索"
            className="mono"
            style={{ width: 160 }}
            prefix={<SearchOutlined />}
            value={actorKeyword}
            onChange={(e) => setActorKeyword(e.target.value)}
            onPressEnter={() => { setPage(1); load(); }}
          />
          <Input
            allowClear
            placeholder="按追踪编号搜索"
            className="mono"
            style={{ width: 180 }}
            prefix={<SearchOutlined />}
            value={traceId}
            onChange={(e) => setTraceId(e.target.value)}
            onPressEnter={() => { setPage(1); load(); }}
          />
          <Select
            allowClear
            placeholder="敏感数据筛选"
            style={{ width: 160 }}
            value={piiOnly}
            onChange={(v) => { setPiiOnly(v); setPage(1); }}
            options={[{ value: true, label: "仅涉及敏感数据" }, { value: false, label: "仅非敏感" }]}
          />
        </Space>

        <Table
          dataSource={items}
          columns={columns}
          rowKey="id"
          loading={loading}
          onRow={rowClickProps}
          pagination={{ current: page, pageSize, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100], onChange: (p, ps) => { setPage(p); setPageSize(ps); }, showTotal: (t) => `共 ${t} 条` }}
          locale={{ emptyText: "暂无审计记录" }}
          size="small"
        />
              </Card>
            ),
          },
          {
            key: "compliance",
            label: "合规报告",
            children: (
              <ComplianceReport
                sensitiveAccess={sensitiveAccess}
                exportRecords={exportRecords}
                loading={complianceLoading}
                onRowClick={openDetail}
              />
            ),
          },
        ]}
      />
      {renderDetailModal()}
    </div>
  );
}
