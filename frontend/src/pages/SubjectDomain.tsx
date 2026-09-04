import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Button, Card, Col, Row, Tree, Descriptions, Modal, Form, Input, InputNumber, Popconfirm,
  Space, Tag, App as AntApp, Empty, Spin, TreeSelect, Tooltip, Alert, Select, Table,
} from "antd";
import { PlusOutlined, EditOutlined, DeleteOutlined, StopOutlined, CheckCircleOutlined, SettingOutlined, BranchesOutlined, ArrowLeftOutlined } from "@ant-design/icons";
import type { DataNode } from "antd/es/tree";
import {
  listDomainTree, getDomain, createDomain, updateDomain, deactivateDomain,
  activateDomain, deleteDomain, getDomainDefaults, updateDomainDefaults,
  listDictItems, listDomainMetrics,
} from "../api";
import type { SubjectDomainTreeNode, SubjectDomain } from "../types";
import { enumLabel, METRIC_TYPE_LABEL, GRANULARITY_LABEL, AGGREGATION_LABEL, TIME_SEMANTICS_LABEL, FRESHNESS_LABEL, DW_LAYER_LABEL, SERVING_MODE_LABEL, ADDITIVITY_LABEL, METRIC_TIER_LABEL } from "../utils/enums";
import { slugifyCode } from "../utils/zhEnDict";
import { usePermission } from "../hooks/usePermission";

/**
 * 前端编码预览（与后端 codegen.slugify_code 规则对齐）：
 * 连续中文段经中英术语字典翻译（贪心最长匹配）、未覆盖词拼音兜底、
 * ASCII 保留、段落用下划线连接；纯标点/空白名无可提取字符 →
 * 根域回退 domain / 子域回退 {父域}_sub。仅用于表单预览，
 * 实际编码以后端生成/返回为准。
 */
export function previewDomainCode(name: string, parentCode?: string | null): string {
  const slug = slugifyCode(name);
  if (slug) return parentCode ? `${parentCode}_${slug}` : slug;
  return parentCode ? `${parentCode}_sub` : "domain";
}

/** 递归收集 id → code 映射。 */
function collectCodeMap(nodes: SubjectDomainTreeNode[], map: Record<number, string> = {}): Record<number, string> {
  for (const n of nodes) {
    map[n.id] = n.code;
    if (n.children.length > 0) collectCodeMap(n.children, map);
  }
  return map;
}

function toSelectTreeData(nodes: SubjectDomainTreeNode[]): DataNode[] {
  return nodes.map((n) => ({
    key: n.id,
    title: n.name,
    value: n.id,
    children: n.children.length > 0 ? toSelectTreeData(n.children) : undefined,
  }));
}

/**
 * 递归查找同父域下同名节点（trim + 小写，忽略首尾空格）。
 * 作用域与后端一致：仅同父域（parent_id）内比较，不同父域允许同名。
 * excludeCode 用于编辑时排除自身。
 */
function findDuplicateNode(
  nodes: SubjectDomainTreeNode[],
  name: string,
  parentId: number | null,
  excludeCode: string | null,
): SubjectDomainTreeNode | null {
  const n = name.trim().toLowerCase();
  if (!n) return null;
  for (const node of nodes) {
    if (
      node.code !== excludeCode &&
      node.parent_id === parentId &&
      node.name.trim().toLowerCase() === n
    ) {
      return node;
    }
    if (node.children.length > 0) {
      const found = findDuplicateNode(node.children, name, parentId, excludeCode);
      if (found) return found;
    }
  }
  return null;
}

/** 按 id 递归查找节点名称（用于同名警告中展示父域上下文）。 */
function findNodeName(nodes: SubjectDomainTreeNode[], id: number): string | null {
  for (const n of nodes) {
    if (n.id === id) return n.name;
    if (n.children.length > 0) {
      const found = findNodeName(n.children, id);
      if (found) return found;
    }
  }
  return null;
}

