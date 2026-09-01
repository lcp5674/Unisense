import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Tabs, Space, Descriptions, Popconfirm } from "antd";
import { PlusOutlined, SendOutlined, ArrowLeftOutlined, HeartOutlined, ThunderboltOutlined, LoadingOutlined, ApartmentOutlined, DeleteOutlined, RedoOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  listTerms,
  createTerm,
  getTerm,
  updateTerm,
  createTermRelation,
  listTermRelations,
  submitTerm,
  publishTerm,
  approveTerm,
  rejectTerm,
  deprecateTerm,
  reactivateTerm,
  deleteTerm,
  restoreTerm,
  batchSubmitTerms,
  batchPublishTerms,
  batchApproveTerms,
  batchRejectTerms,
  batchDeprecateTerms,
  batchReactivateTerms,
  batchDeleteTerms,
  inferTermSuggestion,
  listDomainTree,
  listTermConflicts,
  resolveTermConflict,
  listFavorites,
  addFavorite,
  removeFavorite,
  fetchCurrentUser,
  UnisenseApiError,
} from "../api";
import type { ReviewSubmitBody } from "../api";
import type { GlossaryTerm, GlossaryConflict, SubjectDomainTreeNode, TermRelationViewItem, CurrentUser, BatchResult } from "../types";
import { formatCnTime } from "../utils/timeCn";
import { usePermission } from "../hooks/usePermission";
import { usePersistentPageSize } from "../hooks/usePersistentPageSize";
import { MasterDataBatch, type BatchActionKey } from "../components/MasterDataBatch";
import {
  MasterDataReviewActions,
  MasterDataReviewModals,
  useMasterDataReview,
} from "../components/MasterDataReview";

