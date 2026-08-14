import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Space, Alert, Tooltip, Drawer, Empty } from "antd";
import { PlusOutlined, ReloadOutlined, DeleteOutlined, EyeOutlined } from "@ant-design/icons";
import { listCatalogs, registerCatalog, bulkDeprecateCatalogs, listDataSources, listCatalogDatabases, inferColumnDescription, inferDescriptions, updateColumnDescription, UnisenseApiError } from "../api";
import type { DBCatalog, DataSource, SchemaColumn } from "../types";
import { enumLabel, ENTITY_TYPE_LABEL } from "../utils/enums";
import { SchemaTable } from "../components/SchemaTable";

const SENSITIVITY_LABEL: Record<string, string> = {
  PUBLIC: "公开",
  INTERNAL: "内部",
  CONFIDENTIAL: "机密",
  PII: "PII",
  NEEDS_REVIEW: "待复核",
  UNKNOWN: "未知",
};
const SENSITIVITY_COLOR: Record<string, string> = {
  PUBLIC: "default",
  INTERNAL: "blue",
  CONFIDENTIAL: "orange",
  PII: "red",
  NEEDS_REVIEW: "gold",
  UNKNOWN: "default",
};

/**
 * 模块级推断去重：退出页面再进入时组件内 loading 会丢失，但该 Map 跨组件实例保留，
 * 进行中的推断未完成时拦截重复点击（后端另有 Redis/进程内 409 幂等兜底）。
 */
const inferInflight = new Map<string, Promise<unknown>>();

/** 若 key 对应推断已在途中则返回 null（拦截）；否则执行并登记，完成时清理。 */
function runInflight<T>(key: string, task: () => Promise<T>): Promise<T> | null {
  if (inferInflight.has(key)) return null;
  const p = task().finally(() => inferInflight.delete(key));
  inferInflight.set(key, p);
  return p;
}

/** 后端 409 LLM_INFER_IN_PROGRESS：已有推断进行中（可能是其它会话/进程触发）。 */
function isInferInProgress(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    (err as { code?: string }).code === "LLM_INFER_IN_PROGRESS"
  );
}

