import { useEffect, useRef, useState, type Key } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, InputNumber, Select, message, Tabs, Space, Drawer, Descriptions, Popconfirm, Divider, Tooltip, Alert, Dropdown } from "antd";
import { DeleteOutlined, EditOutlined, PlusOutlined, RedoOutlined, ReloadOutlined, SendOutlined, ArrowLeftOutlined, HeartOutlined, DatabaseOutlined, ThunderboltOutlined, DownOutlined } from "@ant-design/icons";
import {
  listDimensions,
  createDimension,
  getDimension,
  updateDimension,
  submitDimension,
  approveDimension,
  rejectDimension,
  deprecateDimension,
  reactivateDimension,
  deleteDimension,
  restoreDimension,
  batchSubmitDimensions,
  batchApproveDimensions,
  batchRejectDimensions,
  batchDeprecateDimensions,
  batchReactivateDimensions,
  batchDeleteDimensions,
  bindMetricDimension,
  unbindMetricDimension,
  listDimensionMappings,
  createDimensionMapping,
  updateDimensionMapping,
  deleteDimensionMapping,
  listReconciliations,
  submitReconciliation,
  reviewReconciliation,
  listDimensionMembers,
  createDimensionMember,
  updateDimensionMember,
  deleteDimensionMember,
  publishDimensionMember,
  deprecateDimensionMember,
  publishAllDimensionMembers,
  listDimensionMetrics,
  listMetrics,
  listDomainTree,
  listUsers,
  listFavorites,
  addFavorite,
  removeFavorite,
  listDataSources,
  previewColumnValues,
  listSourceTables,
  listSourceDatabases,
  listSourceColumns,
  bindDimensionReference,
  refreshDimensionSnapshot,
  listDimensionSnapshots,
  getDimensionSnapshotLatestRun,
  batchPublishDimensionMembers,
  batchDeprecateDimensionMembers,
  batchDeleteDimensionMembers,
  createDimensionMappingValue,
  listDimensionMappingValues,
  deleteDimensionMappingValue,
  getMappingCoverage,
  translateDimensionValues,
  fetchCurrentUser,
  UnisenseApiError,
} from "../api";
import type { ReviewSubmitBody } from "../api";
import type {
  Dimension,
  DimensionMapping,
  Reconciliation,
  DimensionMember,
  DimensionMetricBinding,
  MetricResponse,
  SubjectDomainTreeNode,
  UserBrief,
  DataSource,
  CurrentUser,
  BatchResult,
  DimensionValueSnapshot,
  SnapshotRun,
  DimensionMappingValue,
  MappingCoverage,
  TranslateResult,
} from "../types";
import { formatCnTime } from "../utils/timeCn";
import { usePersistentPageSize } from "../hooks/usePersistentPageSize";
import { usePermission } from "../hooks/usePermission";
import { MasterDataBatch, type BatchActionKey } from "../components/MasterDataBatch";
import {
  MasterDataReviewActions,
  MasterDataReviewModals,
  useMasterDataReview,
} from "../components/MasterDataReview";

const STATUS_COLOR: Record<string, string> = { DRAFT: "default", REVIEW: "warning", PUBLISHED: "success", DEPRECATED: "error" };
const STATUS_LABEL: Record<string, string> = { DRAFT: "草稿", REVIEW: "审核中", PUBLISHED: "已发布", DEPRECATED: "已废弃" };
// 指标 6 状态中文标签/颜色（区别于维度 3 状态）：绑定指标列表列渲染使用，
// 避免用维度状态映射渲染指标状态导致 EXPERIMENTAL/REVIEW/DATA_SOURCE_DROPPED 直出英文。
const METRIC_STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  EXPERIMENTAL: "灰度",
  REVIEW: "审核中",
  PUBLISHED: "已发布",
  DEPRECATED: "已废弃",
  DATA_SOURCE_DROPPED: "数据源下线",
};
const METRIC_STATUS_COLOR: Record<string, string> = {
  DRAFT: "default",
  EXPERIMENTAL: "processing",
  REVIEW: "warning",
  PUBLISHED: "success",
  DEPRECATED: "error",
  DATA_SOURCE_DROPPED: "error",
};
const RECON_STATUS_LABEL: Record<string, string> = {
  PENDING: "待复核",
  APPROVED: "已通过",
  REJECTED: "已驳回",
};
// 指标-维度绑定角色中文标签（对齐后端 MetricDimensionRole 枚举，业务术语化：中文优先 + 英文溯源）
const ROLE_LABEL: Record<string, string> = {
  PARTITION: "分区",
  SPLICE: "拼接",
  FILTER: "过滤",
};

// 缓慢变化维类型全集（对齐后端 DimensionType 枚举：SCD0-SCD6，业务术语化：中文优先 + 英文溯源）
const SCD_TYPE_OPTIONS = [
  { value: "SCD0", label: "原样保留 (SCD0)" },
  { value: "SCD1", label: "覆盖旧值 (SCD1)" },
  { value: "SCD2", label: "保留历史 (SCD2)" },
  { value: "SCD3", label: "有限历史 (SCD3)" },
  { value: "SCD4", label: "历史表 (SCD4)" },
  { value: "SCD6", label: "混合 (SCD6)" },
];
// 缓慢变化维类型反查中文（列表「类型」列展示，与新建下拉同源，避免裸枚举）
function scdTypeLabel(v: string): string {
  return SCD_TYPE_OPTIONS.find((o) => o.value === v)?.label ?? v;
}

// 源库表/列选项框：手动输入的未在列表值作为「（手动输入）」选项（对齐指标挂载的未采集兜底模式）
function withManualOption(q: string, options: { value: string; label: string }[]) {
  const kw = (q ?? "").trim();
  if (!kw || options.some((o) => o.value === kw)) return options;
  return [{ value: kw, label: kw, manual: true }, ...options];
}
function manualOptionRender(oriOption: { data?: { label?: string; manual?: boolean } }) {
  const opt = oriOption?.data;
  if (opt?.manual) {
    return (
      <span>
        {opt.label}
        <span style={{ color: "#d46b08", marginLeft: 6 }}>（手动输入）</span>
      </span>
    );
  }
  return opt?.label ?? null;
}

// 指标-维度关联角色（对齐后端 MetricDimensionRole 枚举，业务术语化：中文优先 + 英文溯源）
const ROLE_OPTIONS = [
  { value: "PARTITION", label: "分区 (PARTITION)" },
  { value: "SPLICE", label: "拼接 (SPLICE)" },
  { value: "FILTER", label: "过滤 (FILTER)" },
];

// 递归展平主题域树 → code → 中文名映射（业务域选项框用）
function flattenDomainNames(nodes: SubjectDomainTreeNode[], acc: Map<string, string>) {
  for (const n of nodes) {
    acc.set(n.code, n.name);
    if (n.children?.length) flattenDomainNames(n.children, acc);
  }
}