const DICT_FIELDS = [
  { key: "granularity", label: "粒度" },
  { key: "unit", label: "单位" },
  { key: "aggregation", label: "聚合" },
  { key: "time_semantics", label: "时间语义" },
  { key: "freshness", label: "新鲜度" },
  { key: "dw_layer", label: "数仓层" },
  { key: "type", label: "类型" },
  { key: "serving_mode", label: "服务模式" },
  { key: "additivity", label: "可加性" },
  { key: "metric_tier", label: "分级" },
];

const DICT_FIELD_MAPS: Record<string, Record<string, string>> = {
  granularity: GRANULARITY_LABEL,
  aggregation: AGGREGATION_LABEL,
  time_semantics: TIME_SEMANTICS_LABEL,
  freshness: FRESHNESS_LABEL,
  dw_layer: DW_LAYER_LABEL,
  type: METRIC_TYPE_LABEL,
  serving_mode: SERVING_MODE_LABEL,
  additivity: ADDITIVITY_LABEL,
  metric_tier: METRIC_TIER_LABEL,
};

function _nodeDepth(nodes: SubjectDomainTreeNode[], targetId: number, depth: number = 1): number {
  for (const n of nodes) {
    if (n.id === targetId) return depth;
    if (n.children.length > 0) {
      const childDepth = _nodeDepth(n.children, targetId, depth + 1);
      if (childDepth > 0) return childDepth;
    }
  }
  return 0;
}

function treeDataToNodes(nodes: SubjectDomainTreeNode[], onAddChild: (n: SubjectDomainTreeNode) => void, allNodes: SubjectDomainTreeNode[], canAddChild: boolean): DataNode[] {
  return nodes.map((n) => ({
    key: n.code,
    title: (
      // hover-show 包裹：鼠标悬停时显示「+」按钮；点击节点本身仍走 Tree onSelect 查看详情（不冲突）
      <span className="tree-node-title">
        {n.name} <Tag color={n.status === "active" ? "green" : "red"} style={{ marginLeft: 4, fontSize: 10 }}>{n.status === "active" ? "启用" : "停用"}</Tag>
        {n.metric_count > 0 && <Tag color="blue" style={{ marginLeft: 4, fontSize: 10 }}>{n.metric_count}指标</Tag>}
        {(n.dimension_count ?? 0) > 0 && <Tag color="geekblue" style={{ marginLeft: 4, fontSize: 10 }}>{n.dimension_count}维度</Tag>}
        {canAddChild && _nodeDepth(allNodes, n.id) < 3 && (
          <Tooltip title={`在「${n.name}」下新建子域`}>
            <Button
              type="text"
              size="small"
              icon={<BranchesOutlined />}
              className="tree-add-child-btn"
              style={{ marginLeft: 4, fontSize: 12, color: "#E8862D" }}
              onClick={(e) => { e.stopPropagation(); onAddChild(n); }}
              aria-label={`新建子域-${n.name}`}
            />
          </Tooltip>
        )}
      </span>
    ),
    children: n.children.length > 0 ? treeDataToNodes(n.children, onAddChild, allNodes, canAddChild) : undefined,
  }));
}