export function Catalogs() {
  const [searchParams] = useSearchParams();
  // URL 直达参数（?kw= / ?source_id=）作为初始筛选，避免「先查全量再过滤」的竞态覆盖
  const urlKw = searchParams.get("kw") ?? "";
  const urlSourceId = searchParams.get("source_id") ?? "";
  // 敏感级别下钻（?sensitivity=，总览仪表「数据表」资产卡片）作为初始筛选
  const urlSensitivity = searchParams.get("sensitivity") ?? "";
  const [items, setItems] = useState<DBCatalog[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [sourceId, setSourceId] = useState(urlSourceId);
  const [sourceStatus, setSourceStatus] = useState<"" | "active" | "deleted">("");
  const [entityType, setEntityType] = useState("");
  const [sensitivity, setSensitivity] = useState(urlSensitivity);
  const [keyword, setKeyword] = useState(urlKw);
  // 库名筛选（随数据源联动）
  const [database, setDatabase] = useState("");
  const [databases, setDatabases] = useState<string[]>([]);
  const [databasesLoading, setDatabasesLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();
  // 并发查询防竞态：只有最后一次发起的请求允许落地结果
  const loadSeq = useRef(0);
  // 数据源选项（登记实体时选择归属数据源，source_id 由系统自动填充，无需手填）
  const [sources, setSources] = useState<DataSource[]>([]);
  const [sourcesLoading, setSourcesLoading] = useState(false);
  // 字段详情抽屉
  const [fieldDrawerOpen, setFieldDrawerOpen] = useState(false);
  const [fieldDrawerCatalog, setFieldDrawerCatalog] = useState<DBCatalog | null>(null);
  const [fieldColumns, setFieldColumns] = useState<SchemaColumn[]>([]);
  const [inferLoading, setInferLoading] = useState(false);
  const [batchInferLoading, setBatchInferLoading] = useState(false);

  function openFieldDetail(record: DBCatalog) {
    setFieldDrawerCatalog(record);
    setFieldColumns(parseSchemaColumns(record));
    setFieldDrawerOpen(true);
  }

  /** 解析 catalog 的 schema_def.columns 为 SchemaColumn[] */
  function parseSchemaColumns(catalog: DBCatalog): SchemaColumn[] {
    const schemaDef = catalog.schema_def as Record<string, unknown>;
    if (!schemaDef || typeof schemaDef !== "object") return [];
    const columns = schemaDef.columns || schemaDef.fields;
    if (!Array.isArray(columns)) return [];
    return columns.map((col: unknown) => {
      if (typeof col === "object" && col !== null) {
        const c = col as Record<string, unknown>;
        return {
          name: String(c.name || c.column || ""),
          type: c.type ? String(c.type) : c.data_type ? String(c.data_type) : undefined,
          comment: c.comment ? String(c.comment) : undefined,
          nullable: c.nullable != null ? Boolean(c.nullable) : undefined,
          default: c.default != null ? String(c.default) : undefined,
        } as SchemaColumn;
      }
      return { name: String(col) } as SchemaColumn;
    });
  }

  /** 刷新字段列表（推断/编辑后更新某字段的描述） */
  function updateFieldDescription(columnName: string, description: string, source: string) {
    setFieldColumns((prev) =>
      prev.map((col) =>
        col.name === columnName
          ? { ...col, description, description_source: source as "manual" | "llm" | "schema" }
          : col,
      ),
    );
  }

  /** 单字段推断 */
  async function handleInfer(col: SchemaColumn) {
    if (!fieldDrawerCatalog) return;
    setInferLoading(true);
    try {
      const catalogId = (fieldDrawerCatalog as DBCatalog & { id?: number }).id ?? 0;
      if (!catalogId) {
        message.warning("该目录实体缺少 ID，无法推断");
        return;
      }
      const key = `column:${catalogId}:${col.name}`;
      const p = runInflight(key, () =>
        inferColumnDescription(catalogId, col.name, {
          entity_name: fieldDrawerCatalog.entity_name,
          column_type: col.type,
        }).then((result) => {
          updateFieldDescription(col.name, result.description, result.source);
          message.success(`字段「${col.name}」推断成功`);
        }),
      );
      if (!p) {
        message.info("该字段的 LLM 推断正在进行中，请稍候");
        return;
      }
      await p;
    } catch (err) {
      if (isInferInProgress(err)) {
        message.info("该字段的 LLM 推断正在进行中，请稍候");
      } else {
        message.error(
          err instanceof UnisenseApiError
            ? `${err.message}（${err.codeZh}）`
            : "LLM 推断暂时不可用，请稍后重试",
        );
      }
    } finally {
      setInferLoading(false);
    }
  }

  /** 批量推断 */
  async function handleBatchInfer() {
    if (!fieldDrawerCatalog) return;
    setBatchInferLoading(true);
    try {
      const catalogId = (fieldDrawerCatalog as DBCatalog & { id?: number }).id ?? 0;
      if (!catalogId) {
        message.warning("该目录实体缺少 ID，无法推断");
        return;
      }
      const key = `batch:${catalogId}`;
      const p = runInflight(key, () =>
        inferDescriptions(catalogId).then((result) => {
          // 更新推断成功的字段
          for (const item of result.inferred) {
            updateFieldDescription(item.column_name, item.description, item.source);
          }
          message.success(
            `推断完成：成功 ${result.inferred.length}，跳过 ${result.skipped.length}，失败 ${result.failed.length}`,
          );
        }),
      );
      if (!p) {
        message.info("该表的批量推断正在进行中，请稍候");
        return;
      }
      await p;
    } catch (err) {
      if (isInferInProgress(err)) {
        message.info("该表的批量推断正在进行中，请稍候");
      } else {
        message.error(
          err instanceof UnisenseApiError
            ? `${err.message}（${err.codeZh}）`
            : "批量推断暂时不可用，请稍后重试",
        );
      }
    } finally {
      setBatchInferLoading(false);
    }
  }

  /** 人工编辑描述 */
  async function handleEdit(col: SchemaColumn, newDesc: string) {
    if (!fieldDrawerCatalog) return;
    const catalogId = (fieldDrawerCatalog as DBCatalog & { id?: number }).id ?? 0;
    if (!catalogId) {
      message.warning("该目录实体缺少 ID，无法编辑");
      return;
    }
    await updateColumnDescription(catalogId, col.name, newDesc);
    updateFieldDescription(col.name, newDesc, "manual");
    message.success(`字段「${col.name}」描述已更新`);
  }

  // 支持从全局搜索栏 / 数据源详情经 ?kw= 或 ?source_id= 直达定位；初始值已由 useState 承接，
  // 此处仅同步「URL 出现新筛选值」的场景，并保留用户手动清空筛选的能力。
  useEffect(() => {
    if (urlKw && urlKw !== keyword) setKeyword(urlKw);
    if (urlSourceId && urlSourceId !== sourceId) setSourceId(urlSourceId);
    if (urlKw || urlSourceId) setPage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlKw, urlSourceId]);

  // 响应 URL 敏感级别参数变化（总览仪表「数据表」资产卡片二次下钻）；sensitivity 在 load 依赖中自动重查
  useEffect(() => {
    if (urlSensitivity && urlSensitivity !== sensitivity) {
      setSensitivity(urlSensitivity);
      setPage(1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlSensitivity]);

  async function load() {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      const res = await listCatalogs({
        source_id: sourceId || undefined,
        entity_type: entityType || undefined,
        sensitivity_level: sensitivity || undefined,
        database: database || undefined,
        keyword: keyword || undefined,
        source_status: sourceStatus || undefined,
        page,
        page_size: pageSize,
      });
      // 已有更新的请求发起，丢弃本次过时响应（防竞态覆盖）
      if (seq !== loadSeq.current) return;
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      if (seq !== loadSeq.current) return;
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize, sourceId, sourceStatus, entityType, sensitivity, keyword, database]);

  // 库名选项随数据源联动：切换数据源时刷新库名下拉并重置已选库名
  async function loadDatabases() {
    setDatabasesLoading(true);
    try {
      setDatabases(await listCatalogDatabases(sourceId || undefined));
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载库名失败");
      setDatabases([]);
    } finally {
      setDatabasesLoading(false);
    }
  }

  useEffect(() => {
    loadDatabases();
    setDatabase("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceId]);

  async function loadSources() {
    setSourcesLoading(true);
    try {
      const res = await listDataSources({ page: 1, page_size: 200 });
      setSources(res.items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载数据源失败");
    } finally {
      setSourcesLoading(false);
    }
  }

  async function handleRegister(values: Record<string, unknown>) {
    try {
      // source_id 由选中的数据源自动填充，系统不再要求手填
      const sid = String(values.source_id);
      await registerCatalog(sid, {
        entity_name: String(values.entity_name),
        entity_type: String(values.entity_type ?? "TABLE"),
        schema_def: {},
        etl_sql: values.etl_sql ? String(values.etl_sql) : null,
      });
      message.success("目录实体已登记");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "登记失败");
    }
  }

  async function handleBulkDeprecate() {
    if (selectedRowKeys.length === 0) return;
    const selected = items.filter((i) => selectedRowKeys.includes(`${i.source_id}-${i.entity_name}`));
    try {
      const res = await bulkDeprecateCatalogs(selected.map((i) => ({ source_id: i.source_id, entity_name: i.entity_name })));
      message.success(`批量废弃成功 ${res.succeeded.length} 项${res.failed.length ? `，失败 ${res.failed.length}` : ""}`);
      setSelectedRowKeys([]);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量废弃失败");
    }
  }

  const columns = [
    {
      title: "数据源",
      dataIndex: "source_id",
      key: "source_id",
      render: (v: string, r: DBCatalog) => (
        <span>
          <span className="mono">{r.source_name ?? v}</span>
          {r.source_deleted && <Tag color="default" style={{ marginLeft: 6 }}>源已删除</Tag>}
          {r.source_name && r.source_name !== v && (
            <div className="muted mono" style={{ fontSize: 11 }}>{v}</div>
          )}
        </span>
      ),
    },
    { title: "实体", dataIndex: "entity_name", key: "entity_name", ellipsis: true, render: (v: string) => <span className="mono">{v}</span> },
    { title: "类型", dataIndex: "entity_type", key: "type", width: 90, render: (v: string) => <Tag>{enumLabel(ENTITY_TYPE_LABEL, v)}</Tag> },
    {
      title: "敏感度",
      dataIndex: "sensitivity_level",
      key: "sensitivity",
      width: 110,
      render: (v: string) => <Tag color={SENSITIVITY_COLOR[v]}>{SENSITIVITY_LABEL[v] ?? v}</Tag>,
    },
    { title: "责任人", dataIndex: "owner_id", key: "owner", width: 90, render: (v: number | null) => v ?? <Tag>无</Tag> },
    { title: "schema 缺失", dataIndex: "schema_incomplete", key: "incomplete", width: 120, render: (v: boolean) => (v ? <Tag color="error">是</Tag> : <Tag color="success">否</Tag>) },
    { title: "上游签名", dataIndex: "upstream_signature", key: "upstream", width: 130, render: (v: string) => (v ? (
      <Tooltip title={v}><span className="mono" style={{ fontSize: 11 }}>{v.slice(0, 12)}…</span></Tooltip>
    ) : <span className="muted">—</span>) },
    {
      title: "操作",
      key: "action",
      width: 100,
      render: (_: unknown, record: DBCatalog) => (
        <Button
          type="link"
          size="small"
          icon={<EyeOutlined />}
          onClick={() => openFieldDetail(record)}
        >
          字段详情
        </Button>
      ),
    },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Collection / Catalog</div>
          <h2>采集目录</h2>
          <p>采集器登记的元数据目录——含敏感度分级与 schema 完整性标记。</p>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>登记实体</Button>
      </div>

      <Card
        extra={
          <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
            刷新
          </Button>
        }
      >
        <Space style={{ marginBottom: 12 }} wrap>
          <Input
            placeholder="Source ID"
            className="mono"
            style={{ width: 150 }}
            value={sourceId}
            onChange={(e) => setSourceId(e.target.value)}
            onPressEnter={() => { setPage(1); load(); }}
          />
          <Select
            allowClear
            placeholder="全部源状态"
            style={{ width: 130 }}
            value={sourceStatus || undefined}
            onChange={(v) => { setSourceStatus((v as "" | "active" | "deleted") || ""); setPage(1); }}
            options={[
              { value: "active", label: "活跃源" },
              { value: "deleted", label: "已删除源" },
            ]}
          />
          <Select
            allowClear
            placeholder="全部库名"
            style={{ width: 160 }}
            loading={databasesLoading}
            disabled={!sourceId && databases.length === 0}
            value={database || undefined}
            onChange={(v) => { setDatabase(v || ""); setPage(1); }}
            options={databases.map((d) => ({ value: d, label: d }))}
            notFoundContent={databasesLoading ? <span>加载中…</span> : <span>无可用库名</span>}
          />
          <Select
            allowClear
            placeholder="全部类型"
            style={{ width: 120 }}
            value={entityType || undefined}
            onChange={(v) => { setEntityType(v || ""); setPage(1); }}
            options={["TABLE", "VIEW", "FIELD"].map((v) => ({ value: v, label: ENTITY_TYPE_LABEL[v] ?? v }))}
          />
          <Select
            allowClear
            placeholder="全部敏感度"
            style={{ width: 140 }}
            value={sensitivity || undefined}
            onChange={(v) => { setSensitivity(v || ""); setPage(1); }}
            options={Object.entries(SENSITIVITY_LABEL).map(([v, l]) => ({ value: v, label: l }))}
          />
          <Input.Search
            placeholder="搜索实体"
            style={{ width: 200 }}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onSearch={() => { setPage(1); load(); }}
          />
          {selectedRowKeys.length > 0 && (
            <Button danger icon={<DeleteOutlined />} onClick={handleBulkDeprecate}>
              批量废弃（{selectedRowKeys.length}）
            </Button>
          )}
        </Space>

        <Table
          dataSource={items}
          columns={columns}
          rowKey={(r) => `${r.source_id}-${r.entity_name}`}
          loading={loading}
          rowSelection={{ selectedRowKeys, onChange: setSelectedRowKeys }}
          pagination={{ current: page, pageSize, total, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100], onChange: (p, ps) => { setPage(p); setPageSize(ps); }, showTotal: (t) => `共 ${t} 条` }}
          locale={{ emptyText: "暂无目录实体" }}
        />
      </Card>

      <Modal
        title="登记目录实体"
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        okText="登记"
        afterOpenChange={(open) => {
          if (open) loadSources();
        }}
      >
        <Form form={form} layout="vertical" onFinish={handleRegister} style={{ marginTop: 8 }}>
          <Form.Item name="source_id" label="数据源" rules={[{ required: true, message: "请选择归属的数据源" }]}>
            <Select
              showSearch
              loading={sourcesLoading}
              placeholder="选择数据源（source_id 自动填充）"
              optionFilterProp="label"
              options={sources.map((s) => ({ value: s.source_id, label: `${s.name}（${s.source_id}）` }))}
            />
          </Form.Item>
          <Form.Item name="entity_name" label="实体名" rules={[{ required: true }]}>
            <Input className="mono" placeholder="如 dwd_finance_order" />
          </Form.Item>
          <Form.Item name="entity_type" label="类型" initialValue="TABLE">
            <Select options={["TABLE", "VIEW", "FIELD"].map((v) => ({ value: v, label: ENTITY_TYPE_LABEL[v] ?? v }))} />
          </Form.Item>
          <Form.Item name="etl_sql" label="ETL SQL（可选）">
            <Input.TextArea rows={3} className="mono" />
          </Form.Item>
          <Alert type="info" showIcon message="source_id 由所选数据源自动确定，无需手填；schema_def 可在采集时自动填充。" />
        </Form>
      </Modal>

      <Drawer
        title={fieldDrawerCatalog ? `字段详情：${fieldDrawerCatalog.entity_name}` : "字段详情"}
        open={fieldDrawerOpen}
        onClose={() => { setFieldDrawerOpen(false); setFieldDrawerCatalog(null); setFieldColumns([]); }}
        width={720}
        destroyOnClose={false}
      >
        {fieldDrawerCatalog && (() => {
          const isSchemaIncomplete = fieldDrawerCatalog.schema_incomplete;
          const hasNoSchema = fieldColumns.length === 0;

          return (
            <>
              {isSchemaIncomplete && !hasNoSchema && (
                <Alert
                  type="warning"
                  showIcon
                  message="Schema 不完整，部分字段信息缺失"
                  style={{ marginBottom: 12 }}
                />
              )}
              {hasNoSchema ? (
                <Empty description="暂无字段信息，请先执行采集" />
              ) : (
                <SchemaTable
                  columns={fieldColumns}
                  editable={true}
                  inferable={true}
                  onEdit={handleEdit}
                  onInfer={handleInfer}
                  onBatchInfer={handleBatchInfer}
                  loading={inferLoading || batchInferLoading}
                />
              )}
            </>
          );
        })()}
      </Drawer>
    </div>
  );
}
