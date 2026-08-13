import { useEffect, useState } from "react";
import {
  Button, Card, Col, Row, Tree, Descriptions, Modal, Form, Input, InputNumber,
  Space, Tag, App as AntApp, Empty, Spin, TreeSelect, Tooltip,
} from "antd";
import { PlusOutlined, EditOutlined, DeleteOutlined, StopOutlined, CheckCircleOutlined, SettingOutlined, BranchesOutlined } from "@ant-design/icons";
import type { DataNode } from "antd/es/tree";
import {
  listDomainTree, getDomain, createDomain, updateDomain, deactivateDomain,
  activateDomain, deleteDomain, getDomainDefaults, updateDomainDefaults,
} from "../api";
import type { SubjectDomainTreeNode, SubjectDomain } from "../types";
import { enumLabel, METRIC_TYPE_LABEL, GRANULARITY_LABEL, AGGREGATION_LABEL, TIME_SEMANTICS_LABEL, FRESHNESS_LABEL, DW_LAYER_LABEL, SERVING_MODE_LABEL, ADDITIVITY_LABEL, METRIC_TIER_LABEL } from "../utils/enums";

/**
 * 前端编码预览（与后端 SubjectDomainService._generate_unique_code 规则一致）：
 * 显示名 ASCII slug 化；子域拼接父域前缀；纯中文回退 domain / {父域}_sub。
 * 仅用于表单预览，实际编码以后端生成/返回为准。
 */
