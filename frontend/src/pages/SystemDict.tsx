import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Card, Tabs, Table, Button, Modal, Form, Input, InputNumber, Space, Tag, Select, Popconfirm, Collapse, App as AntApp,
} from "antd";
import { PlusOutlined, EditOutlined, DeleteOutlined, StopOutlined, CheckCircleOutlined, ArrowLeftOutlined, RobotOutlined } from "@ant-design/icons";
import {
  listDictTypes, listAllDictItems, createDictItem, updateDictItem,
  deactivateDictItem, activateDictItem, deleteDictItem,
  batchCreateDictItems, batchToggleDictItems, batchDeleteDictItems,
  inferDictDescription,
  UnisenseApiError,
} from "../api";
import type { DictBatchResult, SystemDictItem } from "../types";
import { slugifyCode, resolveUniqueCode } from "../utils/zhEnDict";
import { usePermission } from "../hooks/usePermission";

const DICT_TYPE_LABELS: Record<string, string> = {
  granularity: "粒度",
  unit: "单位",
  aggregation: "聚合方式",
  time_semantics: "时间语义",
  freshness: "新鲜度",
  dw_layer: "数仓层",
  metric_type: "指标类型",
  additivity: "可加性",
  serving_mode: "服务模式",
  metric_tier: "指标分级",
  currency: "币种",
  // PII 合规增强：敏感规则类别与规则配置（供敏感分级规则引擎 DB 可配置）
  pii_category: "PII 类别",
  pii_rule: "PII 规则",
  // 原子指标口径库：度量分类（dict_type=measure_category，在线增删改/启停用）
  measure_category: "度量分类",
  // 原子指标口径库：度量格式（dict_type=measure_format，extra 携带默认单位/小数位联动）
  measure_format: "度量格式",
  // 原子指标口径库：源头系统（dict_type=source_system，提供候选，保留自由输入）
  source_system: "源头系统",
  // 指标命名词根（dict_type=metric_name_morpheme，0102/0103 迁移种入）：指标命名
  // 校验的受控词根，字典管理在线增删/启停用即时对命名校验生效
  metric_name_morpheme: "指标命名词根",
};

// 打开新增弹窗时静默刷新的最小间隔（毫秒）：TTL 内重复打开直接用缓存，避免
// 无并发场景下每次打开都多发一次 listAllDictItems 请求；超 TTL 才刷新，以
// 缩小「他端新增同名编码但本页未刷新」导致的预览滞后窗口。
export const QUIET_REFRESH_TTL_MS = 30_000;

// 批量新增弹窗单行（label 必填，code 由后端自动生成）
interface BatchCreateRow {
  label: string;
  sort_order: number;
  description: string;
}

// ---- 通用扩展属性（extra）编辑器 ----
// 参照数据字典项的 extra 是通用 JSON 列，但此前编辑 UI 与表格列都硬编码为
// 「度量格式（measure_format）」专用（默认单位/小数位）。这里提供对任意类型
// 通用的 key-value 行编辑器：值支持数字/布尔/JSON 自动识别，可表达嵌套结构。
interface ExtraRow {
  key: string;
  value: string;
}

function extraToRows(extra: Record<string, unknown> | null | undefined): ExtraRow[] {
  if (!extra) return [];
  return Object.entries(extra).map(([k, v]) => ({
    key: k,
    value: typeof v === "object" && v !== null ? JSON.stringify(v) : String(v),
  }));
}

// 值类型自动识别：数字 → number、true/false → boolean、JSON 文本 → 对象/数组，其余原样字符串
function parseExtraValue(raw: string): unknown {
  const t = raw.trim();
  if (t === "") return "";
  if (/^-?\d+(\.\d+)?$/.test(t)) return Number(t);
  if (t === "true") return true;
  if (t === "false") return false;
  if ((t.startsWith("{") && t.endsWith("}")) || (t.startsWith("[") && t.endsWith("]"))) {
    try {
      return JSON.parse(t);
    } catch {
      return t;
    }
  }
  return t;
}