export function SubjectDomain() {
  const { message, modal } = AntApp.useApp();
  const { can, snapshot } = usePermission();
  // 域编码是全局标识符（被全部业务资产与用户权限域引用），改编码仅平台管理员可执行
  const isPlatformAdmin =
    snapshot?.roles?.includes("platform_admin") ?? snapshot?.role === "platform_admin";
  const navigate = useNavigate();
  const [treeData, setTreeData] = useState<SubjectDomainTreeNode[]>([]);
  // R9: 主题域树搜索（名称过滤，命中的祖先路径自动展开）
  const [searchValue, setSearchValue] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [selectedCode, setSelectedCode] = useState<string | null>(null);
  // 并发查询防竞态：快速切换树节点时只有最后一次的详情请求允许落地（避免旧节点覆盖新节点）
  const detailSeq = useRef(0);
  const [detail, setDetail] = useState<SubjectDomain | null>(null);
  const [defaults, setDefaults] = useState<Record<string, unknown>>({});
  // 域默认值弹窗：各字段从字典下拉选择（惰性选择，避免手输非法枚举值）
  const [dictOptions, setDictOptions] = useState<Record<string, Array<{ value: string; label: string }>>>({});

  // 加载域默认值相关字典（粒度/单位/聚合/时间语义/新鲜度/数仓层/类型/服务模式/可加性/分级）
  useEffect(() => {
    const dictTypeOf: Record<string, string> = {
      granularity: "granularity",
      unit: "unit",
      aggregation: "aggregation",
      time_semantics: "time_semantics",
      freshness: "freshness",
      dw_layer: "dw_layer",
      type: "metric_type",
      serving_mode: "serving_mode",
      additivity: "additivity",
      metric_tier: "metric_tier",
    };
    Promise.all(
      Object.values(dictTypeOf).map((dt) =>
        listDictItems(dt)
          .then((items) => ({
            dt,
            options: items
              .filter((it) => it.status === "active")
              .map((it) => ({ value: it.code, label: `${it.label}（${it.code}）` })),
          }))
          .catch(() => ({ dt, options: [] as Array<{ value: string; label: string }> })),
      ),
    ).then((results) => {
      const map: Record<string, Array<{ value: string; label: string }>> = {};
      for (const r of results) map[r.dt] = r.options;
      setDictOptions(map);
    });
  }, []);

  // 统一返回上一入口：优先回退浏览器历史（全局搜索等入口），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  // 弹窗
  const [createOpen, setCreateOpen] = useState(false);
  const [createTitle, setCreateTitle] = useState("新建根域");
  const [editOpen, setEditOpen] = useState(false);
  const [defaultsOpen, setDefaultsOpen] = useState(false);
  const [domainMetricsOpen, setDomainMetricsOpen] = useState(false);
  const [domainMetricsLoading, setDomainMetricsLoading] = useState(false);
  const [domainMetrics, setDomainMetrics] = useState<Array<Record<string, unknown>>>([]);
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();
  const [defaultsForm] = Form.useForm();

  // 编码自动生成预览：监听显示名 + 上级域
  const watchName = Form.useWatch("name", createForm);
  const watchParentId = Form.useWatch("parent_id", createForm);
  const watchCode = Form.useWatch("code", createForm);
  // 搜索过滤：保留名称命中的节点及其祖先链（保证可导航到命中节点），返回原始节点树
  function filterDomainNodes(nodes: SubjectDomainTreeNode[], q: string): SubjectDomainTreeNode[] {
    const lower = q.toLowerCase();
    const out: SubjectDomainTreeNode[] = [];
    for (const n of nodes) {
      const self = n.name.toLowerCase().includes(lower);
      const children = filterDomainNodes(n.children, q);
      if (self || children.length) out.push({ ...n, children });
    }
    return out;
  }

  const codeMap = collectCodeMap(treeData);
  const codePreview = previewDomainCode(watchName ?? "", watchParentId ? codeMap[watchParentId] : null);

  // 创建弹窗同名冲突检测（同父域下；与后端 name_exists 口径一致）
  const createDup = watchName?.trim()
    ? findDuplicateNode(treeData, watchName, watchParentId ?? null, null)
    : null;
  const createDupWarning = createDup
    ? `在${createDup.parent_id ? `「${findNodeName(treeData, createDup.parent_id) ?? "上级域"}」下` : "根域"}已存在同名主题域「${createDup.name}」（${createDup.code}）`
    : null;

  // 编辑弹窗同名冲突检测（排除自身 selectedCode）
  const watchEditName = Form.useWatch("name", editForm);
  const watchEditCode = Form.useWatch("code", editForm);
  const editDup = watchEditName?.trim()
    ? findDuplicateNode(treeData, watchEditName, detail?.parent_id ?? null, selectedCode)
    : null;
  const editDupWarning = editDup
    ? `在${editDup.parent_id ? `「${findNodeName(treeData, editDup.parent_id) ?? "上级域"}」下` : "根域"}已存在同名主题域「${editDup.name}」（${editDup.code}）`
    : null;

  async function loadTree() {
    setLoading(true);
    try {
      const data = await listDomainTree();
      setTreeData(data);
    } catch { message.error("加载域树失败"); }
    finally { setLoading(false); }
  }

  async function loadDetail(code: string) {
    const seq = ++detailSeq.current;
    try {
      const d = await getDomain(code);
      if (seq !== detailSeq.current) return;
      if (seq !== detailSeq.current) return;
      setDetail(d);
      const defs = await getDomainDefaults(code);
      if (seq !== detailSeq.current) return;
      setDefaults(defs);
    } catch { message.error("加载域详情失败"); }
  }

  useEffect(() => { loadTree(); }, []);

  useEffect(() => {
    if (selectedCode) loadDetail(selectedCode);
    else { setDetail(null); setDefaults({}); }
  }, [selectedCode]);

  function handleSelect(keys: React.Key[]) {
    if (keys.length > 0) setSelectedCode(keys[0] as string);
  }

  // 创建域
  async function handleCreate(values: { code?: string; name: string; parent_id?: number | null; sort_order?: number; description?: string }) {
    // 同名冲突：提交前拦截，避免依赖后端 409 往返
    if (createDup) {
      message.warning(createDupWarning!);
      return;
    }
    if (saving) return;
    setSaving(true);
    try {
      // code 手动指定或留空（后端按显示名自动生成）；owner_id 由后端以创建人认证身份覆盖（P2-3 修复硬编码）
      await createDomain({
        ...values,
        code: values.code?.trim() || undefined,
        parent_id: values.parent_id ?? null,
        sort_order: values.sort_order ?? 0,
      });
      message.success("创建成功");
      setCreateOpen(false);
      createForm.resetFields();
      loadTree();
    } catch (err: any) {
      message.error(err?.message || "创建失败");
    } finally {
      setSaving(false);
    }
  }

  // 打开新建弹窗：rootMode=true 建根域；否则在指定节点下建子域
  function openCreate(rootMode: boolean, parent?: SubjectDomainTreeNode) {
    createForm.resetFields();
    if (rootMode) {
      createForm.setFieldsValue({ parent_id: null });
      setCreateTitle("新建根域");
    } else if (parent) {
      createForm.setFieldsValue({ parent_id: parent.id });
      setCreateTitle(`在「${parent.name}」下新建子域`);
    }
    setCreateOpen(true);
  }

  // 编辑域
  async function handleEdit(values: { code?: string; name?: string; sort_order?: number; description?: string }) {
    if (!selectedCode) return;
    // 同名冲突：提交前拦截
    if (editDup) {
      message.warning(editDupWarning!);
      return;
    }
    if (saving) return;
    setSaving(true);
    try {
      const newCode = values.code?.trim();
      const renamed = !!newCode && newCode !== selectedCode;
      // code 仅在变更时携带（后端级联更新全部引用并留审计）
      await updateDomain(selectedCode, {
        ...values,
        code: renamed ? newCode : undefined,
      });
      message.success(renamed ? `编码已更新为 ${newCode}（全部引用已同步）` : "更新成功");
      setEditOpen(false);
      editForm.resetFields();
      if (renamed && newCode) setSelectedCode(newCode);
      else loadDetail(selectedCode);
      loadTree();
    } catch (err: any) {
      message.error(err?.message || "更新失败");
    } finally {
      setSaving(false);
    }
  }

  // 停用/启用
  async function handleToggle() {
    if (!selectedCode) return;
    try {
      if (detail?.status === "active") {
        await deactivateDomain(selectedCode);
        message.success("已停用");
      } else {
        await activateDomain(selectedCode);
        message.success("已启用");
      }
      loadDetail(selectedCode);
      loadTree();
    } catch (err: any) {
      message.error(err?.message || "操作失败");
    }
  }

  // 删除
  function handleDelete() {
    if (!selectedCode) return;
    modal.confirm({
      title: "确认删除",
      content: `确定要删除域 "${detail?.name}" 吗？需要该域下无关联指标且无子域。`,
      okText: "删除",
      okType: "danger",
      onOk: async () => {
        try {
          await deleteDomain(selectedCode);
          message.success("删除成功");
          setSelectedCode(null);
          loadTree();
        } catch (err: any) {
          message.error(err?.message || "删除失败");
        }
      },
    });
  }

  // 查看该域下指标列表
  async function openDomainMetrics(code: string) {
    setDomainMetricsOpen(true);
    setDomainMetricsLoading(true);
    try {
      const data = await listDomainMetrics(code);
      setDomainMetrics(data);
    } catch (err: any) {
      message.error(err?.message || "获取域下指标失败");
      setDomainMetricsOpen(false);
    } finally {
      setDomainMetricsLoading(false);
    }
  }

  // 保存默认值
  async function handleSaveDefaults(values: Record<string, string>) {
    if (!selectedCode) return;
    if (saving) return;
    // 只提交用户填写的字段（过滤 undefined/null）——后端 update_defaults 是全量替换，
    // 若把未填字段（antd Form 值为 undefined，序列化被省略）提交会把未配置字段清空。
    // 「清空全部」由独立按钮提交空对象承载，部分填写保存应为"只更新填写项、保留其余"。
    const payload = Object.fromEntries(
      Object.entries(values).filter(([, v]) => v !== undefined && v !== null),
    );
    setSaving(true);
    try {
      await updateDomainDefaults(selectedCode, payload);
      message.success("默认值已保存");
      setDefaultsOpen(false);
      loadDetail(selectedCode);
    } catch (err: any) {
      message.error(err?.message || "保存失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
          <div className="page-kicker">指标资产 / 主题域管理</div>
          <h2>主题域管理</h2>
          <p>业务域树形组织——新建子域、配置域默认值，指标按域归类。</p>
        </div>
      </div>
      <Row gutter={16}>
        <Col span={10}>
          <Card title="主题域树" extra={can("domain:create") && <Button type="primary" icon={<PlusOutlined />} onClick={() => openCreate(true)}>新建根域</Button>} style={{ minHeight: 500 }}>
            {loading ? <Spin /> : treeData.length === 0 ? <Empty description="暂无主题域" /> : (
              <>
                {searchValue.trim() && (
                  <Input
                    allowClear
                    placeholder="搜索主题域名称…"
                    style={{ marginBottom: 8 }}
                    value={searchValue}
                    onChange={(e) => setSearchValue(e.target.value)}
                  />
                )}
                <Tree
                  showLine
                  defaultExpandAll
                  treeData={treeDataToNodes(
                    searchValue.trim() ? filterDomainNodes(treeData, searchValue) : treeData,
                    (n) => openCreate(false, n),
                    treeData,
                    can("domain:create"),
                  )}
                  onSelect={handleSelect}
                  selectedKeys={selectedCode ? [selectedCode] : []}
                />
              </>
            )}
          </Card>
        </Col>
        <Col span={14}>
          {detail ? (
            <Card title={`域详情: ${detail.name}`} extra={
              <Space>
                {can("domain:create") && (
                  <Button icon={<SettingOutlined />} onClick={() => { defaultsForm.setFieldsValue(defaults); setDefaultsOpen(true); }}>默认值</Button>
                )}
                {can("domain:create") && (
                  <Button icon={<EditOutlined />} onClick={() => { editForm.setFieldsValue({ code: detail.code, name: detail.name, sort_order: detail.sort_order, description: detail.description }); setEditOpen(true); }}>编辑</Button>
                )}
                {can("domain:create") && (
                  <Button icon={detail.status === "active" ? <StopOutlined /> : <CheckCircleOutlined />} onClick={handleToggle}>
                    {detail.status === "active" ? "停用" : "启用"}
                  </Button>
                )}
                {can("domain:create") && (
                  <Button danger icon={<DeleteOutlined />} onClick={handleDelete}>删除</Button>
                )}
                <Button icon={<BranchesOutlined />} onClick={() => detail && openDomainMetrics(detail.code)}>域下指标</Button>
              </Space>
            }>
              <Descriptions column={2} bordered size="small">
                <Descriptions.Item label="编码">{detail.code}</Descriptions.Item>
                <Descriptions.Item label="名称">{detail.name}</Descriptions.Item>
                <Descriptions.Item label="层级">{detail.level}</Descriptions.Item>
                <Descriptions.Item label="状态"><Tag color={detail.status === "active" ? "green" : "red"}>{detail.status === "active" ? "启用" : "停用"}</Tag></Descriptions.Item>
                <Descriptions.Item label="排序">{detail.sort_order}</Descriptions.Item>
                <Descriptions.Item label="关联指标数">{detail.metric_count}</Descriptions.Item>
                <Descriptions.Item label="描述" span={2}>{detail.description || "-"}</Descriptions.Item>
              </Descriptions>
              {Object.keys(defaults).length > 0 && (
                <div style={{ marginTop: 16 }}>
                  <h4>域默认值预设</h4>
                  <Descriptions column={2} bordered size="small">
                    {DICT_FIELDS.filter(f => defaults[f.key]).map(f => {
                      const dictType = f.key === "type" ? "metric_type" : f.key;
                      // 优先从字典选项反查中文（与默认值弹窗同源，避免单位等未硬编码枚举展示裸 code）；
                      // 字典未加载时回退 enumLabel 硬编码映射
                      const opt = (dictOptions[dictType] ?? []).find((o) => o.value === String(defaults[f.key]));
                      const label = opt?.label
                        ?? enumLabel(DICT_FIELD_MAPS[f.key] ?? {}, String(defaults[f.key]))
                        ?? String(defaults[f.key]);
                      return (
                        <Descriptions.Item key={f.key} label={f.label}>{label}</Descriptions.Item>
                      );
                    })}
                  </Descriptions>
                </div>
              )}
            </Card>
          ) : (
            <Card><Empty description="选择左侧域节点查看详情" /></Card>
          )}
        </Col>
      </Row>

      {/* 创建弹窗 */}
      <Modal title={createTitle} open={createOpen} onCancel={() => setCreateOpen(false)} onOk={() => createForm.submit()} confirmLoading={saving}>
        <Form form={createForm} onFinish={handleCreate} layout="vertical" scrollToFirstError>
          <Form.Item name="parent_id" label="上级域" initialValue={null}>
            <TreeSelect
              allowClear
              treeDefaultExpandAll
              treeData={toSelectTreeData(treeData)}
              placeholder="不选则为根域"
              notFoundContent="暂无主题域"
            />
          </Form.Item>
          <Form.Item name="name" label="显示名" rules={[{ required: true, message: "请输入显示名" }]}>
            <Input placeholder="如 销售" status={createDup ? "error" : undefined} />
          </Form.Item>
          {createDupWarning && <Alert type="error" showIcon message={createDupWarning} style={{ marginBottom: 16 }} data-testid="create-dup-warning" />}
          <Form.Item
            name="code"
            label="域编码"
            tooltip="可手动指定；留空则由系统按显示名自动生成（子域自动带父域前缀，冲突时自动追加序号）"
            rules={[
              {
                pattern: /^[a-z][a-z0-9_]*$/,
                message: "编码须以小写字母开头，仅含小写字母、数字和下划线",
              },
            ]}
            extra={
              watchCode?.trim()
                ? "编码已手动指定，将以输入值为准"
                : `留空自动生成${codePreview ? `（预览：${codePreview}）` : ""}`
            }
          >
            <Input
              placeholder="留空则按显示名自动生成"
              maxLength={64}
              data-testid="domain-code-input"
            />
          </Form.Item>
          <Form.Item name="sort_order" label="排序" initialValue={0}>
            <InputNumber min={0} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 编辑弹窗 */}
      <Modal title="编辑主题域" open={editOpen} onCancel={() => setEditOpen(false)} onOk={() => editForm.submit()} confirmLoading={saving}>
        <Form form={editForm} onFinish={handleEdit} layout="vertical" scrollToFirstError>
          <Form.Item
            name="code"
            label="域编码"
            tooltip={isPlatformAdmin ? "域编码是全局标识符，修改后将级联更新该域下指标/维度/挂载/模板/逻辑度量/术语/数据源/授权/接入方与用户权限域等全部引用" : "域编码仅平台管理员可修改"}
            rules={[
              { required: true, message: "请输入域编码" },
              {
                pattern: /^[a-z][a-z0-9_]*$/,
                message: "编码须以小写字母开头，仅含小写字母、数字和下划线",
              },
            ]}
          >
            <Input maxLength={64} disabled={!isPlatformAdmin} data-testid="edit-domain-code" />
          </Form.Item>
          {isPlatformAdmin && watchEditCode?.trim() && watchEditCode.trim() !== selectedCode && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message="修改域编码将级联更新全部引用"
              description={`该域下指标、维度、术语、挂载、模板、数据源、冲突/授权记录及用户权限域中的「${selectedCode}」将同步更新为「${watchEditCode.trim()}」。此操作会写入审计日志。`}
              data-testid="code-rename-warning"
            />
          )}
          <Form.Item name="name" label="显示名" rules={[{ required: true }]}>
            <Input status={editDup ? "error" : undefined} />
          </Form.Item>
          {editDupWarning && <Alert type="error" showIcon message={editDupWarning} style={{ marginBottom: 16 }} data-testid="edit-dup-warning" />}
          <Form.Item name="sort_order" label="排序">
            <InputNumber min={0} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 默认值弹窗 */}
      <Modal title="配置域默认值" open={defaultsOpen} onCancel={() => setDefaultsOpen(false)} onOk={() => defaultsForm.submit()} width={600} confirmLoading={saving}>
        <Form form={defaultsForm} onFinish={handleSaveDefaults} layout="vertical" scrollToFirstError>
          {DICT_FIELDS.map(f => {
            const dictType = f.key === "type" ? "metric_type" : f.key;
            const opts = dictOptions[dictType];
            return (
              <Form.Item key={f.key} name={f.key} label={f.label}>
                {opts && opts.length > 0 ? (
                  <Select
                    allowClear
                    showSearch
                    optionFilterProp="label"
                    placeholder={`选择默认${f.label}`}
                    options={opts}
                  />
                ) : (
                  <Input placeholder={`默认${f.label}值`} />
                )}
              </Form.Item>
            );
          })}
        </Form>
        <Space style={{ marginTop: 8 }}>
          <Popconfirm
            title="清空全部默认值"
            description="将清除该域的所有默认值配置（恢复为未配置状态），注册指标时将不再预填。"
            okText="清空"
            okButtonProps={{ danger: true }}
            onConfirm={() => {
              defaultsForm.resetFields();
              handleSaveDefaults({});
            }}
          >
            <Button danger size="small">清空全部默认值</Button>
          </Popconfirm>
          <span className="muted" style={{ fontSize: 12 }}>单个字段可点清除图标；清空后注册指标不再预填该域默认值</span>
        </Space>
      </Modal>
      <Modal
        title={`域下指标（${detail?.name ?? ""}）`}
        open={domainMetricsOpen}
        onCancel={() => setDomainMetricsOpen(false)}
        footer={null}
        width={720}
      >
        <Table
          rowKey={(r) => String((r as { metric_code?: string }).metric_code ?? JSON.stringify(r))}
          loading={domainMetricsLoading}
          dataSource={domainMetrics}
          size="small"
          pagination={{ pageSize: 10, showSizeChanger: false }}
          locale={{ emptyText: "该域下暂无指标" }}
          columns={[
            { title: "指标编码", dataIndex: "metric_code", width: 220 },
            { title: "指标名称", dataIndex: "name", width: 220 },
            {
              title: "类型",
              dataIndex: "metric_type",
              width: 100,
              render: (v: unknown) => enumLabel(METRIC_TYPE_LABEL, String(v ?? "")) || "—",
            },
            { title: "状态", dataIndex: "status", width: 100 },
          ]}
        />
      </Modal>
    </div>
  );
}
