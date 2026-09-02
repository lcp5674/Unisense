import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, InputNumber, Select, Rate, message, Tabs, Space, Alert, Tooltip, Row, Col, Dropdown } from "antd";
import { StarOutlined, DownOutlined } from "@ant-design/icons";
import { listFeedback, submitFeedback, updateFeedbackStatus, clarifyFeedback, submitNps, fetchNpsStats, listUsers, fetchCurrentUser, UnisenseApiError } from "../api";
import { usePermission } from "../hooks/usePermission";
import type { CurrentUser, Feedback, NpsStats } from "../types";
import { formatCnTime, timeAgoCn, parseBackendTime } from "../utils/timeCn";
import { useUserNames } from "../utils/userNames";

// ---- 展示映射（value=英文对接后端，label=中文展示） ----

const STATUS_ZH: Record<string, { label: string; color: string }> = {
  pending: { label: "待处理", color: "default" },
  in_progress: { label: "跟进中", color: "blue" },
  clarifying: { label: "待澄清", color: "orange" },
  adopted: { label: "已采纳", color: "green" },
  rejected: { label: "已驳回", color: "red" },
};

// 反馈对象类型 → 业务术语（覆盖全站可反馈对象，含平台/表/字段等）
const TARGET_TYPE_ZH: Record<string, string> = {
  metric: "指标",
  term: "术语",
  report: "报表",
  dashboard: "仪表盘",
  platform: "平台",
  table: "数据表",
  field: "字段",
  source: "数据源",
  template: "指标模板",
  dimension: "维度",
  favorite: "收藏",
  todo: "待办",
  conflict: "口径冲突",
  nps: "满意度",
};

// 对象类型筛选/提交/NPS 弹窗共用选项（对齐 TARGET_TYPE_ZH 全量可反馈对象）
const TYPE_FILTER_OPTIONS = Object.entries(TARGET_TYPE_ZH).map(([value, label]) => ({
  value,
  label,
}));

// 反馈分类 → 业务术语 + 颜色（运营按类分派处理）
const CATEGORY_ZH: Record<string, { label: string; color: string }> = {
  bug: { label: "缺陷", color: "red" },
  feature: { label: "功能需求", color: "blue" },
  improvement: { label: "改进建议", color: "cyan" },
  question: { label: "咨询", color: "gold" },
  praise: { label: "表扬", color: "green" },
};

// 反馈优先级 → 业务术语 + 颜色（排期与 SLA 依据）
const PRIORITY_ZH: Record<string, { label: string; color: string }> = {
  high: { label: "高", color: "red" },
  medium: { label: "中", color: "orange" },
  low: { label: "低", color: "default" },
};

const CATEGORY_OPTIONS = [
  { value: "bug", label: "缺陷" },
  { value: "feature", label: "功能需求" },
  { value: "improvement", label: "改进建议" },
  { value: "question", label: "咨询" },
  { value: "praise", label: "表扬" },
];

const PRIORITY_OPTIONS = [
  { value: "high", label: "高" },
  { value: "medium", label: "中" },
  { value: "low", label: "低" },
];

// 反馈处理时效：created_at → resolved_at 的耗时中文描述（运营效率 SLA 视角）
function resolveDuration(createdAt: string, resolvedAt: string | null): string | null {
  if (!resolvedAt) return null;
  const start = parseBackendTime(createdAt);
  const end = parseBackendTime(resolvedAt);
  if (!start || !end) return null;
  const mins = Math.max(0, Math.round((end.getTime() - start.getTime()) / 60000));
  if (mins < 60) return `${mins} 分钟`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return mins % 60 ? `${hours} 小时 ${mins % 60} 分钟` : `${hours} 小时`;
  return `${Math.floor(hours / 24)} 天 ${hours % 24} 小时`;
}

const STATUS_FILTER_OPTIONS = [
  { value: "pending", label: "待处理" },
  { value: "in_progress", label: "跟进中" },
  { value: "clarifying", label: "待澄清" },
  { value: "adopted", label: "已采纳" },
  { value: "rejected", label: "已驳回" },
];

interface ProcessDraft {
  feedback: Feedback;
  status: string;
  note: string;
}

interface ClarifyDraft {
  feedback: Feedback;
  text: string;
}