function DimensionsTab() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { can } = usePermission();
  // 主表每页条数（持久化，用户可自定义）
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.dimensions.pageSize", 20);
  // 主表当前页（服务端分页）
  const [page, setPage] = useState(1);
  // URL 直达参数（?kw=）作为初始筛选，避免「先查全量再过滤」的竞态覆盖
  const urlKw = searchParams.get("kw") ?? "";
  // 生命周期状态下钻（?status=，总览仪表「维度」资产卡片）作为初始筛选
  const urlStatus = searchParams.get("status") ?? "";
  // 责任人（Owner）下钻（?owner_id=，总览仪表 Owner 责任分布）
  const urlOwnerId = searchParams.get("owner_id");
  const [items, setItems] = useState<Dimension[]>([]);
  const [total, setTotal] = useState(0);
  const [keyword, setKeyword] = useState(urlKw);
  // 搜索输入框即时显示值：与过滤值 keyword 分离——输入不打断浏览/不发请求，回车确认才过滤
  const [inputValue, setInputValue] = useState(urlKw);
  const [status, setStatus] = useState(urlStatus);
  const [ownerId, setOwnerId] = useState<number | undefined>(
    urlOwnerId && /^\d+$/.test(urlOwnerId) ? Number(urlOwnerId) : undefined,
  );
  // 回收站视图：deleted=true 时列出已软删维度（仅管理员/原 Owner 可恢复）
  const [deleted, setDeleted] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  // 维度收藏（C 层多资产收藏：DIMENSION）
  const [favCodes, setFavCodes] = useState<Set<string>>(new Set());
  const [form] = Form.useForm();
  // 编辑态：复用新建表单布局，打开时预填当前维度值
  const [editTarget, setEditTarget] = useState<Dimension | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editSaving, setEditSaving] = useState(false);
  const [editForm] = Form.useForm();
  // 绑定指标态：选择指标 + 维度内角色（partition/filter/group 等）
  const [bindTarget, setBindTarget] = useState<Dimension | null>(null);
  const [bindSaving, setBindSaving] = useState(false);
  const [bindForm] = Form.useForm();
  // 绑定指标下拉候选（指标列表）
  const [metrics, setMetrics] = useState<MetricResponse[]>([]);
  // 业务域树 → 中文名映射（新建/编辑维度的业务域选项框）
  const [domainMap, setDomainMap] = useState<Map<string, string>>(new Map());
  // 责任人 ID → 中文名映射（「责任人」列渲染）
  const [users, setUsers] = useState<UserBrief[]>([]);
  // 绑定 Modal 中「默认成员」下拉候选（当前维度的成员列表）
  const [bindMembers, setBindMembers] = useState<DimensionMember[]>([]);
  // 详情抽屉：维度详情 + 绑定指标 / 成员 / 映射三个子表格
  const [detailTarget, setDetailTarget] = useState<Dimension | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailMetrics, setDetailMetrics] = useState<DimensionMetricBinding[]>([]);
  const [detailMembers, setDetailMembers] = useState<DimensionMember[]>([]);
  const [detailMappings, setDetailMappings] = useState<DimensionMapping[]>([]);
  // 并发查询防竞态：只有最后一次发起的请求允许落地结果
  const loadSeq = useRef(0);
  // 当前用户（审核权判断：指派评审人/域评审组/域管理员兜底）
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  // 审核流状态（共享 hook：提交审核 Modal / 驳回 Modal / 正在审批的维度）
  const review = useMasterDataReview();
  // 批量操作：多选行（rowSelection）+ 共享 MasterDataBatch 组件（对齐指标完整批量模式）
  const [selected, setSelected] = useState<Dimension[]>([]);
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
      return batchSubmitDimensions(
        opts.codes.map((code) => ({
          code,
          change_reason: opts.changeReason ?? "批量提交审核",
          reviewer_id: opts.reviewerType === "user" ? opts.reviewerId : null,
          reviewer_type: opts.reviewerType,
          reviewer_domain: opts.reviewerType === "domain" ? opts.reviewerDomain : null,
        })),
      );
    }
    if (action === "approve") return batchApproveDimensions(opts.codes);
    if (action === "reject") return batchRejectDimensions(opts.codes, opts.reason ?? "");
    if (action === "reactivate") return batchReactivateDimensions(opts.codes);
    if (action === "delete") return batchDeleteDimensions(opts.codes);
    return batchDeprecateDimensions(opts.codes);
  }

  // 支持从全局搜索栏经 ?kw= 直达定位；初始值已由 useState 承接，
  // 此处仅同步「URL 出现新筛选值」的场景，并保留用户手动清空筛选的能力。
  useEffect(() => {
    if (urlKw && urlKw !== keyword) {
      setKeyword(urlKw);
      setInputValue(urlKw);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlKw]);

  // 响应 URL 状态参数变化（总览仪表「维度」资产卡片二次下钻）；status 在 load 依赖中自动重查
  useEffect(() => {
    if (urlStatus && urlStatus !== status) setStatus(urlStatus);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlStatus]);

  // 响应 URL 责任人参数变化（Owner 责任分布二次下钻）；ownerId 在 load 依赖中自动重查
  useEffect(() => {
    if (urlOwnerId && /^\d+$/.test(urlOwnerId) && Number(urlOwnerId) !== ownerId) {
      setOwnerId(Number(urlOwnerId));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlOwnerId]);

  // 编辑 Modal 打开时预填当前维度值（基于列表行，getDimension 拉最新后覆盖）
  useEffect(() => {
    if (editOpen && editTarget) {
      editForm.setFieldsValue({
        name: editTarget.name,
        domain: editTarget.domain,
        type: editTarget.type,
        description: editTarget.description ?? undefined,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editOpen, editTarget]);

  // 绑定指标候选（指标列表，失败静默不影响列表主流程）；同时加载业务域树供选项框使用
  useEffect(() => {
    // page_size 取后端上限（semantic MetricQuery le=100），避免 422
    listMetrics({ page_size: 100 })
      .then((r) => setMetrics(r.items))
      .catch(() => {});
    listDomainTree()
      .then((tree) => {
        const m = new Map<string, string>();
        flattenDomainNames(tree, m);
        setDomainMap(m);
      })
      .catch(() => {});
    // 责任人候选（失败静默：责任人列回退「用户 #id」）
    listUsers().then(setUsers).catch(() => {});
    // 当前用户维度收藏（DIMENSION）供行内收藏按钮判断
    listFavorites()
      .then((favs) =>
        setFavCodes(
          new Set(favs.filter((f) => f.asset_type === "DIMENSION").map((f) => f.asset_id)),
        ),
      )
      .catch(() => {});
    // 当前用户（审核流评审权判断）
    fetchCurrentUser().then(setCurrentUser).catch(() => {});
  }, []);

  // 维度收藏切换（行内心形）
  async function toggleFavorite(d: Dimension) {
    const fav = favCodes.has(d.dim_code);
    try {
      if (fav) {
        await removeFavorite("DIMENSION", d.dim_code);
        setFavCodes((prev) => {
          const next = new Set(prev);
          next.delete(d.dim_code);
          return next;
        });
        message.success("已取消收藏");
      } else {
        await addFavorite("DIMENSION", d.dim_code);
        setFavCodes((prev) => new Set(prev).add(d.dim_code));
        message.success("已收藏");
      }
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "收藏操作失败",
      );
    }
  }

  async function load(overrideKeyword?: string) {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      const res = await listDimensions({
        keyword: (overrideKeyword ?? keyword) || undefined,
        status: status || undefined,
        owner_id: ownerId,
        deleted,
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
  }, [keyword, status, ownerId, deleted, page]);

  async function handleCreate(values: Record<string, unknown>) {
    if (saving) return;
    setSaving(true);
    try {
      await createDimension({
        dim_code: values.dim_code ? String(values.dim_code) : undefined,
        name: String(values.name),
        domain: String(values.domain),
        type: String(values.type ?? "SCD1"),
        description: values.description ? String(values.description) : null,
      });
      message.success("维度已创建");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    } finally {
      setSaving(false);
    }
  }

  // 提交审核（DRAFT → REVIEW）：维度是下游指标绑定/消费校验的权威来源，发布须先审
  async function handleSubmitReview(values: ReviewSubmitBody) {
    if (!review.submitTarget) return;
    review.setSubmitBusy(true);
    try {
      await submitDimension(review.submitTarget.code, values);
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
      await approveDimension(row.code, { comment: null });
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
      await rejectDimension(review.rejectTarget.code, { reason });
      message.success(`「${review.rejectTarget.name}」已驳回，可修改后重新提交`);
      review.setRejectTarget(null);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "驳回失败");
    } finally {
      review.setRejectBusy(false);
    }
  }

  async function handleDeprecate(d: Dimension) {
    try {
      await deprecateDimension(d.dim_code);
      message.success("已废弃");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "废弃失败");
    }
  }

  // 重新启用（DEPRECATED → DRAFT，可编辑后重新走审核）
  async function handleReactivate(d: Dimension) {
    try {
      await reactivateDimension(d.dim_code);
      message.success("已重新启用，回到草稿，请提交审核后发布");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "重新启用失败");
    }
  }

  // 软删除（仅 DRAFT/DEPRECATED 可删；管理员或原 Owner）
  async function handleDelete(d: Dimension) {
    try {
      await deleteDimension(d.dim_code);
      message.success("已删除，可在回收站恢复");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "删除失败");
    }
  }

  // 回收站恢复
  async function handleRestore(d: Dimension) {
    try {
      await restoreDimension(d.dim_code);
      message.success("已恢复");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "恢复失败");
    }
  }

  // 「更多」下拉中的危险操作二次确认（废弃/删除）——Modal.confirm 与 Dropdown menu 搭配的标准做法
  function confirmDeprecate(d: Dimension) {
    Modal.confirm({
      title: "确认废弃该维度？",
      content: "废弃后为终态，可重新启用（回到草稿重新审核）；被指标绑定的维度无法废弃。",
      okText: "确认",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => handleDeprecate(d),
    });
  }

  function confirmReactivate(d: Dimension) {
    Modal.confirm({
      title: "确认重新启用该维度？",
      content: "回到草稿状态，需重新提交审核后才能发布。",
      okText: "确认",
      cancelText: "取消",
      onOk: () => handleReactivate(d),
    });
  }

  function confirmDelete(d: Dimension) {
    Modal.confirm({
      title: "确认删除该维度？",
      content: "删除后进入回收站，可恢复；被指标绑定的维度无法删除。",
      okText: "确认",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => handleDelete(d),
    });
  }

  // 打开「绑定指标」弹窗（下拉菜单复用）：重置表单 + 加载指标候选与默认成员
  async function openBindMetric(d: Dimension) {
    bindForm.resetFields();
    setBindTarget(d);
    try {
      const r = await listMetrics({ page_size: 100 });
      setMetrics(r.items);
    } catch { /* 静默：已有候选可降级 */ }
    try {
      const r = await listDimensionMembers(d.dim_code);
      setBindMembers(r.items);
    } catch {
      setBindMembers([]);
    }
  }

  // 打开编辑：先拉取最新详情确保基于最新数据（详情端点接线）
  async function openEdit(d: Dimension) {
    setEditTarget(d);
    setEditOpen(true);
    try {
      const fresh = await getDimension(d.dim_code);
      editForm.setFieldsValue({
        dim_code: fresh.dim_code,
        name: fresh.name,
        domain: fresh.domain,
        type: fresh.type,
        description: fresh.description ?? undefined,
      });
    } catch {
      // 详情拉取失败不阻塞：仍可用列表数据编辑
    }
  }

  async function handleEdit(values: Record<string, unknown>) {
    if (!editTarget) return;
    setEditSaving(true);
    try {
      // 编码仅 DRAFT 状态可改（后端强校验）；非 DRAFT 时不传编码，避免误改
      const canEditCode = editTarget.status === "DRAFT";
      await updateDimension(
        editTarget.dim_code,
        {
          ...(canEditCode && values.dim_code ? { dim_code: String(values.dim_code) } : {}),
          name: values.name ? String(values.name) : undefined,
          domain: values.domain ? String(values.domain) : undefined,
          type: values.type ? String(values.type) : undefined,
          description: values.description ? String(values.description) : null,
        },
        // 乐观锁：回传当前 row_version，他人已改则后端 409（防静默覆盖）
        editTarget.row_version,
      );
      message.success("维度已更新");
      setEditOpen(false);
      editForm.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "更新失败");
    } finally {
      setEditSaving(false);
    }
  }

  async function handleBind(values: Record<string, unknown>) {
    if (!bindTarget) return;
    setBindSaving(true);
    try {
      await bindMetricDimension({
        metric_id: Number(values.metric_id),
        dim_code: bindTarget.dim_code,
        role: String(values.role ?? "FILTER"),
        default_member: values.default_member ? String(values.default_member) : null,
      });
      message.success(`指标已绑定到维度「${bindTarget.dim_code}」`);
      setBindTarget(null);
      bindForm.resetFields();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "绑定失败");
    } finally {
      setBindSaving(false);
    }
  }

  // 打开详情抽屉：并行拉取绑定指标 / 成员 / 映射，任一失败静默降级（不影响整体展示）
  function openDetail(d: Dimension) {
    setDetailTarget(d);
    setDetailMetrics([]);
    setDetailMembers([]);
    setDetailMappings([]);
    setDetailLoading(true);
    listDimensionMetrics(d.dim_code).then((r) => setDetailMetrics(r.items)).catch(() => setDetailMetrics([]));
    listDimensionMembers(d.dim_code).then((r) => setDetailMembers(r.items)).catch(() => setDetailMembers([]));
    listDimensionMappings().then((r) => {
      // 仅展示与该维度相关的映射（源或目标）
      setDetailMappings(r.items.filter((m) => m.source_dim_code === d.dim_code || m.target_dim_code === d.dim_code));
    }).catch(() => setDetailMappings([])).finally(() => setDetailLoading(false));
  }

  // 解绑指标（撤销误绑/改绑）：删除绑定 + 从指标声明维度移除，刷新详情绑定列表
  async function handleUnbindMetric(metricId: number, metricCode: string) {
    if (!detailTarget) return;
    try {
      await unbindMetricDimension(detailTarget.dim_code, metricId);
      message.success(`已解除绑定：${metricCode}`);
      // 刷新详情绑定指标列表（其余区块无需刷新）
      const r = await listDimensionMetrics(detailTarget.dim_code);
      setDetailMetrics(r.items);
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "解绑失败",
      );
    }
  }

  // 责任人 ID → 中文名（无记录回退「用户 #id」）
  const ownerName = (ownerId: number) =>
    users.find((u) => u.id === ownerId)?.display_name ?? `用户 #${ownerId}`;

  const columns = [
    { title: "编码", dataIndex: "dim_code", key: "dim_code", render: (v: string) => <span className="mono">{v}</span> },
    { title: "名称", dataIndex: "name", key: "name" },
    { title: "业务域", dataIndex: "domain", key: "domain", width: 130, render: (v: string) => domainMap.get(v) ?? v },
    { title: "责任人", dataIndex: "owner_id", key: "owner", width: 120, render: (v: number) => ownerName(v) },
    { title: "类型", dataIndex: "type", key: "type", width: 130, render: (v: string) => <Tag>{scdTypeLabel(v)}</Tag> },
    { title: "绑定指标", dataIndex: "metric_count", key: "metric_count", width: 90, render: (v?: number) => (v ?? 0) > 0 ? v : <span className="muted">0</span> },
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
      render: (_: unknown, d: Dimension) => {
        if (deleted) {
          return (
            <Popconfirm
              title="确认恢复该维度？"
              description="恢复后回到原状态（草稿/废弃），可重新走审核流"
              onConfirm={() => handleRestore(d)}
            >
              <Button size="small" type="primary" icon={<ReloadOutlined />} disabled={!can("dimension:edit")}>
                恢复
              </Button>
            </Popconfirm>
          );
        }
        const isDeprecated = d.status === "DEPRECATED";
        // 主操作：详情/编辑 + 审核动作（状态相关）；低频管理操作收进「更多」下拉
        const menuItems: any[] = [
          {
            key: "fav",
            icon: <HeartOutlined style={{ color: favCodes.has(d.dim_code) ? "#eb2f96" : undefined }} />,
            label: favCodes.has(d.dim_code) ? "取消收藏" : "收藏",
          },
        ];
        if (!isDeprecated) {
          if (can("dimension:edit")) {
            menuItems.push({ key: "bind", icon: <DatabaseOutlined />, label: "绑定指标" });
          }
          if (can("dimension:deprecate")) {
            menuItems.push({ type: "divider" });
            menuItems.push({ key: "deprecate", icon: <DeleteOutlined />, label: "废弃", danger: true });
          }
        } else {
          menuItems.push({ type: "divider" });
          if (can("dimension:edit")) {
            menuItems.push({ key: "reactivate", icon: <RedoOutlined />, label: "重新启用" });
          }
        }
        if (can("dimension:edit") && (d.status === "DRAFT" || isDeprecated)) {
          menuItems.push({ key: "delete", icon: <DeleteOutlined />, label: "删除", danger: true });
        }
        return (
          <Space size={8} wrap>
            <Button size="small" type="link" onClick={() => openDetail(d)}>
              详情
            </Button>
            {!isDeprecated && can("dimension:edit") && (
              <Button size="small" type="link" icon={<EditOutlined />} onClick={() => openEdit(d)}>
                编辑
              </Button>
            )}
            {!isDeprecated && (
              <MasterDataReviewActions
                row={{
                  code: d.dim_code,
                  name: d.name,
                  status: d.status,
                  reviewer_type: d.reviewer_type,
                  reviewer_id: d.reviewer_id,
                  reviewer_domain: d.reviewer_domain,
                }}
                user={currentUser}
                busyCode={review.busyCode}
                onApprove={handleApprove}
                onOpenSubmit={(r) => review.setSubmitTarget({ code: r.code, name: r.name })}
                onOpenReject={(r) => review.setRejectTarget({ code: r.code, name: r.name })}
                canSubmit={can("dimension:create") || can("dimension:edit")}
              />
            )}
            <Dropdown
              trigger={["click"]}
              menu={{
                items: menuItems,
                onClick: ({ key }) => {
                  if (key === "fav") toggleFavorite(d);
                  else if (key === "bind") openBindMetric(d);
                  else if (key === "deprecate") confirmDeprecate(d);
                  else if (key === "reactivate") confirmReactivate(d);
                  else if (key === "delete") confirmDelete(d);
                },
              }}
            >
              <Button size="small">
                更多 <DownOutlined />
              </Button>
            </Dropdown>
          </Space>
        );
      },
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Input.Search
          placeholder="搜索维度编码 / 名称 / 描述"
          allowClear
          style={{ width: 260 }}
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onSearch={() => setKeyword(inputValue)}
          onClear={() => {
            setInputValue("");
            setKeyword("");
          }}
        />
        <Select
          placeholder="状态筛选"
          allowClear
          style={{ width: 140 }}
          value={status || undefined}
          onChange={(v) => setStatus(v ?? "")}
          options={[
            { value: "DRAFT", label: "草稿" },
            { value: "REVIEW", label: "审核中" },
            { value: "PUBLISHED", label: "已发布" },
            { value: "DEPRECATED", label: "已废弃" },
          ]}
        />
        <Select
          value={deleted ? "trash" : undefined}
          placeholder="回收站"
          allowClear
          style={{ width: 120 }}
          onChange={(v) => {
            setPage(1);
            setDeleted(v === "trash");
            setSelected([]);
          }}
          options={[{ value: "trash", label: "回收站" }]}
        />
        {can("dimension:create") && (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新建维度</Button>
        )}
        <MasterDataBatch
          selected={selected}
          codeKey="dim_code"
          entityLabel="维度"
          actions={[
            { key: "submit", label: "批量提交审核（草稿）" },
            { key: "approve", label: "批量通过（审核中）" },
            { key: "reject", label: "批量驳回（审核中）" },
            { key: "reactivate", label: "批量重新启用（已废弃）" },
            { key: "deprecate", label: "批量废弃（已发布）", danger: true },
            { key: "delete", label: "批量删除（草稿/废弃）", danger: true },
          ]}
          canRun={(a) => (a === "approve" || a === "reject" ? !!canReview : can("dimension:create") || can("dimension:deprecate"))}
          onRun={runBatch}
          onDone={() => {
            setSelected([]);
            load();
          }}
          reviewerDomainOptions={[...domainMap.entries()].map(([value, name]) => ({ value, label: `${name} (${value})` }))}
          user={currentUser}
        />
      </Space>
      <Table
        dataSource={items}
        columns={columns}
        rowKey="dim_code"
        loading={loading}
        rowSelection={{
          selectedRowKeys: selected.map((s) => s.dim_code),
          onChange: (_keys, rows) => setSelected(rows),
        }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          onShowSizeChange: (p: number, ps: number) => {
            setPage(1);
            onShowSizeChange(p, ps);
          },
          onChange: (p: number) => setPage(p),
          pageSizeOptions: [10, 20, 50, 100],
          showTotal: (t: number) => `共 ${t} 条`,
        }}
        locale={{ emptyText: "暂无维度" }}
        onRow={(d) => ({
          onClick: (e) => {
            // 点击行打开详情抽屉；但需避开行内按钮/链接，避免与操作按钮触发冲突
            if ((e.target as HTMLElement).closest("button, a")) return;
            openDetail(d);
          },
        })}
      />

      <Modal title="新建维度" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="创建" confirmLoading={saving}>
        <Form form={form} layout="vertical" scrollToFirstError onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="dim_code" label="维度编码" extra={<span className="mono" style={{ color: "#0E7C86" }}>留空则由系统自动生成</span>}>
            <Input className="mono" placeholder="留空自动生成" />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如 科室" />
          </Form.Item>
          <Form.Item name="domain" label="业务域" rules={[{ required: true }]}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择业务域"
              options={Array.from(domainMap.entries()).map(([code, name]) => ({
                value: code,
                label: `${name}（${code}）`,
              }))}
            />
          </Form.Item>
          <Form.Item name="type" label="缓慢变化维类型">
            <Select options={SCD_TYPE_OPTIONS} placeholder="选择类型" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={editTarget ? `编辑维度：${editTarget.dim_code}` : "编辑维度"}
        open={editOpen}
        onCancel={() => {
          setEditOpen(false);
          editForm.resetFields();
        }}
        onOk={() => editForm.submit()}
        okText="保存"
        confirmLoading={editSaving}
      >
        <Form form={editForm} layout="vertical" scrollToFirstError onFinish={handleEdit} style={{ marginTop: 8 }}>
          <Form.Item
            name="dim_code"
            label="维度编码"
            extra={
              editTarget?.status === "DRAFT" ? (
                <span style={{ color: "#0E7C86" }}>草稿状态可修改；已发布/已废弃禁止</span>
              ) : (
                <span className="muted">已发布/已废弃维度编码不可修改</span>
              )
            }
            rules={[
              { required: true, message: "请输入维度编码" },
              { pattern: /^[a-z][a-z0-9_]*$/, message: "仅小写字母/数字/下划线，且不以数字开头" },
            ]}
          >
            <Input className="mono" disabled={editTarget?.status !== "DRAFT"} />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如 科室" />
          </Form.Item>
          <Form.Item name="domain" label="业务域" rules={[{ required: true }]}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择业务域"
              options={Array.from(domainMap.entries()).map(([code, name]) => ({
                value: code,
                label: `${name}（${code}）`,
              }))}
            />
          </Form.Item>
          <Form.Item name="type" label="缓慢变化维类型">
            <Select options={SCD_TYPE_OPTIONS} placeholder="选择类型" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={bindTarget ? `绑定指标 → ${bindTarget.dim_code}` : "绑定指标"}
        open={bindTarget != null}
        onCancel={() => setBindTarget(null)}
        onOk={() => bindForm.submit()}
        okText="绑定"
        confirmLoading={bindSaving}
      >
        <Form
          form={bindForm}
          layout="vertical" scrollToFirstError
          initialValues={{ role: "FILTER" }}
          onFinish={handleBind}
          style={{ marginTop: 8 }}
        >
          <Form.Item name="metric_id" label="指标" rules={[{ required: true }]}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择指标"
              notFoundContent={metrics.length === 0 ? "暂无指标，请先在指标目录创建" : "无匹配指标"}
              options={metrics.map((m) => ({
                value: m.id,
                label: `${m.metric_code} · ${m.name}`,
              }))}
            />
          </Form.Item>
          <Form.Item name="role" label="维度角色" extra="标识该指标如何消费此维度">
            <Select options={ROLE_OPTIONS} placeholder="选择角色" />
          </Form.Item>
          <Form.Item name="default_member" label="默认成员">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择该维度下已发布的成员"
              notFoundContent={
                bindMembers.length === 0
                  ? "该维度暂无成员"
                  : "无已发布成员（须先发布成员才能绑定为默认值）"
              }
              options={bindMembers
                .filter((mem) => mem.status === "PUBLISHED")
                .map((mem) => ({
                  value: mem.member_code,
                  label: mem.path ? `${mem.path}（${mem.member_name}）` : `${mem.member_code} · ${mem.member_name}`,
                }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        title={detailTarget ? `维度详情：${detailTarget.dim_code} · ${detailTarget.name}` : "维度详情"}
        open={detailTarget != null}
        onClose={() => setDetailTarget(null)}
        width={860}
      >
        {detailTarget && (
          <>
            <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label="编码">{detailTarget.dim_code}</Descriptions.Item>
              <Descriptions.Item label="名称">{detailTarget.name}</Descriptions.Item>
              <Descriptions.Item label="业务域">{domainMap.get(detailTarget.domain) ?? detailTarget.domain}</Descriptions.Item>
              <Descriptions.Item label="SCD 类型">{scdTypeLabel(detailTarget.type)}</Descriptions.Item>
              <Descriptions.Item label="责任人">{ownerName(detailTarget.owner_id)}</Descriptions.Item>
              <Descriptions.Item label="状态">
                <Tag color={STATUS_COLOR[detailTarget.status]}>{STATUS_LABEL[detailTarget.status] ?? detailTarget.status}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="描述">{detailTarget.description || <span className="muted">—</span>}</Descriptions.Item>
              <Descriptions.Item label="创建时间">{detailTarget.created_at ? formatCnTime(detailTarget.created_at) : "—"}</Descriptions.Item>
              <Descriptions.Item label="更新时间">{detailTarget.updated_at ? formatCnTime(detailTarget.updated_at) : "—"}</Descriptions.Item>
            </Descriptions>

            <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>绑定指标（{detailMetrics.length}）</div>
            <Table<DimensionMetricBinding>
              dataSource={detailMetrics}
              rowKey="metric_id"
              size="small"
              loading={detailLoading}
              pagination={false}
              locale={{ emptyText: "暂无绑定指标" }}
              columns={[
                {
                  title: "指标编码",
                  dataIndex: "metric_code",
                  key: "code",
                  render: (v: string) => (
                    <a className="mono" onClick={() => navigate(`/detail/${encodeURIComponent(v)}`)}>{v}</a>
                  ),
                },
                {
                  title: "指标名称",
                  dataIndex: "metric_name",
                  key: "name",
                  render: (v: string, r: { metric_code: string }) => (
                    <a onClick={() => navigate(`/detail/${encodeURIComponent(r.metric_code)}`)}>{v ?? "—"}</a>
                  ),
                },
                { title: "角色", dataIndex: "role", key: "role", render: (v: string) => ROLE_LABEL[v] ?? v },
                { title: "默认成员", dataIndex: "default_member", key: "dm", render: (v: string | null) => v ?? <span className="muted">—</span> },
                { title: "指标状态", dataIndex: "metric_status", key: "status", width: 100, render: (s: string) => <Tag color={METRIC_STATUS_COLOR[s]}>{METRIC_STATUS_LABEL[s] ?? s}</Tag> },
                {
                  title: "操作",
                  key: "actions",
                  width: 80,
                  render: (_: unknown, r: DimensionMetricBinding) => (
                    <Popconfirm
                      title={`解除与 ${r.metric_code} 的绑定？`}
                      description="解绑后该指标口径中不再声明此维度（撤销误绑/改绑）"
                      okText="解绑"
                      cancelText="取消"
                      onConfirm={() => handleUnbindMetric(r.metric_id, r.metric_code)}
                    >
                      {can("dimension:edit") ? <Button size="small" type="link" danger>解绑</Button> : null}
                    </Popconfirm>
                  ),
                },
              ]}
            />

            <div className="muted" style={{ fontSize: 12, margin: "16px 0 8px" }}>成员（{detailMembers.length}）</div>
            <Table
              dataSource={detailMembers}
              rowKey="member_code"
              size="small"
              pagination={false}
              scroll={{ y: 260 }}
              loading={detailLoading}
              locale={{ emptyText: "暂无成员" }}
              columns={[
                { title: "成员编码", dataIndex: "member_code", key: "code", render: (v: string) => <span className="mono">{v}</span> },
                { title: "名称", dataIndex: "member_name", key: "name" },
                { title: "路径", dataIndex: "path", key: "path", render: (v: string | null) => v ? <span className="mono">{v}</span> : <span className="muted">—</span> },
              ]}
            />

            <div className="muted" style={{ fontSize: 12, margin: "16px 0 8px" }}>维度映射（{detailMappings.length}）</div>
            <Table
              dataSource={detailMappings}
              rowKey="id"
              size="small"
              pagination={false}
              scroll={{ y: 260 }}
              loading={detailLoading}
              locale={{ emptyText: "暂无相关映射" }}
              columns={[
                { title: "源维度", dataIndex: "source_dim_code", key: "src", render: (v: string) => <span className="mono">{v}</span> },
                { title: "目标维度", dataIndex: "target_dim_code", key: "tgt", render: (v: string) => <span className="mono">{v}</span> },
                { title: "映射类型", dataIndex: "mapping_type", key: "type", render: (v: string) => <Tag color={v === "EQUIVALENT" ? "success" : "warning"}>{v === "EQUIVALENT" ? "等价" : "部分"}</Tag> },
                { title: "表达式", dataIndex: "expression", key: "expr", render: (v: string | null) => v ? <span className="mono">{v}</span> : <span className="muted">—</span> },
              ]}
            />
          </>
        )}
      </Drawer>

      {/* 提交审核 + 驳回审核 Modal（共享组件）：维度发布前须评审通过 */}
      <MasterDataReviewModals
        entityLabel="维度"
        submitDescription="维度是下游指标绑定/消费校验的权威来源。提交后由评审人审核通过才可发布；审核期间维度锁定不可编辑，驳回后可修改重提。"
        reviewerDomainOptions={Array.from(domainMap.entries()).map(([code, name]) => ({
          value: code,
          label: `${name}（${code}）`,
        }))}
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

// 层级路径实时预览：选择父级/输入编码时自动推算将生成的路径（提交时交由后端兜底）
function PathPreview({
  form,
  members,
}: {
  form: ReturnType<typeof Form.useForm>[0];
  members: DimensionMember[];
}) {
  const parentCode = Form.useWatch("parent_code", form);
  const memberCode = Form.useWatch("member_code", form);
  let path: string;
  if (parentCode) {
    const parent = members.find((m) => m.member_code === parentCode);
    const base = parent?.path ?? `/${parentCode}`;
    path = memberCode ? `${base}/${memberCode}` : `${base}/{member_code}`;
  } else if (memberCode) {
    path = `/${memberCode}`;
  } else {
    path = "/{member_code}";
  }
  return (
    <div className="muted" style={{ fontSize: 12 }}>
      层级路径将自动生成：<code className="mono">{path}</code>
    </div>
  );
}

// 成员树节点：平铺成员按 parent_code 组装出层级后带 children 子集
type MemberTreeNode = DimensionMember & { children: MemberTreeNode[] };

// 平铺成员 → 树：根 = parent_code 为 null 或父级不存在的成员；同级按 path 排序保持稳定
function buildMemberTree(members: DimensionMember[]): MemberTreeNode[] {
  const byCode = new Map<string, MemberTreeNode>();
  for (const m of members) byCode.set(m.member_code, { ...m, children: [] });
  const roots: MemberTreeNode[] = [];
  for (const m of members) {
    const node = byCode.get(m.member_code)!;
    const parent = m.parent_code ? byCode.get(m.parent_code) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  const sortByPath = (arr: MemberTreeNode[]) => arr.sort((a, b) => (a.path ?? a.member_code).localeCompare(b.path ?? b.member_code));
  sortByPath(roots);
  for (const node of byCode.values()) sortByPath(node.children);
  return roots;
}

function MembersTab() {
  const { can } = usePermission();
  const [dims, setDims] = useState<Dimension[]>([]);
  const [dimCode, setDimCode] = useState<string | undefined>(undefined);
  const [members, setMembers] = useState<DimensionMember[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();
  // 编辑态：复用新增布局，打开时预填当前成员值
  const [editTarget, setEditTarget] = useState<DimensionMember | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editSaving, setEditSaving] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editForm] = Form.useForm();
  // 从表自动获取枚举值：数据源列表 + 弹窗 + 预览结果
  const [dataSources, setDataSources] = useState<DataSource[]>([]);
  const [autoOpen, setAutoOpen] = useState(false);
  const [autoLoading, setAutoLoading] = useState(false);
  const [autoForm] = Form.useForm();
  const [previewValues, setPreviewValues] = useState<string[]>([]);
  const [previewTruncated, setPreviewTruncated] = useState(false);
  const [importing, setImporting] = useState(false);
  const [batchPublishing, setBatchPublishing] = useState(false);
  // 源库表/列选项框（从表自动获取 + 绑定引用型共用）：选数据源加载表、选表加载列
  const [sourceTables, setSourceTables] = useState<{ database: string; table: string; name: string }[]>([]);
  const [sourceColumns, setSourceColumns] = useState<{ name: string; data_type: string | null; comment: string | null }[]>([]);
  const [sourceTablesLoading, setSourceTablesLoading] = useState(false);
  const [sourceColumnsLoading, setSourceColumnsLoading] = useState(false);
  // 级联选表：目标库列表（数据源选中后轻量加载）与加载态
  const [sourceDatabases, setSourceDatabases] = useState<string[]>([]);
  const [sourceDatabasesLoading, setSourceDatabasesLoading] = useState(false);
  const [tableKw, setTableKw] = useState("");
  const [columnKw, setColumnKw] = useState("");
  // 导入进度感知：当前处理序号/总数（消除"点了没反应"的长等待）
  const [importProgress, setImportProgress] = useState<{ ok: number; failed: number; done: number; total: number } | null>(null);
  // 批量操作：rowSelection 勾选的成员编码集合
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [batchLoading, setBatchLoading] = useState(false);
  // 引用型维度（sync_mode=snapshot）：绑定表列 + 快照刷新 + 数据质量摘要
  const [bindOpen, setBindOpen] = useState(false);
  const [bindForm] = Form.useForm();
  const [bindSaving, setBindSaving] = useState(false);
  const [latestRun, setLatestRun] = useState<SnapshotRun | null>(null);
  const [snapshots, setSnapshots] = useState<DimensionValueSnapshot[]>([]);
  const [snapshotOpen, setSnapshotOpen] = useState(false);
  const [snapshotLoading, setSnapshotLoading] = useState(false);
  const [refreshingSnapshot, setRefreshingSnapshot] = useState(false);
  // 当前选中维度的完整对象（含 sync_mode/source_* 字段）
  const currentDim = dims.find((d) => d.dim_code === dimCode) ?? null;
  const isSnapshot = currentDim?.sync_mode === "snapshot";

  // 引用型维度：选择后加载最近一次快照运行记录
  useEffect(() => {
    if (!dimCode || !isSnapshot) {
      setLatestRun(null);
      setSnapshots([]);
      return;
    }
    getDimensionSnapshotLatestRun(dimCode)
      .then((r) => setLatestRun(r))
      .catch(() => setLatestRun(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dimCode, isSnapshot]);

  async function loadLatestRun() {
    if (!dimCode) return;
    try {
      setLatestRun(await getDimensionSnapshotLatestRun(dimCode));
    } catch {
      setLatestRun(null);
    }
  }

  async function handleBindReference(values: Record<string, unknown>) {
    if (!dimCode || bindSaving) return;
    setBindSaving(true);
    try {
      await bindDimensionReference(dimCode, {
        source_id: String(values.source_id),
        table: String(values.table),
        column: String(values.column),
        refresh_interval_hours: values.refresh_interval_hours ? Number(values.refresh_interval_hours) : 24,
      });
      message.success("已绑定引用型值来源（维度值 = 源表列快照）");
      setBindOpen(false);
      bindForm.resetFields();
      listDimensions({ page_size: 200 }).then((r) => setDims(r.items)).catch(() => {});
      await loadLatestRun();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "绑定失败");
    } finally {
      setBindSaving(false);
    }
  }

  async function handleRefreshSnapshot() {
    if (!dimCode || refreshingSnapshot) return;
    setRefreshingSnapshot(true);
    try {
      const r = await refreshDimensionSnapshot(dimCode);
      message.success(
        `快照已刷新：共 ${r.total} 个值，新增 ${r.added.length}，消失 ${r.removed.length}${r.null_count ? `，空值 ${r.null_count}` : ""}`,
      );
      await loadLatestRun();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "刷新快照失败");
    } finally {
      setRefreshingSnapshot(false);
    }
  }

  async function openSnapshotList() {
    if (!dimCode) return;
    setSnapshotLoading(true);
    setSnapshotOpen(true);
    try {
      const r = await listDimensionSnapshots(dimCode, 1, 200);
      setSnapshots(r.items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载快照失败");
    } finally {
      setSnapshotLoading(false);
    }
  }

  // 批量发布/废弃/删除（勾选成员）
  async function handleBatchPublish() {
    if (!dimCode || selectedKeys.length === 0 || batchLoading) return;
    setBatchLoading(true);
    try {
      const r = await batchPublishDimensionMembers(dimCode, selectedKeys);
      message.success(`已发布 ${r.published} 个${r.skipped ? `（跳过 ${r.skipped} 个非草稿）` : ""}${r.failed.length ? `，失败 ${r.failed.length}` : ""}`);
      setSelectedKeys([]);
      reload();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量发布失败");
    } finally {
      setBatchLoading(false);
    }
  }

  async function handleBatchDeprecate() {
    if (!dimCode || selectedKeys.length === 0 || batchLoading) return;
    setBatchLoading(true);
    try {
      const r = await batchDeprecateDimensionMembers(dimCode, selectedKeys);
      message.success(`已废弃 ${r.deprecated} 个${r.skipped ? `（跳过 ${r.skipped} 个已废弃）` : ""}${r.failed.length ? `，失败 ${r.failed.length}` : ""}`);
      setSelectedKeys([]);
      reload();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量废弃失败");
    } finally {
      setBatchLoading(false);
    }
  }

  async function handleBatchDelete() {
    if (!dimCode || selectedKeys.length === 0 || batchLoading) return;
    setBatchLoading(true);
    try {
      const r = await batchDeleteDimensionMembers(dimCode, selectedKeys);
      message.success(`已删除 ${r.deleted} 个${r.failed.length ? `，失败 ${r.failed.length}` : ""}`);
      setSelectedKeys([]);
      reload();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量删除失败");
    } finally {
      setBatchLoading(false);
    }
  }

  useEffect(() => {
    listDimensions({ page_size: 200 }).then((r) => setDims(r.items)).catch(() => {});
    listDataSources({ page_size: 100 })
      .then((r) => setDataSources(r.items))
      .catch(() => setDataSources([]));
  }, []);

  useEffect(() => {
    if (!dimCode) return;
    setLoading(true);
    listDimensionMembers(dimCode)
      .then((r) => setMembers(r.items))
      .catch((err) => message.error(err instanceof UnisenseApiError ? err.message : "加载成员失败"))
      .finally(() => setLoading(false));
  }, [dimCode]);

  async function reload() {
    if (!dimCode) return;
    setLoading(true);
    listDimensionMembers(dimCode).then((r) => setMembers(r.items)).finally(() => setLoading(false));
  }

  async function handleCreate(values: Record<string, unknown>) {
    if (!dimCode || saving) return;
    setSaving(true);
    try {
      await createDimensionMember({
        dim_code: dimCode,
        member_code: values.member_code ? String(values.member_code) : undefined,
        member_name: String(values.member_name),
        parent_code: values.parent_code ? String(values.parent_code) : null,
        // path 留空，由后端按父级路径自动推测
      });
      message.success("成员已创建");
      setModalOpen(false);
      form.resetFields();
      reload();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    } finally {
      setSaving(false);
    }
  }

  // 拉取预览：根据所选数据源/表/列，调后端获取去重枚举值
  // 源库表/列选项框：选数据源加载全部库；选库加载该库表；选表加载全部列
  async function loadSourceDatabases(sourceId: string) {
    if (!sourceId) {
      setSourceDatabases([]);
      return;
    }
    setSourceDatabasesLoading(true);
    try {
      const r = await listSourceDatabases(sourceId);
      setSourceDatabases(r.databases);
    } catch {
      setSourceDatabases([]);
    } finally {
      setSourceDatabasesLoading(false);
    }
  }

  async function loadSourceTables(sourceId: string, databases?: string[]) {
    if (!sourceId) {
      setSourceTables([]);
      return;
    }
    setSourceTablesLoading(true);
    try {
      const r = await listSourceTables(sourceId, databases);
      setSourceTables(r.tables);
    } catch {
      setSourceTables([]);
      message.warning("列举数据源表失败，可手动输入表名");
    } finally {
      setSourceTablesLoading(false);
    }
  }

  async function loadSourceColumns(sourceId: string, table: string) {
    if (!sourceId || !table) {
      setSourceColumns([]);
      return;
    }
    setSourceColumnsLoading(true);
    try {
      const r = await listSourceColumns(sourceId, table);
      setSourceColumns(r.columns);
    } catch {
      setSourceColumns([]);
      message.warning("列举表列失败，可手动输入列名");
    } finally {
      setSourceColumnsLoading(false);
    }
  }

  async function handlePreview(values: Record<string, unknown>) {
    if (!dimCode) return;
    setAutoLoading(true);
    setPreviewValues([]);
    setPreviewTruncated(false);
    try {
      const r = await previewColumnValues({
        source_id: String(values.source_id),
        table: String(values.table),
        column: String(values.column),
        limit: 200,
      });
      setPreviewValues(r.values);
      setPreviewTruncated(r.truncated);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "拉取枚举值失败");
    } finally {
      setAutoLoading(false);
    }
  }

  // 导入预览值为维度成员（member_code = 枚举值本身，member_name = 枚举值）
  async function handleImportValues() {
    if (!dimCode || previewValues.length === 0) return;
    setImporting(true);
    let ok = 0;
    let failed = 0;
    const total = previewValues.length;
    try {
      for (let i = 0; i < previewValues.length; i++) {
        const v = previewValues[i];
        try {
          await createDimensionMember({
            dim_code: dimCode,
            member_code: v,
            member_name: v,
          });
          ok += 1;
        } catch {
          failed += 1;
        }
        setImportProgress({ ok, failed, done: i + 1, total });
      }
      message.success(`已导入 ${ok} 个维度值${failed > 0 ? `，跳过 ${failed} 个（已存在或失败）` : ""}`);
      setAutoOpen(false);
      autoForm.resetFields();
      setPreviewValues([]);
      reload();
    } finally {
      setImporting(false);
      setImportProgress(null);
    }
  }

  // 新建成员（可选预填父级="添加下级"）：打开时重置表单避免残留旧值
  function openCreateMember(prefillParent?: string) {
    form.resetFields();
    if (prefillParent) form.setFieldsValue({ parent_code: prefillParent });
    setModalOpen(true);
  }

  function openEdit(m: DimensionMember) {
    setEditTarget(m);
    setEditOpen(true);
    editForm.setFieldsValue({
      member_name: m.member_name,
      parent_code: m.parent_code ?? undefined,
      status: m.status,
    });
  }

  async function handleEdit(values: Record<string, unknown>) {
    if (!dimCode || !editTarget) return;
    setEditSaving(true);
    try {
      await updateDimensionMember({
        dim_code: dimCode,
        member_code: editTarget.member_code,
        member_name: values.member_name ? String(values.member_name) : undefined,
        // 空串表示置为根成员（取消父级），后端据此重算 path
        parent_code: values.parent_code ? String(values.parent_code) : "",
        status: values.status ? String(values.status) : undefined,
      });
      message.success("成员已更新");
      setEditOpen(false);
      editForm.resetFields();
      reload();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "更新失败");
    } finally {
      setEditSaving(false);
    }
  }

  async function handleDeleteMember(m: DimensionMember) {
    if (!dimCode) return;
    try {
      await deleteDimensionMember(dimCode, m.member_code);
      message.success("成员已删除");
      reload();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "删除失败");
    }
  }

  async function handlePublishMember(m: DimensionMember) {
    if (!dimCode) return;
    try {
      await publishDimensionMember(dimCode, m.member_code);
      message.success(`成员「${m.member_name}」已发布`);
      reload();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "发布失败");
    }
  }

  async function handlePublishAllMembers() {
    if (!dimCode) return;
    setBatchPublishing(true);
    try {
      const res = await publishAllDimensionMembers(dimCode);
      message.success(`已发布 ${res.published} 个成员${res.skipped ? `（跳过 ${res.skipped} 个非草稿）` : ""}`);
      reload();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量发布失败");
    } finally {
      setBatchPublishing(false);
    }
  }

  async function handleDeprecateMember(m: DimensionMember) {
    if (!dimCode) return;
    try {
      await deprecateDimensionMember(dimCode, m.member_code);
      message.success(`成员「${m.member_name}」已废弃`);
      reload();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "废弃失败");
    }
  }

  // 维度成员「更多」下拉中的危险操作二次确认（废弃/删除）
  function confirmDeprecateMember(m: DimensionMember) {
    Modal.confirm({
      title: `废弃成员「${m.member_name}」？`,
      content: "已废弃成员为终态，不可恢复；存在子成员时无法废弃。",
      okText: "确认",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => handleDeprecateMember(m),
    });
  }

  function confirmDeleteMember(m: DimensionMember) {
    Modal.confirm({
      title: `删除成员「${m.member_name}」？`,
      content: "若存在子成员将级联删除整个子树。",
      okText: "确认",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => handleDeleteMember(m),
    });
  }

  // 成员下拉选项（父级选择框）：展示路径 + 名称，便于识别层级
  function memberOptions(excludeCode?: string) {
    return members
      .filter((m) => m.member_code !== excludeCode)
      .map((m) => ({
        value: m.member_code,
        label: m.path ? `${m.path}（${m.member_name}）` : `${m.member_code} · ${m.member_name}`,
      }));
  }

  return (
    <div>
      {/* 维度值说明：区分「维度的取值」与「系统用户账号」，避免概念混淆 */}
      <div
        style={{
          marginBottom: 12,
          padding: "8px 12px",
          background: "var(--bg-elevated, #fafafa)",
          border: "1px solid var(--line-soft, #eef1f5)",
          borderRadius: 6,
          fontSize: 13,
          color: "var(--text-2)",
        }}
      >
        维度值 = 该维度允许的<b>业务取值集合</b>（如「科室」维度的值：内科 / 外科 / 儿科），
        用于指标按此维度分组/过滤时校验合法性。这里管理的<b>不是系统用户账号</b>，
        而是维度自身的枚举取值，可手动新增或从数据源表列自动导入。
        <br />
        维度值需与指标口径声明的维度保持一致——指标在维度管理绑定后，消费查询即按此维度校验过滤。
      </div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <Select
          placeholder="选择维度"
          style={{ width: 260 }}
          value={dimCode}
          onChange={setDimCode}
          options={dims.map((d) => ({ value: d.dim_code, label: `${d.dim_code} · ${d.name}` }))}
        />
        <Space>
          {can("dimension:create") && (
            <Button icon={<PlusOutlined />} disabled={!dimCode} onClick={() => openCreateMember()}>新增值</Button>
          )}
          {can("dimension:create") && (
            <Button
              icon={<ThunderboltOutlined />}
              disabled={!dimCode}
              loading={batchPublishing}
              onClick={() => handlePublishAllMembers()}
            >
              全部发布
            </Button>
          )}
          {can("dimension:create") && (
            <Button
              icon={<DatabaseOutlined />}
              disabled={!dimCode}
              onClick={() => {
                autoForm.resetFields();
                setPreviewValues([]);
                setPreviewTruncated(false);
                setSourceTables([]);
                setSourceColumns([]);
                setTableKw("");
                setColumnKw("");
                setAutoOpen(true);
              }}
            >
              从表自动获取
            </Button>
          )}
          {can("dimension:create") && (
            <Button
              icon={<DatabaseOutlined />}
              disabled={!dimCode}
              type={isSnapshot ? "primary" : "default"}
              onClick={() => {
                bindForm.setFieldsValue({
                  source_id: currentDim?.source_id ?? undefined,
                  table: currentDim?.source_table ?? undefined,
                  column: currentDim?.source_column ?? undefined,
                  refresh_interval_hours: currentDim?.refresh_interval_hours ?? 24,
                });
                setSourceTables([]);
                setSourceColumns([]);
                setTableKw("");
                setColumnKw("");
                if (currentDim?.source_id) {
                  loadSourceDatabases(currentDim.source_id);
                  const tbl = currentDim.source_table ?? "";
                  const dotIdx = tbl.indexOf(".");
                  const db = dotIdx > 0 ? tbl.slice(0, dotIdx) : undefined;
                  if (db) bindForm.setFieldValue("database", db);
                  if (tbl) {
                    // 有库前缀：仅枚举该库表（快速）；无库前缀：全量枚举兜底
                    loadSourceTables(currentDim.source_id, db ? [db] : undefined);
                  }
                }
                if (currentDim?.source_id && currentDim.source_table) {
                  loadSourceColumns(currentDim.source_id, currentDim.source_table);
                }
                setBindOpen(true);
              }}
            >
              {isSnapshot ? "重新绑定表列" : "绑定表列（引用型）"}
            </Button>
          )}
          {isSnapshot && can("dimension:create") && (
            <>
              <Button
                icon={<ReloadOutlined />}
                loading={refreshingSnapshot}
                onClick={handleRefreshSnapshot}
              >
                刷新快照
              </Button>
              <Button icon={<DatabaseOutlined />} onClick={openSnapshotList}>
                查看快照值
              </Button>
            </>
          )}
        </Space>
      </div>
      {isSnapshot && latestRun && (
        <div
          style={{
            marginBottom: 12,
            padding: "10px 12px",
            background: "var(--bg-elevated, #fafafa)",
            border: "1px solid var(--line-soft, #eef1f5)",
            borderRadius: 6,
            fontSize: 13,
            color: "var(--text-2)",
            lineHeight: 1.7,
          }}
        >
          <Space size={16} wrap>
            <span>
              引用型值来源：<span className="mono">{currentDim?.source_table}.{currentDim?.source_column}</span>
              {" "}(<span className="muted">{currentDim?.source_id}</span>)
            </span>
            <Tag color={latestRun.status === "SUCCESS" ? "success" : latestRun.status === "FAILED" ? "error" : "processing"}>
              {latestRun.status === "SUCCESS" ? "快照正常" : latestRun.status === "FAILED" ? "最近刷新失败" : "刷新中"}
            </Tag>
            <span>值总数 <b>{latestRun.total_count}</b></span>
            <span>新增 <Tag color="green">{latestRun.added_count}</Tag></span>
            <span>消失 <Tag color="red">{latestRun.removed_count}</Tag></span>
            <span>空值率 <Tag color={latestRun.null_rate != null && latestRun.null_rate > 0.05 ? "orange" : "default"}>
              {latestRun.null_rate != null ? `${(latestRun.null_rate * 100).toFixed(2)}%` : "—"}
            </Tag></span>
            <span className="muted">最近刷新：{latestRun.snapshot_at ? formatCnTime(latestRun.snapshot_at) : "—"}</span>
          </Space>
          {(latestRun.added_sample?.length || latestRun.removed_sample?.length) ? (
            <div style={{ marginTop: 4 }}>
              {latestRun.added_sample?.length ? (
                <span>
                  新增样本：{latestRun.added_sample.map((v) => <Tag key={v} className="mono" style={{ marginLeft: 4 }}>{v}</Tag>)}
                </span>
              ) : null}
              {latestRun.removed_sample?.length ? (
                <span style={{ marginLeft: 8 }}>
                  消失样本：{latestRun.removed_sample.map((v) => <Tag key={v} className="mono" style={{ marginLeft: 4 }} color="red">{v}</Tag>)}
                </span>
              ) : null}
            </div>
          ) : null}
        </div>
      )}
      <div style={{ marginBottom: 8, display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <span className="muted" style={{ fontSize: 12 }}>
          {selectedKeys.length > 0 ? `已选 ${selectedKeys.length} 个成员` : "勾选成员后可批量操作"}
        </span>
        {can("dimension:edit") && (
          <Button
            size="small"
            disabled={selectedKeys.length === 0 || batchLoading}
            loading={batchLoading}
            onClick={handleBatchPublish}
          >
            批量发布
          </Button>
        )}
        {can("dimension:edit") && (
          <Popconfirm
            title={`废弃选中的 ${selectedKeys.length} 个成员？`}
            description="存在子成员/被指标绑定默认值的成员将失败并跳过"
            okText="批量废弃"
            okButtonProps={{ danger: true }}
            trigger="click"
            disabled={selectedKeys.length === 0 || batchLoading}
            onConfirm={handleBatchDeprecate}
          >
            <Button size="small" danger disabled={selectedKeys.length === 0 || batchLoading}>
              批量废弃
            </Button>
          </Popconfirm>
        )}
        {can("dimension:edit") && (
          <Popconfirm
            title={`删除选中的 ${selectedKeys.length} 个成员？`}
            description="级联删除整棵子树；被指标绑定默认值的成员将失败"
            okText="批量删除"
            okButtonProps={{ danger: true }}
            trigger="click"
            disabled={selectedKeys.length === 0 || batchLoading}
            onConfirm={handleBatchDelete}
          >
            <Button size="small" danger icon={<DeleteOutlined />} disabled={selectedKeys.length === 0 || batchLoading}>
              批量删除
            </Button>
          </Popconfirm>
        )}
      </div>
      <Table
        dataSource={buildMemberTree(members)}
        rowKey="member_code"
        loading={loading}
        size="small"
        pagination={false}
        locale={{ emptyText: "请选择维度查看成员" }}
        rowSelection={can("dimension:edit") ? {
          selectedRowKeys: selectedKeys,
          onChange: (keys: Key[]) => setSelectedKeys(keys.map(String)),
          preserveSelectedRowKeys: true,
        } : undefined}
        columns={[
          { title: "成员编码", dataIndex: "member_code", key: "member_code", render: (v: string) => <span className="mono">{v}</span> },
          { title: "名称", dataIndex: "member_name", key: "member_name" },
          { title: "父级", dataIndex: "parent_code", key: "parent_code", render: (v: string | null) => v ?? <span className="muted">—</span> },
          { title: "路径", dataIndex: "path", key: "path", render: (v: string | null) => v && <span className="mono">{v}</span> },
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
            width: 200,
            render: (_: unknown, m: DimensionMember) => {
              const menuItems: any[] = [];
              if (can("dimension:create")) {
                menuItems.push({ key: "add", icon: <PlusOutlined />, label: "添加下级" });
              }
              if (can("dimension:edit") && m.status !== "DEPRECATED") {
                menuItems.push({ type: "divider" });
                menuItems.push({ key: "deprecate", icon: <DeleteOutlined />, label: "废弃", danger: true });
              }
              if (can("dimension:edit")) {
                menuItems.push({ key: "delete", icon: <DeleteOutlined />, label: "删除", danger: true });
              }
              return (
                <Space size={8} wrap>
                  {can("dimension:edit") && m.status === "DRAFT" && (
                    <Button size="small" type="primary" onClick={() => handlePublishMember(m)}>发布</Button>
                  )}
                  {can("dimension:edit") &&
                    (m.status === "DEPRECATED" ? (
                      <Tooltip title="已废弃成员为终态，不可编辑">
                        <Button size="small" icon={<EditOutlined />} disabled>编辑</Button>
                      </Tooltip>
                    ) : (
                      <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(m)}>编辑</Button>
                    ))}
                  <Dropdown
                    trigger={["click"]}
                    menu={{
                      items: menuItems,
                      onClick: ({ key }) => {
                        if (key === "add") openCreateMember(m.member_code);
                        else if (key === "deprecate") confirmDeprecateMember(m);
                        else if (key === "delete") confirmDeleteMember(m);
                      },
                    }}
                  >
                    <Button size="small">
                      更多 <DownOutlined />
                    </Button>
                  </Dropdown>
                </Space>
              );
            },
          },
        ]}
      />

      <Modal title="新增维度成员" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="创建" confirmLoading={saving}>
        <Form form={form} layout="vertical" scrollToFirstError onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="member_code" label="成员编码" extra={<span className="mono" style={{ color: "#0E7C86" }}>留空则由系统自动生成</span>}>
            <Input className="mono" placeholder="留空自动生成" />
          </Form.Item>
          <Form.Item name="member_name" label="成员名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="parent_code" label="父级编码" extra={<span className="muted" style={{ fontSize: 12 }}>留空则为根成员</span>}>
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择父级成员（可留空作为根）"
              notFoundContent={members.length === 0 ? "当前维度暂无成员，该成员将作为根" : "无匹配成员"}
              options={memberOptions()}
            />
          </Form.Item>
          <PathPreview form={form} members={members} />
        </Form>
      </Modal>

      {/* 从表自动获取枚举值：选数据源→表→列，拉取去重值预览后批量导入为维度值 */}
      <Modal
        title={`从表自动获取维度值 → ${dimCode ?? ""}`}
        open={autoOpen}
        onCancel={() => {
          setAutoOpen(false);
          autoForm.resetFields();
          setPreviewValues([]);
          setSourceTables([]);
          setSourceColumns([]);
        }}
        width={640}
        footer={null}
      >
        <Form
          form={autoForm}
          layout="vertical" scrollToFirstError
          onFinish={handlePreview}
          style={{ marginTop: 8 }}
          initialValues={{ limit: 200 }}
        >
          <Form.Item
            label="数据源"
            name="source_id"
            rules={[{ required: true, message: "请选择数据源" }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择数据源（须已注册）"
              options={dataSources.map((s) => ({
                value: s.source_id,
                label: `${s.name}（${s.source_id}）`,
              }))}
              onChange={(v) => {
                autoForm.setFieldValue("database", undefined);
                autoForm.setFieldValue("table", undefined);
                autoForm.setFieldValue("column", undefined);
                setSourceDatabases([]);
                setSourceTables([]);
                setSourceColumns([]);
                if (v) loadSourceDatabases(String(v));
              }}
            />
          </Form.Item>
          <Form.Item
            label="目标库"
            name="database"
            extra={<span className="muted" style={{ fontSize: 12 }}>先选库可快速枚举该库表；清空后可手动输入库.表</span>}
          >
            <Select
              showSearch
              allowClear
              optionFilterProp="label"
              loading={sourceDatabasesLoading}
              placeholder="选择目标库（可跳过，直接输入库.表）"
              options={sourceDatabases.map((d) => ({ value: d, label: d }))}
              notFoundContent={sourceDatabasesLoading ? "正在加载库…" : "无可选库，可直接输入库.表"}
              onChange={(v) => {
                autoForm.setFieldValue("table", undefined);
                autoForm.setFieldValue("column", undefined);
                setSourceTables([]);
                setSourceColumns([]);
                if (v) loadSourceTables(String(autoForm.getFieldValue("source_id")), [String(v)]);
              }}
            />
          </Form.Item>
          <Form.Item
            label="表名"
            name="table"
            extra={<span className="muted" style={{ fontSize: 12 }}>先选目标库快速枚举；也可直接输入库.表（如 dwd.telemedicine）</span>}
            rules={[
              { required: true, message: "请选择表名" },
              { pattern: /^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*$/, message: "表名不合法" },
            ]}
          >
            <Select
              showSearch
              allowClear
              optionFilterProp="label"
              loading={sourceTablesLoading}
              placeholder="选择表，或输入库.表"
              options={withManualOption(
                tableKw,
                (() => {
                  const kw = tableKw.trim().toLowerCase();
                  const all = sourceTables.map((t) => ({ value: t.name, label: t.name }));
                  return kw ? all.filter((o) => o.label.toLowerCase().includes(kw)) : all;
                })(),
              )}
              optionRender={manualOptionRender}
              notFoundContent={sourceTablesLoading ? "正在加载表…" : "无可选表，可直接输入库.表名"}
              filterOption={false}
              onSearch={setTableKw}
              onChange={(v) => {
                autoForm.setFieldValue("column", undefined);
                setSourceColumns([]);
                if (v) loadSourceColumns(String(autoForm.getFieldValue("source_id")), String(v));
              }}
            />
          </Form.Item>
          <Form.Item
            label="列名"
            name="column"
            rules={[
              { required: true, message: "请选择列名" },
              { pattern: /^[a-zA-Z_][a-zA-Z0-9_]*$/, message: "列名不合法" },
            ]}
          >
            <Select
              showSearch
              allowClear
              optionFilterProp="label"
              loading={sourceColumnsLoading}
              placeholder="选择列，或输入列名"
              options={withManualOption(
                columnKw,
                (() => {
                  const kw = columnKw.trim().toLowerCase();
                  const all = sourceColumns.map((c) => ({
                    value: c.name,
                    label: `${c.name}${c.data_type ? ` (${c.data_type})` : ""}`,
                  }));
                  return kw ? all.filter((o) => o.label.toLowerCase().includes(kw)) : all;
                })(),
              )}
              optionRender={manualOptionRender}
              notFoundContent={sourceColumnsLoading ? "正在加载列…" : "无可选列，可直接输入列名"}
              filterOption={false}
              onSearch={setColumnKw}
            />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={autoLoading} icon={<DatabaseOutlined />}>
                拉取去重值
              </Button>
              <span className="muted" style={{ fontSize: 12 }}>
                将执行 <code className="mono">SELECT DISTINCT</code> 读取该列全部取值
              </span>
            </Space>
          </Form.Item>
        </Form>

        {previewValues.length > 0 && (
          <div>
            <Divider style={{ margin: "12px 0" }} />
            <div style={{ marginBottom: 8 }}>
              <span className="muted">已获取 {previewValues.length} 个去重值</span>
              {previewTruncated && (
                <Tag color="orange" style={{ marginLeft: 8 }}>结果已达上限，可能不完整</Tag>
              )}
            </div>
            <div
              style={{
                maxHeight: 200,
                overflow: "auto",
                border: "1px solid var(--line, #e3e7ee)",
                borderRadius: 6,
                padding: 8,
                marginBottom: 12,
              }}
            >
              {previewValues.map((v) => (
                <Tag key={v} className="mono" style={{ marginBottom: 4 }}>
                  {v}
                </Tag>
              ))}
            </div>
            {can("dimension:create") && (
              <>
                <Button
                  type="primary"
                  loading={importing}
                  onClick={handleImportValues}
                  icon={<PlusOutlined />}
                >
                  导入全部为维度值
                </Button>
                {importProgress && (
                  <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>
                    {`正在导入 ${importProgress.done}/${importProgress.total} · 成功 ${importProgress.ok} · 跳过 ${importProgress.failed}`}
                  </span>
                )}
              </>
            )}
          </div>
        )}
      </Modal>

      {/* 绑定引用型值来源：值集合 = 源表列快照（大基数维度，不再逐值维护 member 表） */}
      <Modal
        title={`绑定引用型值来源 → ${dimCode ?? ""}`}
        open={bindOpen}
        onCancel={() => {
          setBindOpen(false);
          bindForm.resetFields();
          setSourceTables([]);
          setSourceColumns([]);
        }}
        onOk={() => bindForm.submit()}
        okText="绑定"
        confirmLoading={bindSaving}
        width={560}
      >
        <Form form={bindForm} layout="vertical" scrollToFirstError onFinish={handleBindReference} style={{ marginTop: 8 }}>
          <div
            style={{
              marginBottom: 12,
              padding: "8px 12px",
              background: "var(--bg-elevated, #fafafa)",
              border: "1px solid var(--line-soft, #eef1f5)",
              borderRadius: 6,
              fontSize: 12,
              color: "var(--text-2)",
              lineHeight: 1.6,
            }}
          >
            引用型 = 维度取值来自<b>维度表列</b>（如客户表 customer_id），值集合为
            <code className="mono">SELECT DISTINCT</code> 列值快照，<b>无需逐个维护成员</b>。
            适合客户/商品/医生等<b>大基数维度</b>；刷新快照可自动检测新增/消失值。
          </div>
          <Form.Item name="source_id" label="数据源" rules={[{ required: true, message: "请选择数据源" }]}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择数据源（须已注册）"
              options={dataSources.map((s) => ({ value: s.source_id, label: `${s.name}（${s.source_id}）` }))}
              onChange={(v) => {
                bindForm.setFieldValue("database", undefined);
                bindForm.setFieldValue("table", undefined);
                bindForm.setFieldValue("column", undefined);
                setSourceDatabases([]);
                setSourceTables([]);
                setSourceColumns([]);
                if (v) loadSourceDatabases(String(v));
              }}
            />
          </Form.Item>
          <Form.Item
            name="database"
            label="目标库"
            extra={<span className="muted" style={{ fontSize: 12 }}>先选库可快速枚举该库表；清空后可手动输入库.表</span>}
          >
            <Select
              showSearch
              allowClear
              optionFilterProp="label"
              loading={sourceDatabasesLoading}
              placeholder="选择目标库（可跳过，直接输入库.表）"
              options={sourceDatabases.map((d) => ({ value: d, label: d }))}
              notFoundContent={sourceDatabasesLoading ? "正在加载库…" : "无可选库，可直接输入库.表"}
              onChange={(v) => {
                bindForm.setFieldValue("table", undefined);
                bindForm.setFieldValue("column", undefined);
                setSourceTables([]);
                setSourceColumns([]);
                if (v) loadSourceTables(String(bindForm.getFieldValue("source_id")), [String(v)]);
              }}
            />
          </Form.Item>
          <Form.Item
            name="table"
            label="表名"
            extra={<span className="muted" style={{ fontSize: 12 }}>先选目标库快速枚举；也可直接输入库.表（如 dwd.dim_customer）</span>}
            rules={[
              { required: true, message: "请选择表名" },
              { pattern: /^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*$/, message: "表名不合法" },
            ]}
          >
            <Select
              showSearch
              allowClear
              optionFilterProp="label"
              loading={sourceTablesLoading}
              placeholder="选择表，或输入库.表"
              options={withManualOption(
                tableKw,
                (() => {
                  const kw = tableKw.trim().toLowerCase();
                  const all = sourceTables.map((t) => ({ value: t.name, label: t.name }));
                  return kw ? all.filter((o) => o.label.toLowerCase().includes(kw)) : all;
                })(),
              )}
              optionRender={manualOptionRender}
              notFoundContent={sourceTablesLoading ? "正在加载表…" : "无可选表，可直接输入库.表名"}
              filterOption={false}
              onSearch={setTableKw}
              onChange={(v) => {
                bindForm.setFieldValue("column", undefined);
                setSourceColumns([]);
                if (v) loadSourceColumns(String(bindForm.getFieldValue("source_id")), String(v));
              }}
            />
          </Form.Item>
          <Form.Item
            name="column"
            label="列名"
            rules={[
              { required: true, message: "请选择列名" },
              { pattern: /^[a-zA-Z_][a-zA-Z0-9_]*$/, message: "列名不合法" },
            ]}
          >
            <Select
              showSearch
              allowClear
              optionFilterProp="label"
              loading={sourceColumnsLoading}
              placeholder="选择列，或输入列名"
              options={withManualOption(
                columnKw,
                (() => {
                  const kw = columnKw.trim().toLowerCase();
                  const all = sourceColumns.map((c) => ({
                    value: c.name,
                    label: `${c.name}${c.data_type ? ` (${c.data_type})` : ""}`,
                  }));
                  return kw ? all.filter((o) => o.label.toLowerCase().includes(kw)) : all;
                })(),
              )}
              optionRender={manualOptionRender}
              notFoundContent={sourceColumnsLoading ? "正在加载列…" : "无可选列，可直接输入列名"}
              filterOption={false}
              onSearch={setColumnKw}
            />
          </Form.Item>
          <Form.Item name="refresh_interval_hours" label="快照刷新间隔（小时）" extra={<span className="muted" style={{ fontSize: 12 }}>系统每 30 分钟扫描到期维度自动刷新；默认 24 小时</span>}>
            <InputNumber min={1} max={2160} style={{ width: 160 }} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 引用型维度快照值列表 */}
      <Modal
        title={`快照值 → ${dimCode ?? ""}（${snapshots.filter((s) => s.status === "ACTIVE").length} 个当前值）`}
        open={snapshotOpen}
        onCancel={() => setSnapshotOpen(false)}
        footer={null}
        width={560}
      >
        <Table
          dataSource={snapshots}
          rowKey="id"
          size="small"
          loading={snapshotLoading}
          pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 条` }}
          columns={[
            { title: "值", dataIndex: "value", render: (v: string) => <span className="mono">{v}</span> },
            { title: "批次", dataIndex: "snapshot_at", width: 170, render: (v: string) => formatCnTime(v) },
            {
              title: "状态",
              dataIndex: "status",
              width: 100,
              render: (s: string) => (s === "ACTIVE" ? <Tag color="success">当前批</Tag> : <Tag color="error">已消失</Tag>),
            },
          ]}
        />
      </Modal>

      <Modal
        title={editTarget ? `编辑成员：${editTarget.member_code}` : "编辑成员"}
        open={editOpen}
        onCancel={() => {
          setEditOpen(false);
          editForm.resetFields();
        }}
        onOk={() => editForm.submit()}
        okText="保存"
        confirmLoading={editSaving}
      >
        <Form form={editForm} layout="vertical" scrollToFirstError onFinish={handleEdit} style={{ marginTop: 8 }}>
          <Form.Item name="member_name" label="成员名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item
            name="parent_code"
            label="父级编码"
            extra={
              <span className="muted" style={{ fontSize: 12 }}>
                {editTarget?.status === "PUBLISHED"
                  ? "已发布成员不可变更父级（层级为下游权威来源，须先废弃重建）"
                  : "清空则置为根成员，层级路径自动重算"}
              </span>
            }
          >
            <Select
              allowClear
              showSearch
              disabled={editTarget?.status === "PUBLISHED"}
              optionFilterProp="label"
              placeholder="选择父级成员"
              notFoundContent={members.length === 0 ? "当前维度暂无成员" : "无匹配成员"}
              options={memberOptions(editTarget?.member_code)}
            />
          </Form.Item>
          <Form.Item name="status" label="状态">
            <Select
              options={[
                { value: "DRAFT", label: "草稿" },
                { value: "PUBLISHED", label: "已发布" },
                { value: "DEPRECATED", label: "已废弃" },
              ]}
            />
          </Form.Item>
          <PathPreview form={editForm} members={members} />
        </Form>
      </Modal>
    </div>
  );
}

function MappingsTab() {
  const { can } = usePermission();
  const [items, setItems] = useState<DimensionMapping[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();
  // 映射表每页条数（持久化，用户可自定义）
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.mappings.pageSize", 20);
  // 服务端分页：页码/总数/防竞态（对齐维度主表模式，避免超过 pageSize 后翻不到）
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const loadSeq = useRef(0);
  // 维度下拉候选（源/目标维度选项框）
  const [dims, setDims] = useState<Dimension[]>([]);
  // F6（审查修复）：维度下拉此前仅拉前 200 条 + 客户端过滤（optionFilterProp）——
  // 第 201 个维度永远无法建立映射。改服务端搜索（输入即查）。
  const loadDims = (kw?: string) => {
    listDimensions({ page_size: 200, keyword: kw || undefined })
      .then((r) => setDims(r.items))
      .catch(() => {});
  };
  // 编辑态：复用新建布局，打开时预填当前映射值
  const [editTarget, setEditTarget] = useState<DimensionMapping | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editSaving, setEditSaving] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editForm] = Form.useForm();

  // 值级映射：source_value → target_value 逐值对应（供翻译服务消费）
  const [valueOpen, setValueOpen] = useState(false);
  const [valueMapping, setValueMapping] = useState<DimensionMapping | null>(null);
  const [valueItems, setValueItems] = useState<DimensionMappingValue[]>([]);
  const [valueTotal, setValueTotal] = useState(0);
  const [valuePage, setValuePage] = useState(1);
  const [valueLoading, setValueLoading] = useState(false);
  const [mvForm] = Form.useForm();
  const [mvSaving, setMvSaving] = useState(false);
  const [coverage, setCoverage] = useState<MappingCoverage | null>(null);
  const [coverageLoading, setCoverageLoading] = useState(false);
  // 翻译预览
  const [translateText, setTranslateText] = useState("");
  const [translateResults, setTranslateResults] = useState<TranslateResult[]>([]);
  const [translating, setTranslating] = useState(false);

  async function loadValueItems(mappingId: number, p = valuePage) {
    setValueLoading(true);
    try {
      const r = await listDimensionMappingValues(mappingId, p, 50);
      setValueItems(r.items);
      setValueTotal(r.total);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载值级映射失败");
    } finally {
      setValueLoading(false);
    }
  }

  async function loadCoverage(mappingId: number) {
    setCoverageLoading(true);
    try {
      setCoverage(await getMappingCoverage(mappingId));
    } catch {
      setCoverage(null);
    } finally {
      setCoverageLoading(false);
    }
  }

  async function openValueMapping(m: DimensionMapping) {
    setValueMapping(m);
    setValueItems([]);
    setValueTotal(0);
    setValuePage(1);
    setCoverage(null);
    setTranslateText("");
    setTranslateResults([]);
    setValueOpen(true);
    await loadValueItems(m.id, 1);
    await loadCoverage(m.id);
  }

  async function handleCreateMappingValue(values: Record<string, unknown>) {
    if (!valueMapping || mvSaving) return;
    setMvSaving(true);
    try {
      await createDimensionMappingValue(valueMapping.id, {
        source_value: String(values.source_value),
        target_value: String(values.target_value),
      });
      message.success("值级映射已添加");
      mvForm.resetFields();
      await loadValueItems(valueMapping.id);
      await loadCoverage(valueMapping.id);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "添加失败");
    } finally {
      setMvSaving(false);
    }
  }

  async function handleDeleteMappingValue(v: DimensionMappingValue) {
    if (!valueMapping) return;
    try {
      await deleteDimensionMappingValue(v.id);
      message.success("值级映射已删除");
      await loadValueItems(valueMapping.id);
      await loadCoverage(valueMapping.id);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "删除失败");
    }
  }

  async function handleTranslate() {
    if (!valueMapping || !translateText.trim() || translating) return;
    setTranslating(true);
    try {
      const values = translateText.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean).slice(0, 100);
      const r = await translateDimensionValues(valueMapping.source_dim_code, valueMapping.target_dim_code, values);
      setTranslateResults(r.results);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "翻译失败");
    } finally {
      setTranslating(false);
    }
  }

  async function load() {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      const res = await listDimensionMappings(undefined, page, pageSize);
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
  }, [page, pageSize]);

  useEffect(() => {
    loadDims();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleCreate(values: Record<string, unknown>) {
    if (saving) return;
    setSaving(true);
    try {
      await createDimensionMapping({
        source_dim_code: String(values.source_dim_code),
        target_dim_code: String(values.target_dim_code),
        mapping_type: String(values.mapping_type ?? "EQUIVALENT"),
        expression: values.expression ? String(values.expression) : null,
      });
      message.success("映射已创建");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    } finally {
      setSaving(false);
    }
  }

  function openEditMapping(m: DimensionMapping) {
    setEditTarget(m);
    setEditOpen(true);
    editForm.setFieldsValue({
      mapping_type: m.mapping_type,
      expression: m.expression ?? undefined,
    });
  }

  async function handleEditMapping(values: Record<string, unknown>) {
    if (!editTarget) return;
    setEditSaving(true);
    try {
      await updateDimensionMapping(editTarget.id, {
        mapping_type: values.mapping_type ? String(values.mapping_type) : undefined,
        expression: values.expression ? String(values.expression) : null,
      });
      message.success("映射已更新");
      setEditOpen(false);
      editForm.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "更新失败");
    } finally {
      setEditSaving(false);
    }
  }

  async function handleDeleteMapping(m: DimensionMapping) {
    try {
      await deleteDimensionMapping(m.id);
      message.success("映射已删除");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "删除失败");
    }
  }

  // 维度映射「更多」下拉中的删除二次确认
  function confirmDeleteMapping(m: DimensionMapping) {
    Modal.confirm({
      title: `删除该映射（${m.source_dim_code} ↔ ${m.target_dim_code}）？`,
      content: "删除后源/目标维度取值不再互相翻译。",
      okText: "确认",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => handleDeleteMapping(m),
    });
  }

  const columns = [
    { title: "源维度", dataIndex: "source_dim_code", key: "source", render: (v: string) => <span className="mono">{v}</span> },
    { title: "目标维度", dataIndex: "target_dim_code", key: "target", render: (v: string) => <span className="mono">{v}</span> },
    { title: "映射类型", dataIndex: "mapping_type", key: "type", width: 130, render: (v: string) => <Tag color={v === "EQUIVALENT" ? "success" : "warning"}>{v === "EQUIVALENT" ? "等价" : "部分"}</Tag> },
    { title: "表达式", dataIndex: "expression", key: "expr", render: (v: string | null) => v ? <span className="mono">{v}</span> : <span className="muted">—</span> },
    {
      title: "操作",
      key: "actions",
      width: 200,
      render: (_: unknown, m: DimensionMapping) => (
        <Space size={8} wrap>
          {can("dimension:mapping") && (
            <Button size="small" type="link" icon={<DatabaseOutlined />} onClick={() => openValueMapping(m)}>值级映射</Button>
          )}
          {can("dimension:mapping") && (
            <Button size="small" type="link" icon={<EditOutlined />} onClick={() => openEditMapping(m)}>编辑</Button>
          )}
          {can("dimension:mapping") && (
            <Dropdown
              trigger={["click"]}
              menu={{
                items: [{ key: "delete", icon: <DeleteOutlined />, label: "删除", danger: true }],
                onClick: ({ key }) => {
                  if (key === "delete") confirmDeleteMapping(m);
                },
              }}
            >
              <Button size="small">
                更多 <DownOutlined />
              </Button>
            </Dropdown>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      {/* 维度映射说明 + 示例引导：解释映射解决什么问题、如何用 */}
      <div
        style={{
          marginBottom: 12,
          padding: "10px 12px",
          background: "var(--bg-elevated, #fafafa)",
          border: "1px solid var(--line-soft, #eef1f5)",
          borderRadius: 6,
          fontSize: 13,
          color: "var(--text-2)",
          lineHeight: 1.7,
        }}
      >
        <b>维度映射</b>表达不同系统间<b>维度取值的对应关系</b>——同一业务概念在不同系统里编码不同，
        指标跨系统对账时需要知道它们等价。
        <br />
        <span className="muted">
          示例：业务库维度 <code className="mono">dept_code</code>（取值 dept_01 / dept_02） ↔ 数仓维度{" "}
          <code className="mono">科室</code>（取值 内科 / 外科）。
          创建一条 <Tag color="success">等价</Tag> 映射（source=dept_code, target=科室），即可让指标在
          「科室」维度上正确对账。
        </span>
      </div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "flex-end" }}>
        {can("dimension:mapping") && (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新建映射</Button>
        )}
      </div>
      <Table
        dataSource={items}
        columns={columns}
        rowKey="id"
        loading={loading}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          onShowSizeChange: (p: number, ps: number) => {
            setPage(1);
            onShowSizeChange(p, ps);
          },
          onChange: (p: number) => setPage(p),
          pageSizeOptions: [10, 20, 50, 100],
          showTotal: (t: number) => `共 ${t} 条`,
        }}
        locale={{ emptyText: "暂无维度映射" }}
      />

      <Modal title="新建维度映射" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="创建" confirmLoading={saving}>
        <Form form={form} layout="vertical" scrollToFirstError onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="source_dim_code" label="源维度" rules={[{ required: true }]}>
            <Select
              showSearch
              filterOption={false}
              onSearch={(kw) => loadDims(kw || undefined)}
              placeholder="选择源维度（输入编码/名称搜索）"
              notFoundContent={dims.length === 0 ? "暂无维度，请先创建" : "无匹配维度"}
              options={dims.map((d) => ({ value: d.dim_code, label: `${d.dim_code} · ${d.name}` }))}
            />
          </Form.Item>
          <Form.Item name="target_dim_code" label="目标维度" rules={[{ required: true }]}>
            <Select
              showSearch
              filterOption={false}
              onSearch={(kw) => loadDims(kw || undefined)}
              placeholder="选择目标维度（输入编码/名称搜索）"
              notFoundContent={dims.length === 0 ? "暂无维度，请先创建" : "无匹配维度"}
              options={dims.map((d) => ({ value: d.dim_code, label: `${d.dim_code} · ${d.name}` }))}
            />
          </Form.Item>
          <Form.Item
            name="mapping_type"
            label="映射类型"
            extra={<span className="muted" style={{ fontSize: 12 }}>等价 = 源/目标取值一一对应（如 app↔APP）；部分 = 存在一对多或需表达式换算</span>}
          >
            <Select options={[{ value: "EQUIVALENT", label: "等价" }, { value: "PARTIAL", label: "部分" }]} />
          </Form.Item>
          <Form.Item
            name="expression"
            label="映射表达式"
            extra={<span className="muted" style={{ fontSize: 12 }}>支持键值对（dept_01=neike）或 SQL 片段（CASE WHEN ...）</span>}
          >
            <Input.TextArea rows={2} className="mono" placeholder="如 dept_01=neike;dept_02=waike" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={editTarget ? `编辑维度映射：${editTarget.source_dim_code} → ${editTarget.target_dim_code}` : "编辑维度映射"}
        open={editOpen}
        onCancel={() => {
          setEditOpen(false);
          editForm.resetFields();
        }}
        onOk={() => editForm.submit()}
        okText="保存"
        confirmLoading={editSaving}
      >
        <Form form={editForm} layout="vertical" scrollToFirstError onFinish={handleEditMapping} style={{ marginTop: 8 }}>
          <Form.Item
            name="mapping_type"
            label="映射类型"
            extra={<span className="muted" style={{ fontSize: 12 }}>等价 = 源/目标取值一一对应（如 app↔APP）；部分 = 存在一对多或需表达式换算</span>}
          >
            <Select options={[{ value: "EQUIVALENT", label: "等价" }, { value: "PARTIAL", label: "部分" }]} />
          </Form.Item>
          <Form.Item
            name="expression"
            label="映射表达式"
            extra={<span className="muted" style={{ fontSize: 12 }}>支持键值对（dept_01=neike）或 SQL 片段（CASE WHEN ...）</span>}
          >
            <Input.TextArea rows={2} className="mono" placeholder="如 dept_01=neike;dept_02=waike" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 值级映射：source_value → target_value 逐值对应 + 覆盖率 + 翻译预览 */}
      <Modal
        title={valueMapping ? `值级映射：${valueMapping.source_dim_code} → ${valueMapping.target_dim_code}` : "值级映射"}
        open={valueOpen}
        onCancel={() => setValueOpen(false)}
        footer={null}
        width={760}
      >
        <div
          style={{
            marginBottom: 12,
            padding: "8px 12px",
            background: "var(--bg-elevated, #fafafa)",
            border: "1px solid var(--line-soft, #eef1f5)",
            borderRadius: 6,
            fontSize: 12,
            color: "var(--text-2)",
            lineHeight: 1.6,
          }}
        >
          值级映射是<b>机器可消费</b>的逐值对应（如 <code className="mono">dept_01 → 内科</code>），
          供跨系统对账/翻译服务调用；未配置逐值映射的源值由「表达式」仅作人工参考（原样返回）。
        </div>
        <Space size={16} wrap style={{ marginBottom: 12 }}>
          <span>覆盖率：</span>
          {coverageLoading ? <span className="muted">计算中…</span> : coverage ? (
            <>
              <span>
                已映射 <Tag color={coverage.covered > 0 ? "success" : "default"}>{coverage.covered}</Tag> /{" "}
                <b>{coverage.total}</b> 个源值
              </span>
              <span>
                未映射{" "}
                <Tag color={coverage.uncovered.length > 0 ? "warning" : "success"}>
                  {coverage.uncovered.length}{coverage.uncovered.length >= 50 ? "+" : ""}
                </Tag>
              </span>
              {coverage.uncovered.length > 0 && (
                <Tooltip title={coverage.uncovered.slice(0, 30).join("、")}>
                  <span className="muted" style={{ cursor: "help" }}>未映射值清单</span>
                </Tooltip>
              )}
            </>
          ) : null}
        </Space>
        <Form form={mvForm} layout="inline" onFinish={handleCreateMappingValue} style={{ marginBottom: 12 }}>
          <Form.Item name="source_value" label="源值" rules={[{ required: true, message: "源值必填" }]}>
            <Input className="mono" placeholder="源值" style={{ width: 200 }} />
          </Form.Item>
          <Form.Item name="target_value" label="目标值" rules={[{ required: true, message: "目标值必填" }]}>
            <Input className="mono" placeholder="目标值" style={{ width: 200 }} />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={mvSaving} icon={<PlusOutlined />}>添加</Button>
          </Form.Item>
        </Form>
        <Table
          dataSource={valueItems}
          rowKey="id"
          size="small"
          loading={valueLoading}
          pagination={{
            current: valuePage,
            pageSize: 50,
            total: valueTotal,
            onChange: (p: number) => {
              setValuePage(p);
              if (valueMapping) loadValueItems(valueMapping.id, p);
            },
            showTotal: (t: number) => `共 ${t} 条`,
          }}
          columns={[
            { title: "源值", dataIndex: "source_value", render: (v: string) => <span className="mono">{v}</span> },
            { title: "目标值", dataIndex: "target_value", render: (v: string) => <span className="mono">{v}</span> },
            {
              title: "操作",
              key: "actions",
              width: 90,
              render: (_: unknown, v: DimensionMappingValue) => (
                <Popconfirm title="删除该值级映射？" okText="删除" okButtonProps={{ danger: true }} onConfirm={() => handleDeleteMappingValue(v)}>
                  <Button size="small" danger icon={<DeleteOutlined />}>删除</Button>
                </Popconfirm>
              ),
            },
          ]}
        />
        <Divider style={{ margin: "12px 0" }} />
        <div style={{ marginBottom: 8 }}>
          <span style={{ fontSize: 13, fontWeight: 500 }}>翻译预览</span>
          <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>
            输入源值（逗号/空格分隔，最多 100 个）→ 查看翻译结果
          </span>
        </div>
        <Space.Compact style={{ width: "100%", marginBottom: 8 }}>
          <Input
            className="mono"
            placeholder="如 dept_01, dept_02"
            value={translateText}
            onChange={(e) => setTranslateText(e.target.value)}
          />
          <Button type="primary" loading={translating} onClick={handleTranslate}>翻译</Button>
        </Space.Compact>
        {translateResults.length > 0 && (
          <div>
            {translateResults.map((r) => (
              <Tag
                key={r.source_value}
                color={r.covered ? "success" : "default"}
                className="mono"
                style={{ marginBottom: 4 }}
              >
                {r.source_value} → {r.target_value ?? "未配置映射"}{r.covered ? "" : "（表达式参考）"}
              </Tag>
            ))}
          </div>
        )}
      </Modal>
    </div>
  );
}

function ReconciliationsTab() {
  const { can } = usePermission();
  const navigate = useNavigate();
  const [items, setItems] = useState<Reconciliation[]>([]);
  const [metrics, setMetrics] = useState<MetricResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();
  // 复核需治理角色（对齐后端 _GOV_DEPS = domain_admin/platform_admin）；
  // 提交对账用 dimension:reconcile（metric_owner 等可提交，但不能复核他人对账）
  const [isGov, setIsGov] = useState(false);
  // 对账表每页条数（持久化，用户可自定义）
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.reconciliations.pageSize", 20);
  // 服务端分页：页码/总数/防竞态（对齐维度主表模式，避免超过 pageSize 后翻不到）
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const loadSeq = useRef(0);

  async function load() {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      const res = await listReconciliations(undefined, page, pageSize);
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
  }, [page, pageSize]);

  useEffect(() => {
    listMetrics({ page_size: 100 }).then((r) => setMetrics(r.items)).catch(() => {});
    fetchCurrentUser()
      .then((u) => setIsGov(u.role === "domain_admin" || u.role === "platform_admin"))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleCreate(values: Record<string, unknown>) {
    if (saving) return;
    setSaving(true);
    try {
      await submitReconciliation({
        metric_id: Number(values.metric_id),
        expected_expr: String(values.expected_expr),
        actual_expr: String(values.actual_expr),
        diff_summary: values.diff_summary ? String(values.diff_summary) : null,
      });
      message.success("对账已提交");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "提交失败");
    } finally {
      setSaving(false);
    }
  }

  async function handleReview(r: Reconciliation, decision: string) {
    try {
      await reviewReconciliation(r.id, decision);
      message.success("复核完成");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "复核失败");
    }
  }

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    {
      title: "指标",
      dataIndex: "metric_id",
      key: "metric",
      width: 200,
      render: (v: number, r: Reconciliation) =>
        r.metric_code ? (
          <span className="mono">{r.metric_code}{r.metric_name ? ` · ${r.metric_name}` : ""}</span>
        ) : (
          <span className="mono">#{v}</span>
        ),
    },
    { title: "期望口径", dataIndex: "expected_expr", key: "expected", ellipsis: true, render: (v: string) => <span className="mono">{v}</span> },
    { title: "实际口径", dataIndex: "actual_expr", key: "actual", ellipsis: true, render: (v: string) => <span className="mono">{v}</span> },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (s: string) => <Tag color={s === "APPROVED" ? "success" : s === "REJECTED" ? "error" : "warning"}>{RECON_STATUS_LABEL[s] ?? s}</Tag>,
    },
    {
      title: "操作",
      key: "actions",
      width: 150,
      render: (_: unknown, r: Reconciliation) =>
        r.status === "PENDING" ? (
          <Space>
            <Button size="small" type="primary" disabled={!isGov} onClick={() => handleReview(r, "APPROVED")}>通过</Button>
            <Button size="small" danger disabled={!isGov} onClick={() => handleReview(r, "REJECTED")}>驳回</Button>
          </Space>
        ) : null,
    },
  ];

  return (
    <div>
      {/* 对账用途说明：对比语义端与应用端口径是否一致，保证数据可信 */}
      <div
        style={{
          marginBottom: 12,
          padding: "10px 12px",
          background: "var(--bg-elevated, #fafafa)",
          border: "1px solid var(--line-soft, #eef1f5)",
          borderRadius: 6,
          fontSize: 13,
          color: "var(--text-2)",
          lineHeight: 1.7,
        }}
      >
        <b>口径对账</b>用于校验<b>同一指标在"语义端"与"业务端"计算口径是否一致</b>——
        防止指标定义与业务实际执行发生漂移（如语义端口径改了、业务端还是旧的）。
        <br />
        <span className="muted">
          提交后由治理人员在「待复核」中通过（口径一致）或驳回（存在漂移需修正）。
          状态含义：<Tag color="warning">待复核</Tag>等待治理确认 ·{" "}
          <Tag color="success">已通过</Tag>口径一致 · <Tag color="error">已驳回</Tag>存在漂移。
        </span>
      </div>
      {/* 自动数值对账入口：dimension 侧为人工口径对账，质量中心承载自动数值对账 */}
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message={
          <span>
            本页为<b>人工口径对账</b>（语义端 vs 业务端口径文本复核）。如需<b>自动数值对账</b>
            （基准值 vs 观测值，系统自动计算 diff 并生成 OK/WARN/ALERT，支持定时执行）——
            请前往<b>质量中心 → 对账</b>。
          </span>
        }
        action={
          <Button size="small" icon={<SendOutlined />} onClick={() => navigate("/quality")}>
            前往质量中心
          </Button>
        }
      />
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "flex-end" }}>
        {can("dimension:reconcile") && (
          <Button type="primary" icon={<SendOutlined />} onClick={() => setModalOpen(true)}>提交对账</Button>
        )}
      </div>
      <Table
        dataSource={items}
        columns={columns}
        rowKey="id"
        loading={loading}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          onShowSizeChange: (p: number, ps: number) => {
            setPage(1);
            onShowSizeChange(p, ps);
          },
          onChange: (p: number) => setPage(p),
          pageSizeOptions: [10, 20, 50, 100],
          showTotal: (t: number) => `共 ${t} 条`,
        }}
        locale={{ emptyText: "暂无对账记录" }}
      />

      <Modal title="提交维度对账" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="提交" confirmLoading={saving}>
        <Form form={form} layout="vertical" scrollToFirstError onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="metric_id" label="指标" rules={[{ required: true }]}>
            <Select showSearch options={metrics.map((m) => ({ value: m.id, label: `${m.metric_code} · ${m.name}` }))} placeholder="选择指标" />
          </Form.Item>
          <Form.Item name="expected_expr" label="期望口径（语义端）" rules={[{ required: true }]}>
            <Input.TextArea rows={2} className="mono" />
          </Form.Item>
          <Form.Item name="actual_expr" label="实际口径（应用端）" rules={[{ required: true }]}>
            <Input.TextArea rows={2} className="mono" />
          </Form.Item>
          <Form.Item name="diff_summary" label="差异摘要">
            <Input />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

export function Dimensions() {
  const navigate = useNavigate();

  // 统一返回上一入口：优先回退浏览器历史（总览资产卡片/全局搜索等入口），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  const tabItems = [
    { key: "dims", label: "维度列表", children: <DimensionsTab /> },
    {
      key: "members",
      label: "维度值管理",
      children: <MembersTab />,
    },
    { key: "mappings", label: "维度映射", children: <MappingsTab /> },
    { key: "reconcile", label: "对账记录", children: <ReconciliationsTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
          <div className="page-kicker">指标资产 / 维度管理</div>
          <h2>维度管理</h2>
          <p>维度定义、成员、跨维度映射与口径对账——保证维度语义一致。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 8 } }}>
        <Tabs items={tabItems} />
      </Card>
    </div>
  );
}