const STATUS_COLOR: Record<string, string> = { DRAFT: "default", REVIEW: "warning", PUBLISHED: "success", DEPRECATED: "error" };
const STATUS_LABEL: Record<string, string> = { DRAFT: "草稿", REVIEW: "审核中", PUBLISHED: "已发布", DEPRECATED: "已废弃" };
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
// 关系类型图谱元数据：语义符号 + 专属色（hex，antd Tag 自适应文字）+ 短标签
const RELATION_TYPE_META: Record<string, { symbol: string; color: string; label: string }> = {
  SYNONYM_OF: { symbol: "≡", color: "#2f54eb", label: "同义" },
  BROADER_THAN: { symbol: "⊇", color: "#722ed1", label: "上位" },
  NARROWER_THAN: { symbol: "⊆", color: "#13c2c2", label: "下位" },
  RELATED_TO: { symbol: "↔", color: "#1677ff", label: "相关" },
  ANTONYM_OF: { symbol: "≠", color: "#f5222d", label: "反义" },
  DEPENDS_ON: { symbol: "⛓", color: "#fa8c16", label: "依赖" },
  DERIVED_FROM: { symbol: "→", color: "#52c41a", label: "派生" },
  INSTANCE_OF: { symbol: "◈", color: "#eb2f96", label: "实例" },
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
  // F-1（第十一轮）：每页条数持久化（对齐 MetricCatalog/Dimensions 模式），切换/刷新后保持
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.glossary.pageSize", 20);
  const setPageSize = (ps: number) => onShowSizeChange(0, ps);
  const [searchParams] = useSearchParams();
  // 生命周期状态下钻（?status=，总览仪表「术语」资产卡片）作为初始筛选
  const urlStatus = searchParams.get("status") ?? "";
  // 责任人（Owner）下钻（?owner_id=，总览仪表 Owner 责任分布）
  const urlOwnerId = searchParams.get("owner_id");
  const [status, setStatus] = useState(urlStatus);
  // 回收站视图：deleted=true 时列出已软删术语（仅管理员/原 Owner 可恢复）
  const [deleted, setDeleted] = useState(false);
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
  // 批量状态流转（多选行 + 共享 MasterDataBatch 组件，对齐指标完整批量模式）
  const [selectedRows, setSelectedRows] = useState<GlossaryTerm[]>([]);
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
  // 当前用户（审核流评审权判断）
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  // 审核流状态（共享 hook：提交审核 Modal / 驳回 Modal / 正在审批的术语）
  const review = useMasterDataReview();
  // 评审角色判断（对齐后端 _REVIEW_ROLES）：平台管理员/域管理员/评审员可审
  const canReview =
    !!currentUser &&
    (currentUser.role === "platform_admin" ||
      currentUser.role === "domain_admin" ||
      currentUser.role === "reviewer");

  async function runBatch(action: BatchActionKey, opts: {
    codes: string[];
    reason?: string;
    changeReason?: string;
    reviewerType?: "user" | "domain" | null;
    reviewerId?: number | null;
    reviewerDomain?: string | null;
  }): Promise<BatchResult> {
    if (action === "submit") {
      return batchSubmitTerms(
        opts.codes.map((code) => ({
          code,
          change_reason: opts.changeReason ?? "批量提交审核",
          reviewer_id: opts.reviewerType === "user" ? opts.reviewerId : null,
          reviewer_type: opts.reviewerType,
          reviewer_domain: opts.reviewerType === "domain" ? opts.reviewerDomain : null,
        })),
      );
    }
    if (action === "approve") return batchApproveTerms(opts.codes);
    if (action === "reject") return batchRejectTerms(opts.codes, opts.reason ?? "");
    if (action === "publish") return batchPublishTerms(opts.codes);
    if (action === "reactivate") return batchReactivateTerms(opts.codes);
    if (action === "delete") return batchDeleteTerms(opts.codes);
    return batchDeprecateTerms(opts.codes);
  }
  // 搜索框初始值承接 URL 关键词（首查即带过滤）
  const [search, setSearch] = useState(urlKw);
  const { can } = usePermission();

  async function load(overSearch?: string) {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      const res = await listTerms({
        search: overSearch ?? search,
        status,
        deleted,
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
  }, [page, pageSize, status, ownerId, deleted]);

  // 加载当前用户术语收藏（TERM 类型）供行内收藏按钮判断；同时取当前用户供审核权判断
  useEffect(() => {
    listFavorites()
      .then((favs) =>
        setFavCodes(
          new Set(favs.filter((f) => f.asset_type === "TERM").map((f) => f.asset_id)),
        ),
      )
      .catch(() => {});
    fetchCurrentUser().then(setCurrentUser).catch(() => {});
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

  // 提交审核（DRAFT → REVIEW）：术语是业务概念标准层，发布须先审
  async function handleSubmitReview(values: ReviewSubmitBody) {
    if (!review.submitTarget) return;
    review.setSubmitBusy(true);
    try {
      await submitTerm(review.submitTarget.code, values);
      message.success(`「${review.submitTarget.name}」已提交审核，待评审通过后发布`);
      review.setSubmitTarget(null);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "提交审核失败");
    } finally {
      review.setSubmitBusy(false);
    }
  }

  // 审核通过（REVIEW → PUBLISHED）
  async function handleApprove(row: { code: string; name: string }) {
    review.setBusyCode(row.code);
    try {
      await approveTerm(row.code, { comment: null });
      message.success(`「${row.name}」审核通过，已发布`);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "审核通过失败");
    } finally {
      review.setBusyCode(null);
    }
  }

  // 审核驳回（REVIEW → DRAFT，驳回原因必填）
  async function handleReject(reason: string) {
    if (!review.rejectTarget) return;
    review.setRejectBusy(true);
    try {
      await rejectTerm(review.rejectTarget.code, { reason });
      message.success(`「${review.rejectTarget.name}」已驳回，可修改后重新提交`);
      review.setRejectTarget(null);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "驳回失败");
    } finally {
      review.setRejectBusy(false);
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

  // 生命周期（对齐维度/度量）：重新启用（DEPRECATED→DRAFT）/ 删除（软删进回收站）/ 恢复（回收站）
  async function handleReactivate(t: GlossaryTerm) {
    try {
      await reactivateTerm(t.term_code);
      message.success("已重新启用，回到草稿状态");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "重新启用失败");
    }
  }

  async function handleDelete(t: GlossaryTerm) {
    try {
      await deleteTerm(t.term_code);
      message.success("已删除，可在回收站恢复");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "删除失败");
    }
  }

  // 回收站恢复
  async function handleRestore(t: GlossaryTerm) {
    try {
      await restoreTerm(t.term_code);
      message.success("已恢复");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "恢复失败");
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
      await updateTerm(
        editTarget.term_code,
        {
          term_code: values.term_code ? String(values.term_code) : undefined,
          name: String(values.name),
          definition: String(values.definition),
          domain: String(values.domain),
          synonyms: values.synonyms ? String(values.synonyms).split(",").map((s) => s.trim()).filter(Boolean) : [],
          boundary: values.boundary ? String(values.boundary) : null,
        },
        // 乐观锁：回传当前 row_version，他人已改则后端 409（防静默覆盖）
        editTarget.row_version,
      );
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
      const res = await listTermRelations(t.term_code);
      setRelationViewItems(res.items);
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
      width: 260,
      render: (_: unknown, t: GlossaryTerm) => {
        // 回收站视图：仅展示恢复（软删项不提供编辑/审核等操作）
        if (deleted) {
          return (
            <Space wrap>
              <Popconfirm
                title="确认恢复该术语？"
                description="恢复后回到原状态（草稿/废弃），可重新走审核流"
                onConfirm={() => handleRestore(t)}
              >
                <Button size="small" type="primary" icon={<ReloadOutlined />}>
                  恢复
                </Button>
              </Popconfirm>
            </Space>
          );
        }
        return (
          <Space wrap>
            <Button size="small" type="link" onClick={() => openDetail(t)}>详情</Button>
            {can("glossary:edit") && (
              <Button size="small" type="link" onClick={() => openEdit(t)}>编辑</Button>
            )}
            <Button size="small" type="link" icon={<ApartmentOutlined />} onClick={() => openRelationView(t)}>关系</Button>
            {can("glossary:create") && (
              <Button size="small" type="link" onClick={() => openRelation(t)}>建立关系</Button>
            )}
            <Button
              size="small"
              type="link"
              icon={<HeartOutlined style={{ color: favCodes.has(t.term_code) ? "#eb2f96" : undefined }} />}
              onClick={() => toggleFavorite(t)}
            >
              {favCodes.has(t.term_code) ? "已收藏" : "收藏"}
            </Button>
            {can("glossary:edit") && (
              <MasterDataReviewActions
                row={{
                  code: t.term_code,
                  name: t.name,
                  status: t.status,
                  reviewer_type: t.reviewer_type,
                  reviewer_id: t.reviewer_id,
                  reviewer_domain: t.reviewer_domain,
                }}
                user={currentUser}
                busyCode={review.busyCode}
                onApprove={handleApprove}
                onOpenSubmit={(r) => review.setSubmitTarget({ code: r.code, name: r.name })}
                onOpenReject={(r) => review.setRejectTarget({ code: r.code, name: r.name })}
              />
            )}
            {t.status === "DEPRECATED" && currentUser?.role === "platform_admin" && can("glossary:edit") && (
              <Button size="small" type="primary" icon={<SendOutlined />} onClick={() => publishTerm(t.term_code).then(() => { message.success("已重新发布"); load(); })}>再次发布</Button>
            )}
            {t.status === "DEPRECATED" && can("glossary:edit") && (
              <Popconfirm
                title="确认重新启用该术语？"
                description="回到草稿状态，需重新提交审核后才能发布"
                onConfirm={() => handleReactivate(t)}
              >
                <Button size="small" icon={<RedoOutlined />}>重新启用</Button>
              </Popconfirm>
            )}
            {t.status !== "DEPRECATED" && can("glossary:deprecate") && (
              <Button size="small" danger onClick={() => handleDeprecate(t)}>废弃</Button>
            )}
            {(t.status === "DRAFT" || t.status === "DEPRECATED") && can("glossary:edit") && (
              <Popconfirm
                title="确认删除该术语？"
                description="删除后进入回收站，可恢复"
                onConfirm={() => handleDelete(t)}
              >
                <Button size="small" danger icon={<DeleteOutlined />} aria-label="删除">删除</Button>
              </Popconfirm>
            )}
          </Space>
        );
      },
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
          options={[{ value: "DRAFT", label: "草稿" }, { value: "REVIEW", label: "审核中" }, { value: "PUBLISHED", label: "已发布" }, { value: "DEPRECATED", label: "已废弃" }]}
        />
        <Select
          value={deleted ? "trash" : undefined}
          placeholder="回收站"
          allowClear
          style={{ width: 120 }}
          onChange={(v) => {
            setPage(1);
            setDeleted(v === "trash");
            setSelectedRows([]);
          }}
          options={[{ value: "trash", label: "回收站" }]}
        />
        {can("glossary:create") && (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新建术语</Button>
        )}
        <MasterDataBatch
          selected={selectedRows}
          codeKey="term_code"
          entityLabel="术语"
          actions={[
            { key: "submit", label: "批量提交审核（草稿）" },
            { key: "approve", label: "批量通过（审核中）" },
            { key: "reject", label: "批量驳回（审核中）" },
            { key: "publish", label: "批量发布（管理员直发）", adminOnly: true },
            { key: "reactivate", label: "批量重新启用（已废弃）" },
            { key: "deprecate", label: "批量废弃（已发布）", danger: true },
            { key: "delete", label: "批量删除（草稿/废弃）", danger: true },
          ]}
          canRun={(a) => {
            if (a === "approve" || a === "reject") return !!canReview;
            if (a === "publish") return currentUser?.role === "platform_admin";
            if (a === "deprecate") return can("glossary:deprecate");
            return can("glossary:edit");
          }}
          onRun={runBatch}
          onDone={() => {
            setSelectedRows([]);
            load();
          }}
          reviewerDomainOptions={domainOptions}
          user={currentUser}
          isAdmin={currentUser?.role === "platform_admin"}
        />
        <span className="muted">共 {total} 条</span>
      </Space>

      <Table
        dataSource={items}
        columns={columns}
        rowKey="term_code"
        loading={loading}
        rowSelection={{
          selectedRowKeys: selectedRows.map((s) => s.term_code),
          onChange: (_keys, rows) => setSelectedRows(rows),
        }}
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
            <Input placeholder="如 门诊挂号人次" />
          </Form.Item>
          <Form.Item
            label="AI 推断"
            style={{ marginBottom: 12 }}
            extra={
              can("glossary:infer")
                ? undefined
                : "无 glossary:infer 权限，AI 推断不可用（消耗 LLM 资源）"
            }
          >
            <Button
              icon={inferring ? <LoadingOutlined /> : <ThunderboltOutlined />}
              loading={inferring}
              disabled={!can("glossary:infer")}
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
            <Input placeholder="门诊人次, outpatient visits" />
          </Form.Item>
          <Form.Item name="boundary" label="边界说明">
            <Input.TextArea rows={2} placeholder="如：不含退费记录" />
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
          <Form.Item
            label="AI 推断"
            style={{ marginBottom: 12 }}
            extra={
              can("glossary:infer")
                ? undefined
                : "无 glossary:infer 权限，AI 推断不可用（消耗 LLM 资源）"
            }
          >
            <Button
              icon={inferring ? <LoadingOutlined /> : <ThunderboltOutlined />}
              loading={inferring}
              disabled={!can("glossary:infer")}
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
            <Input placeholder="门诊人次, outpatient visits" />
          </Form.Item>
          <Form.Item name="boundary" label="边界说明">
            <Input.TextArea rows={2} placeholder="如：不含退费记录" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 术语关系图谱：中心术语 + 上游（对端→本术语）+ 下游（本术语→对端），展示相互关系 */}
      <Modal
        title={
          <Space>
            <ApartmentOutlined style={{ color: "#1677ff" }} />
            <span>
              术语关系图谱
              {relationViewTerm ? (
                <span className="mono muted" style={{ fontSize: 12, marginLeft: 8 }}>
                  {relationViewTerm.name}（{relationViewTerm.term_code}）
                </span>
              ) : null}
            </span>
          </Space>
        }
        open={relationViewTerm !== null}
        onCancel={() => setRelationViewTerm(null)}
        width={720}
        footer={[
          can("glossary:create") ? (
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
            </Button>
          ) : null,
          <Button key="close" onClick={() => setRelationViewTerm(null)}>关闭</Button>,
        ]}
      >
        {relationViewTerm && (
          <div style={{ marginTop: 8 }}>
            {/* 统计条：一眼看到整体关系规模 */}
            {!relationViewLoading && relationViewItems.length > 0 && (
              <div
                style={{
                  display: "flex", justifyContent: "center", gap: 28, marginBottom: 18,
                  padding: "6px 0", background: "var(--bg-elevated, #fafafa)", borderRadius: 6,
                }}
              >
                <span className="muted" style={{ fontSize: 13 }}>
                  关联 <b style={{ color: "var(--text-1)" }}>{relationViewItems.length}</b> 个术语
                </span>
                <span className="muted" style={{ fontSize: 13 }}>
                  上游 <b style={{ color: "#2f54eb" }}>{relationViewItems.filter((i) => i.direction === "incoming").length}</b>
                </span>
                <span className="muted" style={{ fontSize: 13 }}>
                  下游 <b style={{ color: "#52c41a" }}>{relationViewItems.filter((i) => i.direction === "outgoing").length}</b>
                </span>
              </div>
            )}

            {relationViewLoading ? (
              <div style={{ textAlign: "center", padding: 32 }}>
                <LoadingOutlined style={{ marginRight: 8 }} /> 加载关系中…
              </div>
            ) : relationViewItems.length === 0 ? (
              <div style={{ textAlign: "center", padding: 36 }}>
                <div style={{ fontSize: 40, marginBottom: 12 }}>🗂️</div>
                <div style={{ fontWeight: 500 }}>暂无关联术语</div>
                <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
                  点击右下角「建立关系」添加第一个关联
                </div>
              </div>
            ) : (
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 210px 1fr",
                  alignItems: "center",
                  gap: 12,
                }}
              >
                {/* 上游：对端 → 本术语（卡片右对齐，箭头指向中心） */}
                <div style={{ minWidth: 0 }}>
                  {relationViewItems.filter((i) => i.direction === "incoming").length === 0 && (
                    <div className="muted" style={{ textAlign: "center", fontSize: 12, padding: "20px 0" }}>
                      无上游
                    </div>
                  )}
                  {relationViewItems
                    .filter((i) => i.direction === "incoming")
                    .map((i) => {
                      const meta = RELATION_TYPE_META[i.relation_type] ?? { symbol: "•", color: "#8c8c8c", label: i.relation_type };
                      return (
                        <div key={i.peer.id} style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", marginBottom: 10 }}>
                          <div
                            style={{
                              flex: 1, maxWidth: 330, border: "1px solid #eef1f4", borderLeft: `3px solid ${meta.color}`,
                              borderRadius: 8, padding: "8px 12px", background: "#fff", boxShadow: "0 1px 4px rgba(0,0,0,0.05)",
                            }}
                          >
                            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                              <span style={{ color: meta.color, fontWeight: 600 }}>{meta.symbol}</span>
                              <Tag color={meta.color} style={{ marginRight: 0 }}>{meta.label}</Tag>
                            </div>
                            <div style={{ marginTop: 4, fontWeight: 500 }}>{i.peer.name}</div>
                            <div className="mono muted" style={{ fontSize: 12 }}>
                              {i.peer.term_code}
                              {i.peer.domain ? ` · ${i.peer.domain}` : ""}
                            </div>
                          </div>
                          <div style={{ color: meta.color, fontSize: 18, width: 20, textAlign: "center", fontWeight: 600 }}>←</div>
                        </div>
                      );
                    })}
                </div>

                {/* 中心节点：视觉核心 */}
                <div style={{ textAlign: "center", padding: "4px 4px" }}>
                  <div
                    style={{
                      width: 92, height: 92, margin: "0 auto", borderRadius: "50%",
                      background: "linear-gradient(135deg, #1677ff 0%, #0958d9 100%)",
                      color: "#fff", display: "flex", flexDirection: "column",
                      alignItems: "center", justifyContent: "center",
                      boxShadow: "0 8px 24px rgba(22,119,255,0.35)",
                    }}
                  >
                    <span style={{ fontSize: 28, fontWeight: 600, lineHeight: 1.1 }}>
                      {relationViewTerm.name.slice(0, 2)}
                    </span>
                  </div>
                  <div style={{ marginTop: 10, fontWeight: 600, fontSize: 14, lineHeight: 1.3 }}>
                    {relationViewTerm.name}
                  </div>
                  <div className="mono muted" style={{ fontSize: 12 }}>{relationViewTerm.term_code}</div>
                  <div style={{ marginTop: 6 }}>
                    <Space size={4}>
                      <Tag color={relationViewTerm.domain ? "blue" : "default"}>
                        {relationViewTerm.domain ?? "未设域"}
                      </Tag>
                      <Tag color={STATUS_COLOR[relationViewTerm.status] ?? "default"}>
                        {STATUS_LABEL[relationViewTerm.status] ?? relationViewTerm.status}
                      </Tag>
                    </Space>
                  </div>
                </div>

                {/* 下游：本术语 → 对端（卡片左对齐，箭头从中心指出） */}
                <div style={{ minWidth: 0 }}>
                  {relationViewItems.filter((i) => i.direction === "outgoing").length === 0 && (
                    <div className="muted" style={{ textAlign: "center", fontSize: 12, padding: "20px 0" }}>
                      无下游
                    </div>
                  )}
                  {relationViewItems
                    .filter((i) => i.direction === "outgoing")
                    .map((i) => {
                      const meta = RELATION_TYPE_META[i.relation_type] ?? { symbol: "•", color: "#8c8c8c", label: i.relation_type };
                      return (
                        <div key={i.peer.id} style={{ display: "flex", alignItems: "center", justifyContent: "flex-start", marginBottom: 10 }}>
                          <div style={{ color: meta.color, fontSize: 18, width: 20, textAlign: "center", fontWeight: 600 }}>→</div>
                          <div
                            style={{
                              flex: 1, maxWidth: 330, border: "1px solid #eef1f4", borderLeft: `3px solid ${meta.color}`,
                              borderRadius: 8, padding: "8px 12px", background: "#fff", boxShadow: "0 1px 4px rgba(0,0,0,0.05)",
                            }}
                          >
                            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                              <span style={{ color: meta.color, fontWeight: 600 }}>{meta.symbol}</span>
                              <Tag color={meta.color} style={{ marginRight: 0 }}>{meta.label}</Tag>
                            </div>
                            <div style={{ marginTop: 4, fontWeight: 500 }}>{i.peer.name}</div>
                            <div className="mono muted" style={{ fontSize: 12 }}>
                              {i.peer.term_code}
                              {i.peer.domain ? ` · ${i.peer.domain}` : ""}
                            </div>
                          </div>
                        </div>
                      );
                    })}
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

      {/* 提交审核 + 驳回审核 Modal（共享组件）：术语发布前须评审通过 */}
      <MasterDataReviewModals
        entityLabel="术语"
        submitDescription="术语是业务概念标准层，被指标引用的标准定义。提交后由评审人审核通过才可发布；审核期间术语锁定不可编辑，驳回后可修改重提。"
        reviewerDomainOptions={domainOptions}
        user={currentUser}
        submitTarget={review.submitTarget}
        submitBusy={review.submitBusy}
        onCancelSubmit={() => review.setSubmitTarget(null)}
        onConfirmSubmit={handleSubmitReview}
        rejectTarget={review.rejectTarget}
        rejectBusy={review.rejectBusy}
        onCancelReject={() => review.setRejectTarget(null)}
        onConfirmReject={handleReject}
      />
    </div>
  );
}

function ConflictsTab() {
  const [items, setItems] = useState<GlossaryConflict[]>([]);
  const [loading, setLoading] = useState(false);
  const { can } = usePermission();

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
            <Button size="small" type="primary" disabled={!can("glossary:deprecate")} onClick={() => handleResolve(c, "RESOLVED")}>解决</Button>
            <Button size="small" disabled={!can("glossary:deprecate")} onClick={() => handleResolve(c, "IGNORED")}>忽略</Button>
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