function FeedbackTab({ refreshToken }: { refreshToken?: number }) {
  const { can } = usePermission();
  const [items, setItems] = useState<Feedback[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [targetType, setTargetType] = useState<string | undefined>();
  const [status, setStatus] = useState<string | undefined>();
  const [draft, setDraft] = useState<ProcessDraft | null>(null);
  const [updateLoading, setUpdateLoading] = useState(false);
  // 质疑闭环：澄清弹窗（clarifying 状态反馈由提交人补充说明）
  const [clarifyDraft, setClarifyDraft] = useState<ClarifyDraft | null>(null);
  const [clarifyLoading, setClarifyLoading] = useState(false);
  // 当前登录用户（判断「提交人本人」以展示提交澄清入口）
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  // 业务化解析：user_id → 用户名（对象名称由服务端 target_name 直接提供，前端不再逐条探测）
  const [usersMap, setUsersMap] = useState<Record<number, string>>({});
  // 跨组织精确解析：反馈提交人/处理人可能不在本组织 /auth/users 列表，
  // 用 useUserNames 按已知 id 反查真实中文名，避免回退为「#id」占位。
  const feedbackUserNames = useUserNames(items.flatMap((f) => [f.user_id, f.resolver_id]));
  const userName = (id: number | null | undefined): string | null => {
    if (id == null) return null;
    const resolved = feedbackUserNames[id];
    if (resolved) return resolved.display_name || resolved.username;
    return usersMap[id] ?? null;
  };
  const navigate = useNavigate();

  // 当前登录用户：反馈列表「提交澄清」入口仅提交人本人可见
  useEffect(() => {
    fetchCurrentUser()
      .then((me) => setCurrentUser(me))
      .catch(() => {});
  }, []);

  // 加载用户名单：反馈列表「用户」列展示用户名而非数字 ID
  useEffect(() => {
    listUsers()
      .then((us) => {
        const m: Record<number, string> = {};
        for (const u of us) m[u.id] = u.display_name || u.username;
        setUsersMap(m);
      })
      .catch(() => {});
  }, []);

  async function load(p = page, ps = pageSize, tt = targetType, st = status) {
    setLoading(true);
    try {
      const res = await listFeedback({ target_type: tt, status: st, page: p, page_size: ps });
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(1, pageSize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshToken]);

  function changeType(tt: string | undefined) {
    setTargetType(tt);
    setPage(1);
    load(1, pageSize, tt, status);
  }

  function changeStatus(st: string | undefined) {
    setStatus(st);
    setPage(1);
    load(1, pageSize, targetType, st);
  }

  async function confirmProcess() {
    if (!draft) return;
    setUpdateLoading(true);
    try {
      await updateFeedbackStatus(draft.feedback.id, draft.status, draft.note ? draft.note : null);
      message.success("处理完成");
      setDraft(null);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "更新失败");
    } finally {
      setUpdateLoading(false);
    }
  }

  // 质疑闭环：提交人补充澄清说明（clarifying → in_progress）
  async function confirmClarify() {
    if (!clarifyDraft) return;
    if (!clarifyDraft.text.trim()) {
      message.warning("请填写澄清说明");
      return;
    }
    setClarifyLoading(true);
    try {
      await clarifyFeedback(clarifyDraft.feedback.id, clarifyDraft.text.trim());
      message.success("澄清已提交");
      setClarifyDraft(null);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "提交失败");
    } finally {
      setClarifyLoading(false);
    }
  }

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 64 },
    {
      title: "用户",
      dataIndex: "user_id",
      key: "user",
      width: 100,
      render: (v: number) => userName(v) ?? <span className="muted">未知用户</span>,
    },
    {
      title: "对象类型",
      dataIndex: "target_type",
      key: "targetType",
      width: 100,
      render: (v: string) => <Tag>{TARGET_TYPE_ZH[v] ?? v}</Tag>,
    },
    {
      title: "对象",
      dataIndex: "target_id",
      key: "targetId",
      width: 200,
      render: (v: string | null, f: Feedback) => {
        if (!v) return <span className="muted">—</span>;
        if (f.target_type === "metric") {
          // 对象名称由服务端批量解析（target_name），null 表示对象已失效/被删除
          if (f.target_name) {
            return (
              <Button type="link" size="small" style={{ padding: 0 }} onClick={() => navigate(`/detail/${v}`)}>
                {f.target_name}（{v}）
              </Button>
            );
          }
          // 保留编码但明确标记，避免运营误认为对象仍存在
          return (
            <span>
              <span className="mono">{v}</span> <Tag style={{ marginLeft: 4 }}>已失效</Tag>
            </span>
          );
        }
        return <span className="mono">{v}</span>;
      },
    },
    { title: "评分", dataIndex: "rating", key: "rating", width: 100, render: (v: number | null) => (v !== null ? <Rate disabled defaultValue={v} count={5} /> : <span className="muted">—</span>) },
    {
      title: "分类",
      dataIndex: "category",
      key: "category",
      width: 96,
      render: (v: string) => {
        const c = CATEGORY_ZH[v] ?? { label: v, color: "default" };
        return <Tag color={c.color}>{c.label}</Tag>;
      },
    },
    {
      title: "优先级",
      dataIndex: "priority",
      key: "priority",
      width: 76,
      render: (v: string) => {
        const p = PRIORITY_ZH[v] ?? { label: v, color: "default" };
        return <Tag color={p.color}>{p.label}</Tag>;
      },
    },
    {
      title: "内容",
      dataIndex: "comment",
      key: "comment",
      ellipsis: true,
      render: (v: string | null, f: Feedback) => {
        const main = v ?? "—";
        // 质疑闭环：展示已提交的澄清说明（clarifying/in_progress 期间可追溯）
        const hasClarify = Boolean(f.clarification);
        const full = hasClarify ? `${main}\n\n【澄清】${f.clarification}` : main;
        return (
          <div>
            <Tooltip title={full} placement="topLeft">
              <span>{main}</span>
            </Tooltip>
            {hasClarify && (
              <div style={{ marginTop: 2 }}>
                <Tag color="orange" style={{ marginRight: 0 }}>已澄清</Tag>
              </div>
            )}
          </div>
        );
      },
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 94,
      render: (v: string) => {
        const s = STATUS_ZH[v] ?? { label: v, color: "default" };
        return <Tag color={s.color}>{s.label}</Tag>;
      },
    },
    {
      title: "处理人",
      dataIndex: "resolver_id",
      key: "resolver",
      width: 100,
      render: (v: number | null) => (v !== null ? userName(v) ?? <span className="muted">未知用户</span> : <span className="muted">—</span>),
    },
    {
      title: "处理时间",
      dataIndex: "resolved_at",
      key: "resolved",
      width: 120,
      render: (v: string | null) => (v ? <Tooltip title={formatCnTime(v)}><span>{timeAgoCn(v)}</span></Tooltip> : <span className="muted">—</span>),
    },
    {
      title: "处理时效",
      key: "resolveDuration",
      width: 120,
      render: (_: unknown, f: Feedback) => {
        const d = resolveDuration(f.created_at, f.resolved_at);
        return d ? <span>{d}</span> : <span className="muted">—</span>;
      },
    },
    {
      title: "提交时间",
      dataIndex: "created_at",
      key: "created",
      width: 120,
      render: (v: string) => (
        <Tooltip title={formatCnTime(v)}>
          <span>{timeAgoCn(v)}</span>
        </Tooltip>
      ),
    },
    {
      title: "处理",
      key: "actions",
      width: 280,
      render: (_: unknown, f: Feedback) => {
        // 终态（已采纳/已驳回）：处理结果已由状态列展示，操作列留空避免重复
        if (f.status === "adopted" || f.status === "rejected") {
          return <span className="muted">—</span>;
        }
        // 质疑闭环：待澄清反馈由提交人本人补充说明（PLAT-2：他人不可代答）
        if (f.status === "clarifying" && currentUser && f.user_id === currentUser.id) {
          return (
            <Button
              size="small"
              type="primary"
              onClick={() => setClarifyDraft({ feedback: f, text: f.clarification ?? "" })}
            >
              提交澄清
            </Button>
          );
        }
        if (!can("feedback:manage")) {
          return <span className="muted">无处置权限</span>;
        }
        return (
          <Space size={8} wrap>
            <Button size="small" type="primary" onClick={() => setDraft({ feedback: f, status: "adopted", note: "" })}>
              采纳
            </Button>
            <Button size="small" danger onClick={() => setDraft({ feedback: f, status: "rejected", note: "" })}>
              驳回
            </Button>
            <Dropdown
              trigger={["click"]}
              menu={{
                items: [
                  {
                    key: "follow",
                    label: "跟进",
                    disabled: f.status === "in_progress",
                    onClick: () => setDraft({ feedback: f, status: "in_progress", note: "" }),
                  },
                  {
                    key: "clarify",
                    label: "待澄清",
                    onClick: () => setDraft({ feedback: f, status: "clarifying", note: "" }),
                  },
                ],
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
        <span className="muted">类型：</span>
        <Select showSearch
          allowClear
          placeholder="全部类型"
          style={{ width: 120 }}
          options={TYPE_FILTER_OPTIONS}
          value={targetType}
          onChange={changeType}
        />
        <span className="muted">状态：</span>
        <Select showSearch
          allowClear
          placeholder="全部状态"
          style={{ width: 120 }}
          options={STATUS_FILTER_OPTIONS}
          value={status}
          onChange={changeStatus}
        />
      </Space>

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
          pageSizeOptions: [10, 20, 50],
          showTotal: (t) => `共 ${t} 条`,
          onChange: (p, ps) => {
            setPage(p);
            setPageSize(ps);
            load(p, ps, targetType, status);
          },
        }}
        locale={{ emptyText: "暂无反馈" }}
      />

      <Modal
        title={`${STATUS_ZH[draft?.status ?? ""]?.label ?? ""}反馈`}
        open={draft !== null}
        onCancel={() => setDraft(null)}
        onOk={confirmProcess}
        okText="确认处理"
        confirmLoading={updateLoading}
      >
        <div style={{ marginBottom: 12 }}>
          <span className="muted">反馈 #{draft?.feedback.id}：{draft?.feedback.comment ?? "（无内容）"}</span>
        </div>
        <div className="muted" style={{ marginBottom: 6 }}>处理说明：</div>
        <Input.TextArea
          value={draft?.note ?? ""}
          onChange={(e) => setDraft((d) => (d ? { ...d, note: e.target.value } : d))}
          placeholder="请输入处理说明（如：已反馈产品，排期支持）"
          rows={3}
        />
      </Modal>

      <Modal
        title={`提交澄清 · 反馈 #${clarifyDraft?.feedback.id ?? ""}`}
        open={clarifyDraft !== null}
        onCancel={() => setClarifyDraft(null)}
        onOk={confirmClarify}
        okText="提交澄清"
        confirmLoading={clarifyLoading}
      >
        <div style={{ marginBottom: 12 }}>
          <span className="muted">反馈 #{clarifyDraft?.feedback.id}：{clarifyDraft?.feedback.comment ?? "（无内容）"}</span>
        </div>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="请补充口径分歧的澄清说明。提交后该反馈回到「跟进中」，由处理人继续修订/采纳/驳回。"
        />
        <div className="muted" style={{ marginBottom: 6 }}>澄清说明：</div>
        <Input.TextArea
          value={clarifyDraft?.text ?? ""}
          onChange={(e) => setClarifyDraft((d) => (d ? { ...d, text: e.target.value } : d))}
          placeholder="如：该指标按门诊人次口径统计（含退号），与药品处方口径不同……"
          rows={4}
        />
        {clarifyDraft?.feedback.clarification && (
          <div style={{ marginTop: 12 }}>
            <div className="muted" style={{ marginBottom: 4 }}>已提交过的澄清：</div>
            <div style={{ padding: "8px 12px", background: "var(--paper)", borderRadius: 4 }}>
              {clarifyDraft.feedback.clarification}
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}

function SubmitFeedbackTab({ onSubmitted }: { onSubmitted?: () => void }) {
  const [form] = Form.useForm();

  async function handleSubmit(values: Record<string, unknown>) {
    try {
      await submitFeedback({
        target_type: String(values.target_type),
        rating: values.rating !== undefined ? Number(values.rating) : null,
        comment: values.comment ? String(values.comment) : null,
        category: values.category ? String(values.category) : "improvement",
        priority: values.priority ? String(values.priority) : "medium",
        // 自动捕获当前页面 URL，便于运营复现问题/了解用户路径
        source_url: window.location.href,
      });
      message.success("反馈已提交");
      form.resetFields();
      // 提交成功 → 切回「用户反馈」列表并刷新，让新反馈立即可见
      onSubmitted?.();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "提交失败");
    }
  }

  return (
    <Card title="提交反馈" size="small">
      <Form form={form} layout="vertical" onFinish={handleSubmit} style={{ maxWidth: 520 }}>
        <Form.Item name="target_type" label="对象类型" rules={[{ required: true }]} initialValue="metric">
          <Select showSearch options={TYPE_FILTER_OPTIONS} />
        </Form.Item>
        <Form.Item name="category" label="分类" initialValue="improvement">
          <Select showSearch options={CATEGORY_OPTIONS} />
        </Form.Item>
        <Form.Item name="priority" label="优先级" initialValue="medium">
          <Select showSearch options={PRIORITY_OPTIONS} />
        </Form.Item>
        <Form.Item name="rating" label="评分">
          <Rate count={5} />
        </Form.Item>
        <Form.Item name="comment" label="意见" rules={[{ required: true, message: "请填写反馈意见" }]}>
          <Input.TextArea rows={3} placeholder="请描述你的问题或建议（便于运营分派处理）" />
        </Form.Item>
        <div className="muted" style={{ marginBottom: 12 }}>来源页面将自动记录（便于运营复现与定位）。</div>
        <Button type="primary" htmlType="submit">提交反馈</Button>
      </Form>
    </Card>
  );
}

function NpsScores({ stats }: { stats: NpsStats | null }) {
  const score = stats?.score;
  const scoreColor = score !== undefined ? (score >= 50 ? "var(--ok)" : score >= 0 ? "var(--signal)" : "var(--danger)") : "var(--text-tertiary)";
  return (
    <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
      <Col xs={24} sm={6}>
        <Card size="small">
          <div className="muted">NPS 得分</div>
          <div style={{ fontFamily: "var(--font-display)", fontSize: 40, fontWeight: 700, color: scoreColor }}>
            {score !== undefined ? score : "—"}
          </div>
        </Card>
      </Col>
      <Col xs={24} sm={6}>
        <Card size="small">
          <div className="muted">推荐者（9-10）</div>
          <div style={{ fontSize: 28, fontWeight: 600, color: "var(--ok)" }}>{stats?.promoters ?? "—"}</div>
        </Card>
      </Col>
      <Col xs={24} sm={6}>
        <Card size="small">
          <div className="muted">被动者（7-8）</div>
          <div style={{ fontSize: 28, fontWeight: 600, color: "var(--signal)" }}>{stats?.passives ?? "—"}</div>
        </Card>
      </Col>
      <Col xs={24} sm={6}>
        <Card size="small">
          <div className="muted">贬损者（0-6）</div>
          <div style={{ fontSize: 28, fontWeight: 600, color: "var(--danger)" }}>{stats?.detractors ?? "—"}</div>
        </Card>
      </Col>
    </Row>
  );
}

function NpsTab() {
  const [modalOpen, setModalOpen] = useState(false);
  const [score, setScore] = useState<number>(8);
  const [stats, setStats] = useState<NpsStats | null>(null);
  const [form] = Form.useForm();

  function loadStats() {
    fetchNpsStats().then(setStats).catch(() => {});
  }

  useEffect(() => {
    loadStats();
  }, []);

  async function handleSubmit(values: Record<string, unknown>) {
    try {
      await submitNps({
        score: score,
        comment: values.comment ? String(values.comment) : null,
        target_type: String(values.target_type ?? "platform"),
        target_id: values.target_id ? String(values.target_id) : null,
      });
      message.success(`NPS ${score}/10 已提交，感谢反馈`);
      setModalOpen(false);
      form.resetFields();
      loadStats();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "提交失败");
    }
  }

  return (
    <div>
      <Alert type="info" showIcon style={{ marginBottom: 12 }} message="净推荐值（NPS）调查——0-10 分，9-10 为推荐者。" />
      <NpsScores stats={stats} />
      <Button type="primary" icon={<StarOutlined />} onClick={() => setModalOpen(true)}>参与 NPS 调查</Button>

      <Modal title="NPS 调查" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="提交">
        <div style={{ textAlign: "center", padding: "12px 0" }}>
          <div className="muted" style={{ marginBottom: 8 }}>你有多大可能向同事推荐 Unisense？</div>
          <div style={{ fontFamily: "var(--font-display)", fontSize: 48, fontWeight: 700, color: score >= 9 ? "var(--ok)" : score >= 7 ? "var(--signal)" : "var(--danger)" }}>
            {score}
          </div>
          <InputNumber min={0} max={10} value={score} onChange={(v) => setScore(v ?? 8)} style={{ width: 140 }} />
        </div>
        <Form form={form} layout="vertical" onFinish={handleSubmit}>
          <Form.Item name="target_type" label="对象类型" initialValue="platform">
            <Select showSearch options={TYPE_FILTER_OPTIONS} />
          </Form.Item>
          <Form.Item name="comment" label="原因（可选）">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

export function FeedbackCenter() {
  // 提交反馈成功后自动切回列表并刷新（避免用户需手动刷新才能看到新反馈）
  const [activeTab, setActiveTab] = useState("feedback");
  const [refreshToken, setRefreshToken] = useState(0);
  const tabItems = [
    { key: "feedback", label: "用户反馈", children: <FeedbackTab refreshToken={refreshToken} /> },
    {
      key: "submit",
      label: "提交反馈",
      children: (
        <SubmitFeedbackTab
          onSubmitted={() => {
            setRefreshToken((t) => t + 1);
            setActiveTab("feedback");
          }}
        />
      ),
    },
    { key: "nps", label: "NPS 调查", children: <NpsTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Operation / Feedback</div>
          <h2>用户反馈</h2>
          <p>反馈闭环处理与满意度（NPS）采集——面向平台运营。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 8 } }}>
        <Tabs activeKey={activeTab} onChange={setActiveTab} items={tabItems} />
      </Card>
    </div>
  );
}