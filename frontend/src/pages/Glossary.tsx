import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Tabs, Space, Descriptions } from "antd";
import { PlusOutlined, SendOutlined, ArrowLeftOutlined, HeartOutlined, ThunderboltOutlined, LoadingOutlined, ApartmentOutlined } from "@ant-design/icons";
import {
  listTerms,
  createTerm,
  getTerm,
  updateTerm,
  createTermRelation,
  listTermRelations,
  submitTerm,
  deprecateTerm,
  batchSubmitTerms,
  batchDeprecateTerms,
  inferTermSuggestion,
  listDomainTree,
  listTermConflicts,
  resolveTermConflict,
  listFavorites,
  addFavorite,
  removeFavorite,
  UnisenseApiError,
} from "../api";
import type { GlossaryTerm, GlossaryConflict, SubjectDomainTreeNode, TermRelationViewItem } from "../types";
import { formatCnTime } from "../utils/timeCn";

const STATUS_COLOR: Record<string, string> = { DRAFT: "default", PUBLISHED: "success", DEPRECATED: "error" };
const STATUS_LABEL: Record<string, string> = { DRAFT: "草稿", PUBLISHED: "已发布", DEPRECATED: "已废弃" };
// 关系类型 8 种（产品丰富增强，对齐后端 TermRelationType 枚举）
const RELATION_TYPE_LABEL: Record<string, string> = {
  SYNONYM_OF: "同义（SYNONYM_OF）",
  BROADER_THAN: "上位（BROADER_THAN）",
  NARROWER_THAN: "下位（NARROWER_THAN）",
  RELATED_TO: "相关（RELATED_TO）",
  ANTONYM_OF: "反义（ANTONYM_OF）",
  DEPENDS_ON: "依赖（DEPENDS_ON）",
  DERIVED_FROM: "派生（DERIVED_FROM）",
  INSTANCE_OF: "实例（INSTANCE_OF）",
};
const CONFLICT_TYPE_LABEL: Record<string, string> = {
  alias_overlap: "同义别名冲突",
  name_overlap: "同名冲突",
  definition_overlap: "语义漂移",
};
const CONFLICT_STATUS_LABEL: Record<string, string> = {
  OPEN: "待处理",
  RESOLVED: "已解决",
  IGNORED: "已忽略",
};

/** 主题域树展平为 Select 选项（含层级缩进；code 为提交值）。 */
function flattenDomains(nodes: SubjectDomainTreeNode[], depth = 0): { value: string; label: string }[] {
  const out: { value: string; label: string }[] = [];
  for (const n of nodes) {
    out.push({ value: n.code, label: `${"　".repeat(depth)}${n.name}（${n.code}）` });
    if (n.children?.length) out.push(...flattenDomains(n.children, depth + 1));
  }
  return out;
}

/** 根据名称用 LLM 推断定义/同义词/边界，回填指定 Form。 */
async function inferFromName(
  form: ReturnType<typeof Form.useForm>[0],
  setInferring: (v: boolean) => void,
) {
  const name = String(form.getFieldValue("name") ?? "").trim();
  if (!name) {
    message.warning("请先填写术语名称，再进行 AI 推断");
    return;
  }
  setInferring(true);
  try {
    const res = await inferTermSuggestion(name);
    form.setFieldsValue({
      definition: res.definition,
      synonyms: (res.synonyms ?? []).join(", "),
      boundary: res.boundary ?? "",
    });
    message.success(
      `已根据「${name}」生成建议${res.confidence != null ? `（置信度 ${Math.round(res.confidence * 100)}%）` : ""}`,
    );
  } catch (err) {
    message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "AI 推断失败");
  } finally {
    setInferring(false);
  }
}