function rowsToExtra(rows: ExtraRow[]): Record<string, unknown> | null {
  const extra: Record<string, unknown> = {};
  for (const r of rows) {
    const k = r.key.trim();
    if (!k) continue;
    extra[k] = parseExtraValue(r.value);
  }
  return Object.keys(extra).length > 0 ? extra : null;
}

// 通用 key-value 扩展属性编辑器。父组件以 key 重挂载驱动初始值（value 仅作初始
// 快照），内部状态自管、编辑即 onChange 通知外部——避免受控 value 回传重建行。
function ExtraEditor({
  value,
  onChange,
}: {
  value?: Record<string, unknown> | null;
  onChange?: (v: Record<string, unknown> | null) => void;
}) {
  const [rows, setRows] = useState<ExtraRow[]>(() => {
    const init = extraToRows(value);
    // 无既有 extra 时也保留一个空行，便于直接填写
    return init.length > 0 ? init : [{ key: "", value: "" }];
  });
  function update(next: ExtraRow[]) {
    setRows(next);
    onChange?.(rowsToExtra(next));
  }
  function addRow() {
    update([...rows, { key: "", value: "" }]);
  }
  function patchRow(i: number, patch: Partial<ExtraRow>) {
    update(rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }
  function removeRow(i: number) {
    update(rows.filter((_, idx) => idx !== i));
  }
  const hasContent = rows.some((r) => r.key.trim() !== "");
  const preview = hasContent ? rowsToExtra(rows) : null;
  return (
    <div>
      {rows.map((r, i) => (
        <div key={i} style={{ display: "flex", gap: 8, marginBottom: 8, alignItems: "baseline" }}>
          <Input
            placeholder="属性名，如 category"
            value={r.key}
            onChange={(e) => patchRow(i, { key: e.target.value })}
            style={{ width: 200, flex: "0 0 200px" }}
            data-testid={`dict-extra-key-${i}`}
          />
          <Input
            placeholder="属性值（数字/布尔/JSON 自动识别）"
            value={r.value}
            onChange={(e) => patchRow(i, { value: e.target.value })}
            style={{ flex: 1, minWidth: 0 }}
            data-testid={`dict-extra-value-${i}`}
          />
          <Button
            type="text"
            danger
            icon={<DeleteOutlined />}
            onClick={() => removeRow(i)}
            disabled={rows.length === 1}
            data-testid={`dict-extra-del-${i}`}
          />
        </div>
      ))}
      <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={addRow} data-testid="dict-extra-add">
        添加属性
      </Button>
      {preview && (
        <Collapse
          ghost
          size="small"
          style={{ marginTop: 8 }}
          items={[{
            key: "json",
            label: "原始 JSON",
            children: (
              <pre style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                {JSON.stringify(preview, null, 2)}
              </pre>
            ),
          }]}
        />
      )}
    </div>
  );
}

export function SystemDict() {
  const { message, modal } = AntApp.useApp();
  const { can } = usePermission();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  // 启用状态下钻（?status=，总览仪表「数据字典」资产卡片）作为初始筛选
  const urlStatus = searchParams.get("status") ?? "";
  const [dictTypes, setDictTypes] = useState<string[]>([]);
  const [activeType, setActiveType] = useState<string>("");
  const [items, setItems] = useState<SystemDictItem[]>([]);
  const [status, setStatus] = useState<string>(urlStatus);
  const [loading, setLoading] = useState(false);

  // 统一返回上一入口：优先回退浏览器历史（总览资产卡片等入口），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  const [createOpen, setCreateOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [editItem, setEditItem] = useState<SystemDictItem | null>(null);
  // 通用扩展属性草稿：非 measure_format 类型在新增/编辑弹窗用 ExtraEditor 编辑的 extra
  const [createExtra, setCreateExtra] = useState<Record<string, unknown> | null>(null);
  const [editExtra, setEditExtra] = useState<Record<string, unknown> | null>(null);
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();
  // 批量操作（按当前 tab 的 dict_type 作用域）：行选 + 批量新增/启停/删除
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchRows, setBatchRows] = useState<BatchCreateRow[]>([
    { label: "", sort_order: 0, description: "" },
  ]);
  const [batchSubmitting, setBatchSubmitting] = useState(false);
  // LLM 推断描述 loading：单条新增/编辑弹窗标记（"create"|"edit"|null），批量新增按行 index
  const [inferringForm, setInferringForm] = useState<"create" | "edit" | null>(null);
  const [inferringBatchIdx, setInferringBatchIdx] = useState<number | null>(null);
  // 静默刷新的上次执行时间（TTL 防抖：QUIET_REFRESH_TTL_MS 内不重复请求）
  const lastQuietRefreshRef = useRef(0);
  // 编码自动生成预览：监听显示名，与后端 codegen 规则对齐——
  // slugify_code 生成 base（纯标点/空白名回退 item），再对当前类型已加载
  // （非软删）项编码做冲突自增（resolveUniqueCode，与 generate_unique_code
  // 逐字节一致），预览即后端将生成的最终编码。
  const watchLabel = Form.useWatch("label", createForm);
  const usedCodes = useMemo(() => items.map((i) => i.code), [items]);
  const codePreview = useMemo(() => {
    const base = slugifyCode(watchLabel ?? "") || "item";
    return resolveUniqueCode(base, usedCodes);
  }, [watchLabel, usedCodes]);
  // 超上限回退 base（resolveUniqueCode 返回的 base 必然仍被占用）→ 无法自动
  // 生成唯一编码，切换为手动指定（后端对应抛 DICT_CODE_EXHAUSTED）。
  const codeExhausted = usedCodes.includes(codePreview);

  // 状态筛选为客户端过滤（数据字典按类型 Tabs 一次性加载全部项）
  const visibleItems = useMemo(
    () => (status ? items.filter((i) => i.status === status) : items),
    [items, status],
  );

  useEffect(() => {
    listDictTypes().then((types) => {
      setDictTypes(types);
      if (types.length > 0 && !activeType) setActiveType(types[0]);
    }).catch(() => message.error("加载参照数据类型失败"));
  }, []);

  useEffect(() => {
    if (!activeType) return;
    setLoading(true);
    listAllDictItems(activeType)
      .then(setItems)
      .catch(() => message.error("加载参照数据项失败"))
      .finally(() => setLoading(false));
  }, [activeType]);

  function loadItems() {
    if (!activeType) return;
    setLoading(true);
    listAllDictItems(activeType)
      .then(setItems)
      .finally(() => setLoading(false));
  }

  // 静默刷新项列表：不置 loading，仅更新 items（打开新增弹窗时调用）。
  // TTL 防抖——QUIET_REFRESH_TTL_MS 内重复打开直接用缓存，超 TTL 才刷新。
  function refreshItemsQuietly() {
    if (!activeType) return;
    const now = Date.now();
    if (now - lastQuietRefreshRef.current < QUIET_REFRESH_TTL_MS) return;
    lastQuietRefreshRef.current = now;
    listAllDictItems(activeType).then(setItems).catch(() => {});
  }

  function openCreate() {
    setCreateOpen(true);
    // 新建默认无扩展属性（度量格式专用字段由表单自带）
    setCreateExtra(null);
    // 打开弹窗时基于最新项列表重算编码预览，缩小「他端新增同名编码但本页未
    // 刷新」导致的预览滞后窗口（提交仍以后端权威判定为准）。
    refreshItemsQuietly();
  }

  // AI 生成描述：单条新增/编辑弹窗共用（取当前表单的显示名 + 字典类型上下文，
  // LLM 只回填描述文本，用户可编辑后随表单提交）。
  async function handleInferDescription(form: "create" | "edit") {
    const f = form === "create" ? createForm : editForm;
    const label = (f.getFieldValue("label") ?? "").trim();
    if (!label) {
      message.warning("请先填写显示名，再生成描述");
      return;
    }
    setInferringForm(form);
    try {
      const desc = await inferDictDescription(activeType, label, DICT_TYPE_LABELS[activeType]);
      f.setFieldValue("description", desc);
      message.success("已生成描述，可编辑后保存");
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError && err.code === "LLM_INFER_UNAVAILABLE"
          ? "LLM 不可用：请检查 LLM 配置或稍后重试"
          : err instanceof UnisenseApiError
            ? `${err.message}（${err.codeZh}）`
            : "AI 生成描述失败，请稍后重试",
      );
    } finally {
      setInferringForm(null);
    }
  }

  // AI 生成描述：批量新增弹窗按行（用该行显示名，回填该行描述）。
  async function handleBatchInferDescription(idx: number) {
    const label = (batchRows[idx].label ?? "").trim();
    if (!label) {
      message.warning("请先填写该行显示名，再生成描述");
      return;
    }
    setInferringBatchIdx(idx);
    try {
      const desc = await inferDictDescription(activeType, label, DICT_TYPE_LABELS[activeType]);
      updateBatchRow(idx, { description: desc });
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError && err.code === "LLM_INFER_UNAVAILABLE"
          ? "LLM 不可用：请检查 LLM 配置或稍后重试"
          : err instanceof UnisenseApiError
            ? `${err.message}（${err.codeZh}）`
            : "AI 生成描述失败，请稍后重试",
      );
    } finally {
      setInferringBatchIdx(null);
    }
  }

  function composeExtra(values: { extra_unit?: string; extra_decimal?: number | null }): Record<string, unknown> | null {
    // 度量格式字典项的 extra 携带联动默认单位/小数位；仅当任一字段填写时组装
    const unit = values.extra_unit?.trim() ?? "";
    const hasUnit = unit !== "";
    const hasDecimal = values.extra_decimal != null;
    if (!hasUnit && !hasDecimal) return null;
    const extra: Record<string, unknown> = {};
    if (hasUnit) extra.unit = unit;
    if (hasDecimal) extra.decimal = values.extra_decimal;
    return extra;
  }

  async function handleCreate(values: { code?: string; label: string; sort_order?: number; description?: string; extra_unit?: string; extra_decimal?: number | null }) {
    try {
      // code 不传由后端按显示名自动生成英文编码（冲突自动追加序号）；
      // 仅「无法自动生成」时手动指定 code 才随表单透传。
      // extra：度量格式走专用单位/小数位控件组装；其他类型走通用 ExtraEditor 草稿
      const extra = activeType === "measure_format" ? composeExtra(values) : createExtra;
      await createDictItem(activeType, {
        ...values,
        sort_order: values.sort_order ?? 0,
        extra,
      });
      message.success("新增成功");
      setCreateOpen(false);
      createForm.resetFields();
      loadItems();
    } catch (err: any) {
      message.error(err?.message || "新增失败");
    }
  }

  async function handleEdit(values: { label?: string; sort_order?: number; description?: string; extra_unit?: string; extra_decimal?: number | null }) {
    if (!editItem) return;
    try {
      const extra = activeType === "measure_format" ? composeExtra(values) : editExtra;
      await updateDictItem(activeType, editItem.code, {
        ...values,
        extra,
      });
      message.success("更新成功");
      setEditOpen(false);
      editForm.resetFields();
      setEditItem(null);
      loadItems();
    } catch (err: any) {
      message.error(err?.message || "更新失败");
    }
  }

  async function handleToggle(item: SystemDictItem) {
    try {
      if (item.status === "active") await deactivateDictItem(activeType, item.code);
      else await activateDictItem(activeType, item.code);
      message.success(item.status === "active" ? "已停用" : "已启用");
      loadItems();
    } catch (err: any) {
      message.error(err?.message || "操作失败");
    }
  }

  function handleDelete(item: SystemDictItem) {
    modal.confirm({
      title: "确认删除",
      content: `确定要删除 "${item.label}" 吗？被引用的参照数据项不可删除。`,
      okText: "删除",
      okType: "danger",
      onOk: async () => {
        try {
          await deleteDictItem(activeType, item.code);
          message.success("删除成功");
          loadItems();
        } catch (err: any) {
          message.error(err?.message || "删除失败");
        }
      },
    });
  }

  // 批量失败清单：label/code（原因）——批量 207 语义下逐项标注，便于治理定位
  function batchFailSummary(result: DictBatchResult): string {
    return result.failed
      .map((f) => `${f.label || f.code || "?"}（${f.message ?? f.error_code ?? "失败"}）`)
      .join("、");
  }

  async function handleBatchToggle(enabled: boolean) {
    if (selectedRowKeys.length === 0) return;
    const action = enabled ? "启用" : "停用";
    setBatchSubmitting(true);
    try {
      const codes = selectedRowKeys.map(String);
      const result = await batchToggleDictItems(
        activeType,
        codes,
        enabled ? "activate" : "deactivate",
      );
      if (result.failed.length > 0) {
        message.warning(`${action}完成 ${result.succeeded.length} 个，失败 ${result.failed.length} 个：${batchFailSummary(result)}`);
      } else {
        message.success(`已${action} ${result.succeeded.length} 个参照数据项`);
      }
      setSelectedRowKeys([]);
      loadItems();
    } catch (err: any) {
      message.error(err?.message || `批量${action}失败`);
    } finally {
      setBatchSubmitting(false);
    }
  }

  async function handleBatchDelete() {
    if (selectedRowKeys.length === 0) return;
    setBatchSubmitting(true);
    try {
      const codes = selectedRowKeys.map(String);
      const result = await batchDeleteDictItems(activeType, codes);
      if (result.failed.length > 0) {
        message.warning(`删除完成 ${result.succeeded.length} 个，失败 ${result.failed.length} 个：${batchFailSummary(result)}`);
      } else {
        message.success(`已删除 ${result.succeeded.length} 个参照数据项`);
      }
      setSelectedRowKeys([]);
      loadItems();
    } catch (err: any) {
      message.error(err?.message || "批量删除失败");
    } finally {
      setBatchSubmitting(false);
    }
  }

  function addBatchRow() {
    setBatchRows([...batchRows, { label: "", sort_order: 0, description: "" }]);
  }

  function updateBatchRow(idx: number, patch: Partial<BatchCreateRow>) {
    setBatchRows(batchRows.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  }

  function removeBatchRow(idx: number) {
    setBatchRows(batchRows.filter((_, i) => i !== idx));
  }

  async function handleBatchCreate() {
    const valid = batchRows
      .map((r) => ({ ...r, label: r.label.trim() }))
      .filter((r) => r.label.length > 0);
    if (valid.length === 0) {
      message.warning("请至少填写一行的显示名");
      return;
    }
    setBatchSubmitting(true);
    try {
      const result = await batchCreateDictItems(activeType, valid);
      if (result.failed.length > 0) {
        message.warning(`新增成功 ${result.succeeded.length} 个，失败 ${result.failed.length} 个：${batchFailSummary(result)}`);
      } else {
        message.success(`已新增 ${result.succeeded.length} 个参照数据项`);
      }
      setBatchOpen(false);
      setBatchRows([{ label: "", sort_order: 0, description: "" }]);
      loadItems();
    } catch (err: any) {
      message.error(err?.message || "批量新增失败");
    } finally {
      setBatchSubmitting(false);
    }
  }

  const columns = [
    { title: "编码", dataIndex: "code", key: "code", width: 140 },
    { title: "显示名", dataIndex: "label", key: "label", width: 140 },
    { title: "排序", dataIndex: "sort_order", key: "sort_order", width: 60 },
    {
      title: "状态", dataIndex: "status", key: "status", width: 80,
      render: (s: string) => <Tag color={s === "active" ? "green" : "red"}>{s === "active" ? "启用" : "停用"}</Tag>,
    },
    { title: "引用数", dataIndex: "ref_count", key: "ref_count", width: 80 },
    { title: "描述", dataIndex: "description", key: "description", ellipsis: true },
    {
      title: "扩展属性",
      key: "extra",
      width: 200,
      render: (_: unknown, record: SystemDictItem) => {
        const extra = record.extra as Record<string, unknown> | null;
        if (!extra || Object.keys(extra).length === 0) return <span className="muted">—</span>;
        // 度量格式：保留单位/小数位语义展示（联动消费方按 unit/decimal 读取）
        if (activeType === "measure_format") {
          const unit = extra.unit != null && String(extra.unit) ? String(extra.unit) : null;
          const decimal = extra.decimal != null ? Number(extra.decimal) : null;
          return (
            <span>
              {unit ? `单位:${unit}` : ""}
              {unit && decimal != null ? "，" : ""}
              {decimal != null ? `${decimal}位` : unit ? "" : "—"}
            </span>
          );
        }
        // 其他类型：通用 key: value 标签展示（对象/数组值 JSON 序列化）
        return (
          <Space size={4} wrap>
            {Object.entries(extra).map(([k, v]) => (
              <Tag key={k}>
                {k}: {typeof v === "object" && v !== null ? JSON.stringify(v) : String(v)}
              </Tag>
            ))}
          </Space>
        );
      },
    },
    {
      title: "操作", key: "action", width: 200,
      render: (_: unknown, record: SystemDictItem) => (
        <Space size="small">
          {can("dict:create") && <Button size="small" icon={<EditOutlined />} onClick={() => { setEditItem(record); setEditExtra((record.extra ?? null) as Record<string, unknown> | null); const ex = (record.extra ?? {}) as { unit?: unknown; decimal?: unknown }; editForm.setFieldsValue({ label: record.label, sort_order: record.sort_order, description: record.description, extra_unit: ex.unit != null ? String(ex.unit) : undefined, extra_decimal: ex.decimal != null ? Number(ex.decimal) : undefined }); setEditOpen(true); }}>编辑</Button>}
          {can("dict:create") && (
            <Button size="small" icon={record.status === "active" ? <StopOutlined /> : <CheckCircleOutlined />} onClick={() => handleToggle(record)}>
              {record.status === "active" ? "停用" : "启用"}
            </Button>
          )}
          {can("dict:create") && <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(record)} />}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 8 }}>
        返回
      </Button>
      <Card title="参照数据管理">
      <Tabs
        activeKey={activeType}
        onChange={setActiveType}
        items={dictTypes.map((t) => ({
          key: t,
          label: DICT_TYPE_LABELS[t] || t,
        }))}
      />
      <div style={{ marginBottom: 16, display: "flex", gap: 12, alignItems: "center" }}>
        {can("dict:create") && (
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增参照数据项</Button>
        )}
        {can("dict:create") && (
          <Button icon={<PlusOutlined />} onClick={() => setBatchOpen(true)}>批量新增</Button>
        )}
        {can("dict:create") && selectedRowKeys.length > 0 && (
          <>
            <Button onClick={() => handleBatchToggle(true)} disabled={batchSubmitting}>
              批量启用
            </Button>
            <Popconfirm
              title="批量停用"
              description={`确定停用选中的 ${selectedRowKeys.length} 个参照数据项？停用后新指标无法再选用该取值。`}
              okText="确认停用"
              onConfirm={() => handleBatchToggle(false)}
              disabled={batchSubmitting}
            >
              <Button icon={<StopOutlined />} disabled={batchSubmitting}>
                批量停用
              </Button>
            </Popconfirm>
            <Popconfirm
              title="批量删除"
              description={`确定删除选中的 ${selectedRowKeys.length} 个参照数据项？被指标引用的项不可删除。`}
              okText="确认删除"
              okButtonProps={{ danger: true }}
              onConfirm={handleBatchDelete}
              disabled={batchSubmitting}
            >
              <Button danger icon={<DeleteOutlined />} disabled={batchSubmitting}>
                批量删除
              </Button>
            </Popconfirm>
          </>
        )}
        {selectedRowKeys.length > 0 && (
          <span style={{ color: "rgba(0,0,0,0.45)", fontSize: 13 }}>
            已选 {selectedRowKeys.length} 项
          </span>
        )}
        <Select showSearch
          allowClear
          placeholder="全部状态"
          style={{ width: 140 }}
          value={status || undefined}
          onChange={(v?: string) => setStatus(v ?? "")}
          options={[
            { value: "active", label: "启用" },
            { value: "inactive", label: "停用" },
          ]}
        />
      </div>
      <Table
        columns={columns}
        dataSource={visibleItems}
        rowKey="code"
        loading={loading}
        size="small"
        pagination={false}
        rowSelection={{
          selectedRowKeys,
          onChange: (keys) => setSelectedRowKeys(keys),
        }}
      />

      {/* 新增弹窗 */}
      <Modal title={`新增 ${DICT_TYPE_LABELS[activeType] || activeType} 参照数据项`} open={createOpen} onCancel={() => setCreateOpen(false)} onOk={() => createForm.submit()}>
        <Form form={createForm} onFinish={handleCreate} layout="vertical">
          <Form.Item name="label" label="显示名" rules={[{ required: true }]}>
            <Input placeholder="如 人民币元" />
          </Form.Item>
          {codeExhausted ? (
            <Form.Item
              name="code"
              preserve={false}
              label="编码（需手动指定）"
              tooltip="已存在大量同名编码（如 x、x_2 … x_100），无法自动生成唯一编码；请手动指定一个未占用的编码，或修改显示名后重试"
              rules={[{ required: true, pattern: /^[A-Za-z0-9_]+$/, message: "编码仅支持字母、数字、下划线" }]}
            >
              <Space.Compact style={{ width: "100%" }}>
                <Input placeholder="如 item_101" data-testid="dict-code-manual" />
                <Tag color="orange" style={{ lineHeight: "30px", margin: 0 }}>需手动指定</Tag>
              </Space.Compact>
            </Form.Item>
          ) : (
            <Form.Item label="编码（自动生成）" tooltip="系统根据显示名自动生成英文编码，与已有编码冲突时自动追加序号（如 minute_2）；若无法自动生成可手动指定编码后重试">
              <Space.Compact style={{ width: "100%" }}>
                <Input value={codePreview} disabled data-testid="dict-code-preview" />
                <Tag color="blue" style={{ lineHeight: "30px", margin: 0 }}>自动生成</Tag>
              </Space.Compact>
            </Form.Item>
          )}
          <Form.Item name="sort_order" label="排序" initialValue={0}>
            <InputNumber min={0} />
          </Form.Item>
          {activeType === "measure_format" ? (
            <Space size={16} style={{ display: "flex" }}>
              <Form.Item
                name="extra_unit"
                label="默认单位"
                style={{ width: 220 }}
                extra="度量格式联动默认单位（如 元 / 小数 / %）"
              >
                <Input placeholder="如 元" maxLength={32} />
              </Form.Item>
              <Form.Item name="extra_decimal" label="默认小数位" style={{ width: 160 }}>
                <Select showSearch
                  allowClear
                  placeholder="按需"
                  options={[
                    { value: 0, label: "0" },
                    { value: 1, label: "1" },
                    { value: 2, label: "2" },
                    { value: 4, label: "4" },
                  ]}
                />
              </Form.Item>
            </Space>
          ) : (
            <Form.Item
              label="扩展属性"
              extra="按需为字典项补充扩展属性（如 category: 度量词根）；值支持数字/布尔/JSON 自动识别"
            >
              <ExtraEditor value={createExtra} onChange={setCreateExtra} key={createOpen ? "create" : "closed"} />
            </Form.Item>
          )}
          {/* 描述框必须由 Form.Item 直接包裹（不可经 Space.Compact 中转，
              否则 value/onChange 注入被布局容器吞掉——AI 生成回填与手动输入均不生效） */}
          <div style={{ display: "flex", gap: 8, alignItems: "flex-end", marginBottom: 24 }}>
            <Form.Item
              name="description"
              label="描述"
              extra="可点击「AI 生成」根据显示名自动生成描述"
              style={{ flex: 1, marginBottom: 0 }}
            >
              <Input.TextArea rows={2} placeholder="该取值的含义与用途" data-testid="dict-create-desc" />
            </Form.Item>
            <Button
              icon={<RobotOutlined />}
              loading={inferringForm === "create"}
              onClick={() => handleInferDescription("create")}
              data-testid="dict-infer-create"
            >
              AI 生成
            </Button>
          </div>
        </Form>
      </Modal>

      {/* 编辑弹窗 */}
      <Modal title="编辑参照数据项" open={editOpen} onCancel={() => { setEditOpen(false); setEditItem(null); }} onOk={() => editForm.submit()}>
        <Form form={editForm} onFinish={handleEdit} layout="vertical">
          <Form.Item name="label" label="显示名" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="sort_order" label="排序">
            <InputNumber min={0} />
          </Form.Item>
          {activeType === "measure_format" ? (
            <Space size={16} style={{ display: "flex" }}>
              <Form.Item
                name="extra_unit"
                label="默认单位"
                style={{ width: 220 }}
                extra="度量格式联动默认单位（如 元 / 小数 / %）"
              >
                <Input placeholder="如 元" maxLength={32} />
              </Form.Item>
              <Form.Item name="extra_decimal" label="默认小数位" style={{ width: 160 }}>
                <Select showSearch
                  allowClear
                  placeholder="按需"
                  options={[
                    { value: 0, label: "0" },
                    { value: 1, label: "1" },
                    { value: 2, label: "2" },
                    { value: 4, label: "4" },
                  ]}
                />
              </Form.Item>
            </Space>
          ) : (
            <Form.Item
              label="扩展属性"
              extra="按需为字典项补充扩展属性（如 category: 度量词根）；值支持数字/布尔/JSON 自动识别"
            >
              <ExtraEditor value={editExtra} onChange={setEditExtra} key={editItem?.code ?? "edit"} />
            </Form.Item>
          )}
          <div style={{ display: "flex", gap: 8, alignItems: "flex-end", marginBottom: 24 }}>
            <Form.Item
              name="description"
              label="描述"
              extra="可点击「AI 生成」根据显示名自动生成描述"
              style={{ flex: 1, marginBottom: 0 }}
            >
              <Input.TextArea rows={2} placeholder="该取值的含义与用途" data-testid="dict-edit-desc" />
            </Form.Item>
            <Button
              icon={<RobotOutlined />}
              loading={inferringForm === "edit"}
              onClick={() => handleInferDescription("edit")}
              data-testid="dict-infer-edit"
            >
              AI 生成
            </Button>
          </div>
        </Form>
      </Modal>

      {/* 批量新增弹窗：多行显示名/排序/描述，编码由后端逐条自动生成 */}
      <Modal
        title={`批量新增 ${DICT_TYPE_LABELS[activeType] || activeType} 参照数据项`}
        open={batchOpen}
        onCancel={() => setBatchOpen(false)}
        onOk={handleBatchCreate}
        okText="批量新增"
        confirmLoading={batchSubmitting}
        width={680}
      >
        <div style={{ marginBottom: 12 }}>
          <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={addBatchRow}>
            添加一行
          </Button>
          <span style={{ marginLeft: 8, color: "rgba(0,0,0,0.45)", fontSize: 13 }}>
            每行显示名必填；编码自动生成，与既有项冲突的条目会标记为失败
          </span>
        </div>
        {batchRows.map((row, idx) => (
          <Space key={idx} style={{ display: "flex", marginBottom: 8 }} align="baseline">
            <Input
              placeholder="显示名（必填）"
              value={row.label}
              data-testid={`dict-batch-label-${idx}`}
              onChange={(e) => updateBatchRow(idx, { label: e.target.value })}
              style={{ width: 180 }}
            />
            <InputNumber
              placeholder="排序"
              min={0}
              value={row.sort_order}
              data-testid={`dict-batch-sort-${idx}`}
              onChange={(v) => updateBatchRow(idx, { sort_order: v ?? 0 })}
              style={{ width: 90 }}
            />
            <Space.Compact>
              <Input
                placeholder="描述"
                value={row.description}
                onChange={(e) => updateBatchRow(idx, { description: e.target.value })}
                style={{ width: 240 }}
                data-testid={`dict-batch-desc-${idx}`}
              />
              <Button
                size="small"
                icon={<RobotOutlined />}
                loading={inferringBatchIdx === idx}
                onClick={() => handleBatchInferDescription(idx)}
                data-testid={`dict-batch-infer-${idx}`}
              >
                AI
              </Button>
            </Space.Compact>
            <Button
              type="text"
              danger
              icon={<DeleteOutlined />}
              onClick={() => removeBatchRow(idx)}
              disabled={batchRows.length === 1}
            />
          </Space>
        ))}
      </Modal>
    </Card>
    </div>
  );
}