function slugifyCode(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

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

function treeDataToNodes(nodes: SubjectDomainTreeNode[], onAddChild: (n: SubjectDomainTreeNode) => void): DataNode[] {
  return nodes.map((n) => ({
    key: n.code,
    title: (
      <span>
        {n.name} <Tag color={n.status === "active" ? "green" : "red"} style={{ marginLeft: 4, fontSize: 10 }}>{n.status === "active" ? "启用" : "停用"}</Tag>
        {n.metric_count > 0 && <Tag color="blue" style={{ marginLeft: 4, fontSize: 10 }}>{n.metric_count}指标</Tag>}
        <Tooltip title={`在「${n.name}」下新建子域`}>
          <Button
            type="text"
            size="small"
            icon={<BranchesOutlined />}
            style={{ marginLeft: 4, fontSize: 12, color: "#E8862D" }}
            onClick={(e) => { e.stopPropagation(); onAddChild(n); }}
            aria-label={`新建子域-${n.name}`}
          />
        </Tooltip>
      </span>
    ),
    children: n.children.length > 0 ? treeDataToNodes(n.children, onAddChild) : undefined,
  }));
}

export function SubjectDomain() {
  const { message, modal } = AntApp.useApp();
  const [treeData, setTreeData] = useState<SubjectDomainTreeNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedCode, setSelectedCode] = useState<string | null>(null);
  const [detail, setDetail] = useState<SubjectDomain | null>(null);
  const [defaults, setDefaults] = useState<Record<string, unknown>>({});

  // 弹窗
  const [createOpen, setCreateOpen] = useState(false);
  const [createTitle, setCreateTitle] = useState("新建根域");
  const [editOpen, setEditOpen] = useState(false);
  const [defaultsOpen, setDefaultsOpen] = useState(false);
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();
  const [defaultsForm] = Form.useForm();

  // 编码自动生成预览：监听显示名 + 上级域
  const watchName = Form.useWatch("name", createForm);
  const watchParentId = Form.useWatch("parent_id", createForm);
  const codeMap = collectCodeMap(treeData);
  const codePreview = previewDomainCode(watchName ?? "", watchParentId ? codeMap[watchParentId] : null);

  async function loadTree() {
    setLoading(true);
    try {
      const data = await listDomainTree();
      setTreeData(data);
    } catch { message.error("加载域树失败"); }
    finally { setLoading(false); }
  }

  async function loadDetail(code: string) {
    try {
      const d = await getDomain(code);
      setDetail(d);
      const defs = await getDomainDefaults(code);
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
  async function handleCreate(values: { name: string; parent_id?: number | null; sort_order?: number; description?: string }) {
    try {
      // code 不传：由后端按显示名自动生成
      await createDomain({ ...values, parent_id: values.parent_id ?? null, owner_id: 1, sort_order: values.sort_order ?? 0 });
      message.success("创建成功");
      setCreateOpen(false);
      createForm.resetFields();
      loadTree();
    } catch (err: any) {
      message.error(err?.message || "创建失败");
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
  async function handleEdit(values: { name?: string; sort_order?: number; description?: string }) {
    if (!selectedCode) return;
    try {
      await updateDomain(selectedCode, values);
      message.success("更新成功");
      setEditOpen(false);
      editForm.resetFields();
      loadDetail(selectedCode);
      loadTree();
    } catch (err: any) {
      message.error(err?.message || "更新失败");
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

  // 保存默认值
  async function handleSaveDefaults(values: Record<string, string>) {
    if (!selectedCode) return;
    try {
      await updateDomainDefaults(selectedCode, values);
      message.success("默认值已保存");
      setDefaultsOpen(false);
      loadDetail(selectedCode);
    } catch (err: any) {
      message.error(err?.message || "保存失败");
    }
  }

  return (
    <div>
      <Row gutter={16}>
        <Col span={10}>
          <Card title="主题域树" extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => openCreate(true)}>新建根域</Button>} style={{ minHeight: 500 }}>
            {loading ? <Spin /> : treeData.length === 0 ? <Empty description="暂无主题域" /> : (
              <Tree
                showLine
                defaultExpandAll
                treeData={treeDataToNodes(treeData, (n) => openCreate(false, n))}
                onSelect={handleSelect}
                selectedKeys={selectedCode ? [selectedCode] : []}
              />
            )}
          </Card>
        </Col>
        <Col span={14}>
          {detail ? (
            <Card title={`域详情: ${detail.name}`} extra={
              <Space>
                <Button icon={<SettingOutlined />} onClick={() => { defaultsForm.setFieldsValue(defaults); setDefaultsOpen(true); }}>默认值</Button>
                <Button icon={<EditOutlined />} onClick={() => { editForm.setFieldsValue({ name: detail.name, sort_order: detail.sort_order, description: detail.description }); setEditOpen(true); }}>编辑</Button>
                <Button icon={detail.status === "active" ? <StopOutlined /> : <CheckCircleOutlined />} onClick={handleToggle}>
                  {detail.status === "active" ? "停用" : "启用"}
                </Button>
                <Button danger icon={<DeleteOutlined />} onClick={handleDelete}>删除</Button>
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
                    {DICT_FIELDS.filter(f => defaults[f.key]).map(f => (
                      <Descriptions.Item key={f.key} label={f.label}>{enumLabel(DICT_FIELD_MAPS[f.key] ?? {}, String(defaults[f.key]))}</Descriptions.Item>
                    ))}
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
      <Modal title={createTitle} open={createOpen} onCancel={() => setCreateOpen(false)} onOk={() => createForm.submit()}>
        <Form form={createForm} onFinish={handleCreate} layout="vertical">
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
            <Input placeholder="如 销售" />
          </Form.Item>
          <Form.Item label="域编码（自动生成）" tooltip="系统根据显示名自动生成，子域自动带父域前缀，冲突时自动追加序号；提交后以后端返回为准">
            <Space.Compact style={{ width: "100%" }}>
              <Input value={codePreview} disabled data-testid="domain-code-preview" />
              <Tag color="blue" style={{ lineHeight: "30px", margin: 0 }}>自动生成</Tag>
            </Space.Compact>
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
      <Modal title="编辑主题域" open={editOpen} onCancel={() => setEditOpen(false)} onOk={() => editForm.submit()}>
        <Form form={editForm} onFinish={handleEdit} layout="vertical">
          <Form.Item name="name" label="显示名" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="sort_order" label="排序">
            <InputNumber min={0} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 默认值弹窗 */}
      <Modal title="配置域默认值" open={defaultsOpen} onCancel={() => setDefaultsOpen(false)} onOk={() => defaultsForm.submit()} width={600}>
        <Form form={defaultsForm} onFinish={handleSaveDefaults} layout="vertical">
          {DICT_FIELDS.map(f => (
            <Form.Item key={f.key} name={f.key} label={f.label}>
              <Input placeholder={`默认${f.label}值`} />
            </Form.Item>
          ))}
        </Form>
      </Modal>
    </div>
  );
}