function TermsTab() {
  const [items, setItems] = useState<GlossaryTerm[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [searchParams] = useSearchParams();
  // 生命周期状态下钻（?status=，总览仪表「术语」资产卡片）作为初始筛选
  const urlStatus = searchParams.get("status") ?? "";
  // 责任人（Owner）下钻（?owner_id=，总览仪表 Owner 责任分布）
  const urlOwnerId = searchParams.get("owner_id");
  const [status, setStatus] = useState(urlStatus);
  const [ownerId, setOwnerId] = useState<number | undefined>(
    urlOwnerId && /^\d+$/.test(urlOwnerId) ? Number(urlOwnerId) : undefined,
  );
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  // 术语收藏（C 层多资产收藏：TERM）
  const [favCodes, setFavCodes] = useState<Set<string>>(new Set());
  const [form] = Form.useForm();
  // 业务域选项（主题域树，不手造）+ AI 推断中标记
  const [domainOptions, setDomainOptions] = useState<{ value: string; label: string }[]>([]);
  const [inferring, setInferring] = useState(false);
  // 批量状态流转（多选行 + 确认弹窗）
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [batchAction, setBatchAction] = useState<"submit" | "deprecate" | null>(null);
  // 关系目标术语选项（Select 搜索）
  const [relationOptions, setRelationOptions] = useState<{ value: number; label: string }[]>([]);
  const [relationLoading, setRelationLoading] = useState(false);
  // 术语关系图谱查看：中心术语 + 上游/下游关系列表
  const [relationViewTerm, setRelationViewTerm] = useState<GlossaryTerm | null>(null);
  const [relationViewItems, setRelationViewItems] = useState<TermRelationViewItem[]>([]);
  const [relationViewLoading, setRelationViewLoading] = useState(false);
  // 详情/编辑/关系管理：详情为只读弹窗，编辑与关系用独立 Form 避免与新建表单互相污染
  const [detailTerm, setDetailTerm] = useState<GlossaryTerm | null>(null);
  const [editTarget, setEditTarget] = useState<GlossaryTerm | null>(null);
  const [editForm] = Form.useForm();
  const [relationTarget, setRelationTarget] = useState<GlossaryTerm | null>(null);
  const [relationForm] = Form.useForm();
  // URL 直达关键词（?kw=，全局搜索跳术语）作为初始筛选，避免「先查全量再过滤」的竞态覆盖
  const urlKw = searchParams.get("kw") ?? "";
  const focusCode = searchParams.get("focus");
  // 并发查询防竞态：只有最后一次发起的请求允许落地结果
  const loadSeq = useRef(0);
  // 搜索框初始值承接 URL 关键词（首查即带过滤）
  const [search, setSearch] = useState(urlKw);

  async function load(overSearch?: string) {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      const res = await listTerms({
        search: overSearch ?? search,
        status,
        owner_id: ownerId,
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

  // 响应 URL 直达关键词变化（全局搜索 SPA 内跳转，同路由不 remount）；初始值已由 useState 承接，
  // 此处仅同步「URL 出现新关键词」的场景，并保留用户手动清空/修改搜索的能力。
  useEffect(() => {
    if (urlKw && urlKw !== search) {
      setSearch(urlKw);
      setPage(1);
      // search 不在 load 依赖中（手动搜索模式），此处直接用新值查询
      load(urlKw);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlKw]);

  // 响应 URL 状态参数变化（总览仪表「术语」资产卡片二次下钻）；status 在 load 依赖中，
  // setStatus 会经依赖链自动触发重查，无需手动 load
  useEffect(() => {
    if (urlStatus && urlStatus !== status) {
      setStatus(urlStatus);
      setPage(1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlStatus]);

  // 响应 URL 责任人参数变化（Owner 责任分布二次下钻）；ownerId 在 load 依赖中自动重查
  useEffect(() => {
    if (urlOwnerId && /^\d+$/.test(urlOwnerId) && Number(urlOwnerId) !== ownerId) {
      setOwnerId(Number(urlOwnerId));
      setPage(1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlOwnerId]);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize, status, ownerId]);

  // 加载当前用户术语收藏（TERM 类型）供行内收藏按钮判断
  useEffect(() => {
    listFavorites()
      .then((favs) =>
        setFavCodes(
          new Set(favs.filter((f) => f.asset_type === "TERM").map((f) => f.asset_id)),
        ),
      )
      .catch(() => {});
  }, []);

  // 加载主题域树作为业务域选项（新建/编辑不手造）
  useEffect(() => {
    listDomainTree("active")
      .then((tree) => setDomainOptions(flattenDomains(tree)))
      .catch(() => {});
  }, []);

  // 加载术语候选供关系目标搜索（Select showSearch）
  async function loadRelationOptions(searchKw?: string) {
    setRelationLoading(true);
    try {
      const res = await listTerms({ search: searchKw || undefined, page: 1, page_size: 100 });
      setRelationOptions(
        res.items.map((t) => ({
          value: t.id,
          label: `${t.term_code} - ${t.name}`,
        })),
      );
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载术语列表失败");
    } finally {
      setRelationLoading(false);
    }
  }

  // 批量发布/废弃（207 语义：成功/失败逐条反馈）
  async function handleBatch(action: "submit" | "deprecate") {
    if (!selectedRowKeys.length) return;
    const codes = selectedRowKeys.map(String);
    try {
      const results =
        action === "submit"
          ? await batchSubmitTerms(codes)
          : await batchDeprecateTerms(codes);
      const okCount = results.filter((r) => r.ok).length;
      const failCount = results.length - okCount;
      message[okCount > 0 ? "success" : "error"](
        `批量${action === "submit" ? "发布" : "废弃"}完成：成功 ${okCount} 条` +
          (failCount ? `，失败 ${failCount} 条（已跳过不影响成功项）` : ""),
      );
      setSelectedRowKeys([]);
      setBatchAction(null);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量操作失败");
    }
  }

  // 术语收藏切换（行内心形）
  async function toggleFavorite(t: GlossaryTerm) {
    const fav = favCodes.has(t.term_code);
    try {
      if (fav) {
        await removeFavorite("TERM", t.term_code);
        setFavCodes((prev) => {
          const next = new Set(prev);
          next.delete(t.term_code);
          return next;
        });
        message.success("已取消收藏");
      } else {
        await addFavorite("TERM", t.term_code);
        setFavCodes((prev) => new Set(prev).add(t.term_code));
        message.success("已收藏");
      }
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "收藏操作失败",
      );
    }
  }

  async function handleCreate(values: Record<string, unknown>) {
    try {
      await createTerm({
        term_code: values.term_code ? String(values.term_code) : undefined,
        name: String(values.name),
        definition: String(values.definition),
        domain: String(values.domain),
        synonyms: values.synonyms ? String(values.synonyms).split(",").map((s) => s.trim()).filter(Boolean) : [],
        boundary: values.boundary ? String(values.boundary) : null,
      });
      message.success("术语已创建（已自动触发冲突检测）");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    }
  }

  async function handleSubmit(t: GlossaryTerm) {
    try {
      await submitTerm(t.term_code);
      message.success("已提交发布");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "提交失败");
    }
  }

  async function handleDeprecate(t: GlossaryTerm) {
    try {
      await deprecateTerm(t.term_code);
      message.success("已废弃");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "废弃失败");
    }
  }

  // 详情：先用列表行数据即时展示，再拉取最新完整详情补全（owner/版本/时间戳等列外字段）
  async function openDetail(t: GlossaryTerm) {
    setDetailTerm(t);
    try {
      const full = await getTerm(t.term_code);
      setDetailTerm(full);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载详情失败");
    }
  }

  // 编辑：回填当前值（同义词逗号连接还原为表单输入格式；编码可编辑）
  function openEdit(t: GlossaryTerm) {
    setEditTarget(t);
    editForm.setFieldsValue({
      term_code: t.term_code,
      name: t.name,
      definition: t.definition,
      domain: t.domain,
      synonyms: (t.synonyms ?? []).join(", "),
      boundary: t.boundary ?? "",
    });
  }

  async function handleUpdate(values: Record<string, unknown>) {
    if (!editTarget) return;
    try {
      await updateTerm(editTarget.term_code, {
        term_code: values.term_code ? String(values.term_code) : undefined,
        name: String(values.name),
        definition: String(values.definition),
        domain: String(values.domain),
        synonyms: values.synonyms ? String(values.synonyms).split(",").map((s) => s.trim()).filter(Boolean) : [],
        boundary: values.boundary ? String(values.boundary) : null,
      });
      message.success("术语已更新");
      setEditTarget(null);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "更新失败");
    }
  }

  // 关系图谱查看：加载该术语的全部关系（上游 incoming / 下游 outgoing）
  async function openRelationView(t: GlossaryTerm) {
    setRelationViewTerm(t);
    setRelationViewItems([]);
    setRelationViewLoading(true);
    try {
      const items = await listTermRelations(t.term_code);
      setRelationViewItems(items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载关系失败");
    } finally {
      setRelationViewLoading(false);
    }
  }

  // 关系管理：为当前术语建立与另一术语的关系（目标按关键词搜索选择，不手输 ID）
  function openRelation(t: GlossaryTerm) {
    setRelationTarget(t);
    relationForm.resetFields();
    relationForm.setFieldsValue({ relation_type: "RELATED_TO" });
    loadRelationOptions();
  }

  async function handleCreateRelation(values: Record<string, unknown>) {
    if (!relationTarget) return;
    try {
      await createTermRelation(relationTarget.term_code, {
        target_term_id: Number(values.target_term_id),
        relation_type: String(values.relation_type),
      });
      message.success("术语关系已建立");
      setRelationTarget(null);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "建立关系失败");
    }
  }

  const columns = [
    { title: "编码", dataIndex: "term_code", key: "term_code", render: (v: string) => <span className="mono">{v}</span> },
    { title: "名称", dataIndex: "name", key: "name" },
    { title: "定义", dataIndex: "definition", key: "definition", ellipsis: true },
    { title: "域", dataIndex: "domain", key: "domain", width: 120 },
    { title: "同义词", dataIndex: "synonyms", key: "synonyms", width: 160, render: (v: unknown[]) => (v?.length ? v.join("、") : <span className="muted">—</span>) },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (s: string) => <Tag color={STATUS_COLOR[s]}>{STATUS_LABEL[s] ?? s}</Tag>,
    },
    {
      title: "操作",
      key: "actions",
      width: 420,
      render: (_: unknown, t: GlossaryTerm) => (
        <Space wrap>
          <Button size="small" type="link" onClick={() => openDetail(t)}>详情</Button>
          <Button size="small" type="link" onClick={() => openEdit(t)}>编辑</Button>
          <Button size="small" type="link" icon={<ApartmentOutlined />} onClick={() => openRelationView(t)}>关系</Button>
          <Button size="small" type="link" onClick={() => openRelation(t)}>建立关系</Button>
          <Button
            size="small"
            type="link"
            icon={<HeartOutlined style={{ color: favCodes.has(t.term_code) ? "#eb2f96" : undefined }} />}
            onClick={() => toggleFavorite(t)}
          >
            {favCodes.has(t.term_code) ? "已收藏" : "收藏"}
          </Button>
          {t.status === "DRAFT" && (
            <Button size="small" type="primary" icon={<SendOutlined />} onClick={() => handleSubmit(t)}>提交</Button>
          )}
          {t.status === "DEPRECATED" && (
            <Button size="small" type="primary" icon={<SendOutlined />} onClick={() => handleSubmit(t)}>再次发布</Button>
          )}
          {t.status !== "DEPRECATED" && (
            <Button size="small" danger onClick={() => handleDeprecate(t)}>废弃</Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Input.Search
          placeholder="搜索术语名/定义/编码"
          allowClear
          style={{ width: 260 }}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onSearch={() => { setPage(1); load(); }}
        />
        <Select
          allowClear
          placeholder="全部状态"
          style={{ width: 140 }}
          value={status || undefined}
          onChange={(v) => { setStatus(v || ""); setPage(1); }}
          options={[{ value: "DRAFT", label: "草稿" }, { value: "PUBLISHED", label: "已发布" }, { value: "DEPRECATED", label: "已废弃" }]}
        />
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新建术语</Button>
        <Button icon={<SendOutlined />} disabled={!selectedRowKeys.length} onClick={() => setBatchAction("submit")}>
          批量发布
        </Button>
        <Button danger icon={<PlusOutlined rotate={45} />} disabled={!selectedRowKeys.length} onClick={() => setBatchAction("deprecate")}>
          批量废弃
        </Button>
        <span className="muted">共 {total} 条</span>
      </Space>

      <Table
        dataSource={items}
        columns={columns}
        rowKey="term_code"
        loading={loading}
        rowSelection={{ selectedRowKeys, onChange: setSelectedRowKeys }}
        pagination={{ current: page, pageSize, total, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100], onChange: (p, ps) => { setPage(p); setPageSize(ps); }, showTotal: (t) => `共 ${t} 条` }}
        locale={{ emptyText: "暂无术语" }}
        rowClassName={(r) => (focusCode && r.term_code === focusCode ? "ant-table-row-selected" : "")}
      />

      <Modal title="新建术语" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="创建">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="term_code" label="术语编码" extra={<span className="mono" style={{ color: "#0E7C86" }}>留空则由系统自动生成</span>}>
            <Input className="mono" placeholder="留空自动生成" />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如 成交总额" />
          </Form.Item>
          <Form.Item label="AI 推断" style={{ marginBottom: 12 }}>
            <Button
              icon={inferring ? <LoadingOutlined /> : <ThunderboltOutlined />}
              loading={inferring}
              onClick={() => inferFromName(form, setInferring)}
            >
              根据名称生成定义 / 同义词 / 边界建议
            </Button>
          </Form.Item>
          <Form.Item name="domain" label="业务域" rules={[{ required: true }]}>
            <Select
              showSearch
              placeholder="请选择业务域"
              options={domainOptions}
              optionFilterProp="label"
              notFoundContent={domainOptions.length ? undefined : "暂无启用中的主题域"}
            />
          </Form.Item>
          <Form.Item name="definition" label="定义" rules={[{ required: true }]}>
            <Input.TextArea rows={3} />
          </Form.Item>
          <Form.Item name="synonyms" label="同义词（逗号分隔）">
            <Input placeholder="GMV, gross merchandise volume" />
          </Form.Item>
          <Form.Item name="boundary" label="边界说明">
            <Input.TextArea rows={2} placeholder="如：不含退款订单" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 术语详情：只读展示完整字段（含列外字段：owner/版本/时间戳） */}
      <Modal
        title={detailTerm ? `术语详情：${detailTerm.term_code}` : "术语详情"}
        open={detailTerm !== null}
        onCancel={() => setDetailTerm(null)}
        footer={<Button onClick={() => setDetailTerm(null)}>关闭</Button>}
      >
        {detailTerm && (
          <Descriptions column={1} size="small" bordered style={{ marginTop: 8 }}>
            <Descriptions.Item label="术语编码">{detailTerm.term_code}</Descriptions.Item>
            <Descriptions.Item label="名称">{detailTerm.name}</Descriptions.Item>
            <Descriptions.Item label="业务域">{detailTerm.domain}</Descriptions.Item>
            <Descriptions.Item label="状态"><Tag color={STATUS_COLOR[detailTerm.status]}>{STATUS_LABEL[detailTerm.status] ?? detailTerm.status}</Tag></Descriptions.Item>
            <Descriptions.Item label="定义">{detailTerm.definition}</Descriptions.Item>
            <Descriptions.Item label="同义词">{(detailTerm.synonyms ?? []).length ? (detailTerm.synonyms as string[]).join("、") : <span className="muted">—</span>}</Descriptions.Item>
            <Descriptions.Item label="边界说明">{detailTerm.boundary ?? <span className="muted">—</span>}</Descriptions.Item>
            <Descriptions.Item label="Owner ID"><span className="mono">{detailTerm.owner_id}</span></Descriptions.Item>
            <Descriptions.Item label="版本"><span className="mono">{detailTerm.version ?? 1}</span></Descriptions.Item>
            <Descriptions.Item label="创建时间">{detailTerm.created_at ? formatCnTime(detailTerm.created_at) : "—"}</Descriptions.Item>
            <Descriptions.Item label="更新时间">{detailTerm.updated_at ? formatCnTime(detailTerm.updated_at) : "—"}</Descriptions.Item>
          </Descriptions>
        )}
      </Modal>

      {/* 编辑术语：回填当前值，缺省字段不更新 */}
      <Modal
        title={editTarget ? `编辑术语：${editTarget.term_code}` : "编辑术语"}
        open={editTarget !== null}
        onCancel={() => setEditTarget(null)}
        onOk={() => editForm.submit()}
        okText="保存"
      >
        <Form form={editForm} layout="vertical" onFinish={handleUpdate} style={{ marginTop: 8 }}>
          <Form.Item name="term_code" label="术语编码" rules={[{ required: true }]}>
            <Input className="mono" />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item label="AI 推断" style={{ marginBottom: 12 }}>
            <Button
              icon={inferring ? <LoadingOutlined /> : <ThunderboltOutlined />}
              loading={inferring}
              onClick={() => inferFromName(editForm, setInferring)}
            >
              根据名称重新生成定义 / 同义词 / 边界建议
            </Button>
          </Form.Item>
          <Form.Item name="domain" label="业务域" rules={[{ required: true }]}>
            <Select
              showSearch
              placeholder="请选择业务域"
              options={domainOptions}
              optionFilterProp="label"
              notFoundContent={domainOptions.length ? undefined : "暂无启用中的主题域"}
            />
          </Form.Item>
          <Form.Item name="definition" label="定义" rules={[{ required: true }]}>
            <Input.TextArea rows={3} />
          </Form.Item>
          <Form.Item name="synonyms" label="同义词（逗号分隔）">
            <Input placeholder="GMV, gross merchandise volume" />
          </Form.Item>
          <Form.Item name="boundary" label="边界说明">
            <Input.TextArea rows={2} placeholder="如：不含退款订单" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 术语关系图谱：中心术语 + 上游（对端→本术语）+ 下游（本术语→对端），展示相互关系 */}
      <Modal
        title={relationViewTerm ? `术语关系图谱：${relationViewTerm.term_code}` : "术语关系图谱"}
        open={relationViewTerm !== null}
        onCancel={() => setRelationViewTerm(null)}
        width={720}
        footer={[
          <Button
            key="add"
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              if (relationViewTerm) {
                const t = relationViewTerm;
                setRelationViewTerm(null);
                openRelation(t);
              }
            }}
          >
            建立关系
          </Button>,
          <Button key="close" onClick={() => setRelationViewTerm(null)}>关闭</Button>,
        ]}
      >
        {relationViewTerm && (
          <div style={{ marginTop: 8 }}>
            {/* 中心术语 */}
            <div style={{ textAlign: "center", marginBottom: 16 }}>
              <Tag color="geekblue" style={{ fontSize: 14, padding: "4px 14px" }}>
                {relationViewTerm.name} <span className="mono">（{relationViewTerm.term_code}）</span>
              </Tag>
              <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                域：{relationViewTerm.domain ?? "—"} · {STATUS_LABEL[relationViewTerm.status] ?? relationViewTerm.status}
              </div>
            </div>

            {relationViewLoading ? (
              <div style={{ textAlign: "center", padding: 24 }}>
                <LoadingOutlined /> 加载关系中…
              </div>
            ) : relationViewItems.length === 0 ? (
              <div className="muted" style={{ textAlign: "center", padding: 24 }}>
                暂无关联术语，点击右下角「建立关系」添加。
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                {/* 上游：对端 → 本术语 */}
                <div>
                  <div className="muted" style={{ marginBottom: 8, fontSize: 12 }}>▲ 上游（引用本术语）</div>
                  {relationViewItems
                    .filter((i) => i.direction === "incoming")
                    .map((i) => (
                      <div key={i.peer.id} style={{ border: "1px solid #eef1f4", borderRadius: 6, padding: 8, marginBottom: 8 }}>
                        <Tag color="blue">{RELATION_TYPE_LABEL[i.relation_type] ?? i.relation_type}</Tag>
                        <div style={{ marginTop: 4 }}>{i.peer.name}</div>
                        <div className="mono muted" style={{ fontSize: 12 }}>{i.peer.term_code}</div>
                      </div>
                    ))}
                  {!relationViewItems.some((i) => i.direction === "incoming") && (
                    <div className="muted" style={{ fontSize: 12 }}>无上游</div>
                  )}
                </div>
                {/* 下游：本术语 → 对端 */}
                <div>
                  <div className="muted" style={{ marginBottom: 8, fontSize: 12 }}>▼ 下游（本术语引用）</div>
                  {relationViewItems
                    .filter((i) => i.direction === "outgoing")
                    .map((i) => (
                      <div key={i.peer.id} style={{ border: "1px solid #eef1f4", borderRadius: 6, padding: 8, marginBottom: 8 }}>
                        <Tag color="green">{RELATION_TYPE_LABEL[i.relation_type] ?? i.relation_type}</Tag>
                        <div style={{ marginTop: 4 }}>{i.peer.name}</div>
                        <div className="mono muted" style={{ fontSize: 12 }}>{i.peer.term_code}</div>
                      </div>
                    ))}
                  {!relationViewItems.some((i) => i.direction === "outgoing") && (
                    <div className="muted" style={{ fontSize: 12 }}>无下游</div>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </Modal>

      {/* 关系管理：为目标术语建立术语间关系（目标按数据库 id 定位） */}
      <Modal
        title={relationTarget ? `建立关系：${relationTarget.term_code}` : "建立术语关系"}
        open={relationTarget !== null}
        onCancel={() => setRelationTarget(null)}
        onOk={() => relationForm.submit()}
        okText="建立"
      >
        <Form form={relationForm} layout="vertical" onFinish={handleCreateRelation} style={{ marginTop: 8 }}>
          <Form.Item
            name="target_term_id"
            label="关联目标术语"
            extra={<span className="muted">按编码 / 名称搜索选择，无需手输 ID</span>}
            rules={[{ required: true, message: "请选择关联目标术语" }]}
          >
            <Select
              showSearch
              loading={relationLoading}
              placeholder="搜索术语编码或名称…"
              options={relationOptions}
              filterOption={false}
              onSearch={(kw) => loadRelationOptions(kw)}
              onFocus={() => loadRelationOptions()}
              optionFilterProp="label"
            />
          </Form.Item>
          <Form.Item name="relation_type" label="关系类型" rules={[{ required: true }]}>
            <Select options={Object.entries(RELATION_TYPE_LABEL).map(([v, label]) => ({ value: v, label }))} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 批量状态流转确认弹窗（可控 Modal，明确展示操作数量） */}
      <Modal
        title={batchAction === "submit" ? "批量发布术语" : "批量废弃术语"}
        open={batchAction !== null}
        onCancel={() => setBatchAction(null)}
        onOk={() => handleBatch(batchAction as "submit" | "deprecate")}
        okText={batchAction === "submit" ? "发布" : "废弃"}
        okButtonProps={{ danger: batchAction === "deprecate" }}
        confirmLoading={false}
      >
        <p style={{ marginBottom: 0 }}>
          确定{batchAction === "submit" ? "发布" : "废弃"}选中的 <b>{selectedRowKeys.length}</b> 个术语吗？
          {batchAction === "deprecate" ? "废弃后可通过「再次发布」重新发布。" : "草稿 / 已废弃术语可发布；已发布将幂等跳过。"}
        </p>
      </Modal>
    </div>
  );
}

function ConflictsTab() {
  const [items, setItems] = useState<GlossaryConflict[]>([]);
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const res = await listTermConflicts();
      setItems(res.items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleResolve(c: GlossaryConflict, decision: string) {
    try {
      await resolveTermConflict(c.id, decision);
      message.success(decision === "RESOLVED" ? "已解决冲突" : "已忽略冲突");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    }
  }

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    { title: "术语", dataIndex: "term_id", key: "term", width: 90, render: (v: number) => <span className="mono">#{v}</span> },
    { title: "冲突类型", dataIndex: "conflict_type", key: "type", width: 180, render: (v: string) => CONFLICT_TYPE_LABEL[v] ?? v },
    { title: "关联术语", dataIndex: "ref_term_id", key: "refTerm", render: (v: number | null) => v ? <span className="mono">#{v}</span> : <span className="muted">—</span> },
    { title: "关联指标", dataIndex: "ref_metric_id", key: "refMetric", render: (v: number | null) => v ? <span className="mono">#{v}</span> : <span className="muted">—</span> },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (s: string) => <Tag color={s === "OPEN" ? "warning" : "success"}>{CONFLICT_STATUS_LABEL[s] ?? s}</Tag>,
    },
    {
      title: "操作",
      key: "actions",
      width: 160,
      render: (_: unknown, c: GlossaryConflict) =>
        c.status === "OPEN" ? (
          <Space>
            <Button size="small" type="primary" onClick={() => handleResolve(c, "RESOLVED")}>解决</Button>
            <Button size="small" onClick={() => handleResolve(c, "IGNORED")}>忽略</Button>
          </Space>
        ) : (
          <Tag>已处理</Tag>
        ),
    },
  ];

  return (
    <Table dataSource={items} columns={columns} rowKey="id" loading={loading} pagination={false} locale={{ emptyText: "暂无术语冲突" }} />
  );
}

export function Glossary() {
  const navigate = useNavigate();

  // 统一返回上一入口：优先回退浏览器历史（总览资产卡片/全局搜索等入口），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  const tabItems = [
    { key: "terms", label: "术语列表", children: <TermsTab /> },
    { key: "conflicts", label: "术语冲突", children: <ConflictsTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
          <div className="page-kicker">Governance / Glossary</div>
          <h2>术语表</h2>
          <p>业务术语统一定义——创建即触发冲突检测，保证全组织口径一致。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 8 } }}>
        <Tabs items={tabItems} />
      </Card>
    </div>
  );
}
