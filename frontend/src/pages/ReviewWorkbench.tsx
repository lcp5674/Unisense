import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card, Input, Modal, Radio, Select, Space, Table, Tag, message } from "antd";
import { ArrowLeftOutlined } from "@ant-design/icons";
import {
  arbitrateConflict,
  closeConflict,
  compareMetrics,
  escalateConflict,
  listConflicts,
  listConflictRulings,
  UnisenseApiError,
} from "../api";
import type { ConflictResponse, MetricCompareResult, RulingRecord } from "../types";
import { useTracking } from "../hooks/useTracking";
import { MetricCompareTable } from "../components/MetricCompareTable";
import { formatCnTime } from "../utils/timeCn";

const STATUS_LABEL: Record<string, string> = {
  OPEN: "待处理",
  NEGOTIATING: "协商中",
  RULED: "已裁决",
  CLOSED: "已关闭",
  ESCALATED: "已升级",
};

const STATUS_COLOR: Record<string, string> = {
  OPEN: "warning",
  NEGOTIATING: "processing",
  RULED: "success",
  CLOSED: "default",
  ESCALATED: "error",
};

const CONFLICT_TYPE_LABEL: Record<string, string> = {
  same_name_diff_def: "同名不同义",
  same_def_diff_name: "同义不同名",
  grain_unit: "粒度/单位冲突",
  cross_domain_same_def: "跨域同口径异源",
  version_conflict: "口径版本冲突",
  pii: "PII 冲突",
};

// 仲裁决策：choose_existing/choose_candidate 映射后端 choose_canonical；merge/keep_diff 一一对应。
type ArbitralDecision = "choose_existing" | "choose_candidate" | "merge" | "keep_diff";

interface DecisionOption {
  value: ArbitralDecision;
  label: string;
  desc: string;
}

// TD §12.4：仲裁 = 选唯一口径 / 合并 / 保留差异。不同冲突类型给出差异化决策集与默认值。
const DECISION_OPTIONS: Record<string, DecisionOption[]> = {
  same_name_diff_def: [
    { value: "choose_existing", label: "采纳现有为权威", desc: "现有口径更准确，候选修正后发布" },
    { value: "choose_candidate", label: "采纳候选为权威", desc: "候选口径更准确，现有标记废弃" },
    { value: "keep_diff", label: "保留差异（非真冲突）", desc: "认定非冲突，两者共存" },
  ],
  same_def_diff_name: [
    { value: "merge", label: "合并到现有", desc: "候选并入现有口径，消除重复建设" },
    { value: "choose_candidate", label: "以候选为权威", desc: "候选口径更准确，现有并入候选" },
    { value: "keep_diff", label: "保留差异", desc: "认定非冲突，两者共存" },
  ],
  grain_unit: [
    { value: "choose_existing", label: "采纳现有口径", desc: "以现有粒度/单位为准" },
    { value: "choose_candidate", label: "采纳候选口径", desc: "以候选粒度/单位为准" },
    { value: "keep_diff", label: "保留差异", desc: "消费方自行绑定正确粒度/单位" },
  ],
  cross_domain_same_def: [
    { value: "merge", label: "合并到现有", desc: "统一口径，明确单一真相源" },
    { value: "choose_existing", label: "明确现有为权威源", desc: "现有源为权威，候选不启用" },
    { value: "keep_diff", label: "保留差异", desc: "两域各自维护，不合并" },
  ],
  version_conflict: [
    { value: "choose_existing", label: "采纳现有版本", desc: "以当前生效版本口径为准" },
    { value: "choose_candidate", label: "采纳候选版本", desc: "以候选新版本口径为准" },
  ],
};

// 前端决策 → 后端 arbitrate 入参（decision + canonical_metric_code）
function toBackendPayload(c: ConflictResponse, d: ArbitralDecision) {
  const candidate = c.candidate_metric_code ?? "";
  const existing = c.existing_metric_code ?? "";
  switch (d) {
    case "choose_existing":
      return { decision: "choose_canonical", canonical_metric_code: existing };
    case "choose_candidate":
      return { decision: "choose_canonical", canonical_metric_code: candidate };
    case "merge":
      return { decision: "merge", canonical_metric_code: existing };
    case "keep_diff":
      return { decision: "keep_diff", canonical_metric_code: "" };
  }
}

function errText(err: unknown, fallback: string) {
  return err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : fallback;
}

/** 弹窗顶部冲突摘要：类型 + 相似度 + 状态 */
function ConflictSummary({ c }: { c: ConflictResponse }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <Space wrap>
        <Tag>{CONFLICT_TYPE_LABEL[c.type] ?? c.type}</Tag>
        <Tag color={STATUS_COLOR[c.status]}>{STATUS_LABEL[c.status] ?? c.status}</Tag>
        <span className="muted">相似度 {(Number(c.similarity_score) * 100).toFixed(1)}%</span>
      </Space>
      {c.description ? <p style={{ marginTop: 8 }}>{c.description}</p> : null}
    </div>
  );
}

export function ReviewWorkbench() {
  const [items, setItems] = useState<ConflictResponse[]>([]);
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  // 仲裁弹窗
  const [arbitrating, setArbitrating] = useState<ConflictResponse | null>(null);
  const [decision, setDecision] = useState<ArbitralDecision>("choose_existing");
  const [reason, setReason] = useState("");
  // 对比数据（仲裁弹窗与只读对比弹窗共用）
  const [compareResult, setCompareResult] = useState<MetricCompareResult | null>(null);
  const [compareLoading, setCompareLoading] = useState(false);
  const [compareOpen, setCompareOpen] = useState<ConflictResponse | null>(null);
  // 升级弹窗
  const [escalating, setEscalating] = useState<ConflictResponse | null>(null);
  const [escalateNote, setEscalateNote] = useState("");
  // 裁决记录（历史知识库）弹窗
  const [rulingsFor, setRulingsFor] = useState<ConflictResponse | null>(null);
  const [rulings, setRulings] = useState<RulingRecord[]>([]);
  const [rulingsLoading, setRulingsLoading] = useState(false);

  const navigate = useNavigate();
  const { track } = useTracking();

  // 统一返回上一入口：优先回退浏览器历史（总览快捷入口等），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  async function load() {
    setLoading(true);
    try {
      const res = await listConflicts({ status, page_size: 50 });
      setItems(res.items);
    } catch (err) {
      message.error(errText(err, "加载失败"));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  async function loadCompare(c: ConflictResponse) {
    const candidate = c.candidate_metric_code ?? "";
    const existing = c.existing_metric_code ?? "";
    if (!candidate || !existing) {
      setCompareResult(null);
      return;
    }
    setCompareLoading(true);
    setCompareResult(null);
    try {
      setCompareResult(await compareMetrics(candidate, existing));
    } catch (err) {
      message.error(errText(err, "加载差异对比失败"));
    } finally {
      setCompareLoading(false);
    }
  }

  function openArbitrate(c: ConflictResponse) {
    setArbitrating(c);
    setDecision(DECISION_OPTIONS[c.type]?.[0]?.value ?? "choose_existing");
    setReason("");
    loadCompare(c);
  }

  function openCompare(c: ConflictResponse) {
    setCompareOpen(c);
    loadCompare(c);
  }

  async function submitArbitrate() {
    const c = arbitrating;
    if (!c) return;
    const payload = toBackendPayload(c, decision);
    setBusyId(c.conflict_id);
    try {
      await arbitrateConflict(c.conflict_id, payload.decision, payload.canonical_metric_code);
      message.success(`已仲裁：${c.conflict_id}`);
      track("review_arbitrate", c.conflict_id, "conflict");
      setArbitrating(null);
      load();
    } catch (err) {
      message.error(errText(err, "仲裁失败（仅 compliance_officer/domain_admin）"));
    } finally {
      setBusyId(null);
    }
  }

  async function submitEscalate() {
    const c = escalating;
    if (!c) return;
    setBusyId(c.conflict_id);
    try {
      await escalateConflict(c.conflict_id, escalateNote);
      message.success(`已升级：${c.conflict_id}`);
      track("review_escalate", c.conflict_id, "conflict");
      setEscalating(null);
      load();
    } catch (err) {
      message.error(errText(err, "升级失败"));
    } finally {
      setBusyId(null);
    }
  }

  async function handleClose(c: ConflictResponse) {
    setBusyId(c.conflict_id);
    try {
      await closeConflict(c.conflict_id);
      message.success(`已关闭：${c.conflict_id}`);
      load();
    } catch (err) {
      message.error(errText(err, "关闭失败（仅 RULED 状态可关闭）"));
    } finally {
      setBusyId(null);
    }
  }

  // 打开历史裁决记录（知识库条目，GET /conflicts/{id}/rulings）
  async function openRulings(c: ConflictResponse) {
    setRulingsFor(c);
    setRulings([]);
    setRulingsLoading(true);
    try {
      setRulings(await listConflictRulings(c.conflict_id));
    } catch (err) {
      message.error(errText(err, "加载裁决记录失败"));
    } finally {
      setRulingsLoading(false);
    }
  }

  const actionsFor = (c: ConflictResponse) => {
    if (c.type === "pii") {
      return <Tag>已转交治理</Tag>;
    }
    const actions: ReactNode[] = [];
    if (c.status !== "CLOSED") {
      actions.push(
        <Button type="link" size="small" onClick={() => openCompare(c)}>
          对比
        </Button>,
      );
    }
    if (c.status === "OPEN" || c.status === "NEGOTIATING" || c.status === "ESCALATED") {
      actions.push(
        <Button
          type="primary"
          size="small"
          disabled={busyId === c.conflict_id}
          onClick={() => openArbitrate(c)}
        >
          仲裁
        </Button>,
      );
    }
    if (c.status === "OPEN" || c.status === "NEGOTIATING") {
      actions.push(
        <Button size="small" danger disabled={busyId === c.conflict_id} onClick={() => {
          setEscalating(c);
          setEscalateNote("");
        }}>
          升级
        </Button>,
      );
    }
    if (c.status === "RULED") {
      actions.push(
        <Button size="small" disabled={busyId === c.conflict_id} onClick={() => handleClose(c)}>
          关闭
        </Button>,
      );
    }
    // 已裁决 / 已关闭可查看历史裁决记录（知识库）
    if (c.status === "RULED" || c.status === "CLOSED") {
      actions.push(
        <Button size="small" onClick={() => openRulings(c)}>
          裁决记录
        </Button>,
      );
    }
    return actions.length ? <Space>{actions}</Space> : null;
  };

  const columns = [
    { title: "冲突ID", dataIndex: "conflict_id", key: "conflict_id" },
    { title: "类型", dataIndex: "type", key: "type", render: (v: string) => CONFLICT_TYPE_LABEL[v] ?? v },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      render: (s: string) => <Tag color={STATUS_COLOR[s]}>{STATUS_LABEL[s] ?? s}</Tag>,
    },
    {
      title: "相似度",
      dataIndex: "similarity_score",
      key: "similarity_score",
      width: 90,
      render: (v: number) => `${(Number(v) * 100).toFixed(1)}%`,
    },
    {
      title: "候选指标",
      key: "candidate",
      width: 180,
      ellipsis: true,
      render: (_: unknown, r: ConflictResponse) => {
        const code = r.candidate_metric_code ?? "";
        return (
          <Button type="link" size="small" onClick={() => navigate(`/detail/${code}`)}>
            {code}
          </Button>
        );
      },
    },
    {
      title: "现有指标",
      key: "existing",
      width: 180,
      ellipsis: true,
      render: (_: unknown, r: ConflictResponse) => {
        const code = r.existing_metric_code ?? "";
        return (
          <Button type="link" size="small" onClick={() => navigate(`/detail/${code}`)}>
            {code}
          </Button>
        );
      },
    },
    {
      title: "描述",
      key: "description",
      width: 200,
      ellipsis: true,
      render: (_: unknown, r: ConflictResponse) => r.description ?? <span className="muted">—</span>,
    },
    {
      title: "检测时间",
      key: "detected_at",
      width: 160,
      render: (_: unknown, r: ConflictResponse) => (r.detected_at ? formatCnTime(r.detected_at) : ""),
    },
    { title: "操作", key: "actions", render: (_: unknown, c: ConflictResponse) => actionsFor(c) },
  ];

  const decisionOptions = arbitrating ? DECISION_OPTIONS[arbitrating.type] ?? [] : [];
  const arbitrateBusy = arbitrating != null && busyId === arbitrating.conflict_id;

  return (
    <div>
      <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 8 }}>
        返回
      </Button>
      <Card
        title="审核工作台（冲突仲裁）"
        extra={
          <Select
            value={status || undefined}
            onChange={(v) => setStatus(v || "")}
            style={{ width: 140 }}
            allowClear
            placeholder="全部状态"
            options={[
              { value: "OPEN", label: "待处理" },
              { value: "NEGOTIATING", label: "协商中" },
              { value: "RULED", label: "已裁决" },
              { value: "ESCALATED", label: "已升级" },
              { value: "CLOSED", label: "已关闭" },
            ]}
          />
        }
      >
        <Table
          dataSource={items}
          columns={columns}
          rowKey="conflict_id"
          loading={loading}
          pagination={false}
          locale={{ emptyText: "暂无冲突" }}
        />
      </Card>

      {/* 只读对比弹窗：当前窗口展示候选 vs 现有差异，无需跳转指标目录 */}
      <Modal
        className="compare-modal"
        title={`差异对比 ${compareOpen?.conflict_id ?? ""}`}
        open={compareOpen != null}
        onCancel={() => setCompareOpen(null)}
        footer={<Button onClick={() => setCompareOpen(null)}>关闭</Button>}
        width={1120}
      >
        {compareOpen ? (
          <>
            <ConflictSummary c={compareOpen} />
            {compareLoading ? (
              <Card loading style={{ minHeight: 160 }} />
            ) : compareResult ? (
              <MetricCompareTable
                result={compareResult}
                codeA={compareOpen.candidate_metric_code ?? ""}
                codeB={compareOpen.existing_metric_code ?? ""}
                labelA="候选指标"
                labelB="现有指标"
                size="small"
              />
            ) : (
              <p className="muted">无可对比的指标编码</p>
            )}
          </>
        ) : null}
      </Modal>

      {/* 仲裁弹窗：差异对比 + 按类型差异化的决策表单 */}
      <Modal
        className="compare-modal"
        title={`仲裁冲突 ${arbitrating?.conflict_id ?? ""}`}
        open={arbitrating != null}
        onCancel={() => setArbitrating(null)}
        onOk={submitArbitrate}
        okText="提交裁决"
        okButtonProps={{ loading: arbitrateBusy, disabled: decisionOptions.length === 0 }}
        cancelButtonProps={{ disabled: arbitrateBusy }}
        width={1120}
      >
        {arbitrating ? (
          <>
            <ConflictSummary c={arbitrating} />
            {compareLoading ? (
              <Card loading style={{ minHeight: 160 }} />
            ) : compareResult ? (
              <MetricCompareTable
                result={compareResult}
                codeA={arbitrating.candidate_metric_code ?? ""}
                codeB={arbitrating.existing_metric_code ?? ""}
                labelA="候选指标"
                labelB="现有指标"
                size="small"
              />
            ) : (
              <p className="muted">无可对比的指标编码</p>
            )}
            {decisionOptions.length > 0 ? (
              <div style={{ marginTop: 16 }}>
                <p>
                  <strong>裁决方式</strong>
                </p>
                <Radio.Group
                  value={decision}
                  onChange={(e) => setDecision(e.target.value)}
                  disabled={arbitrateBusy}
                >
                  <Space direction="vertical">
                    {decisionOptions.map((opt) => (
                      <Radio key={opt.value} value={opt.value}>
                        <span>
                          {opt.label}
                          <span className="muted" style={{ marginLeft: 8 }}>
                            {opt.desc}
                          </span>
                        </span>
                      </Radio>
                    ))}
                  </Space>
                </Radio.Group>
                <Input.TextArea
                  style={{ marginTop: 12 }}
                  rows={2}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="裁决理由（沉淀为规则知识库，建议写明口径依据）"
                  disabled={arbitrateBusy}
                />
              </div>
            ) : (
              <p className="muted">该冲突类型已转交治理流程，请在治理中心处理。</p>
            )}
          </>
        ) : null}
      </Modal>

      {/* 升级弹窗 */}
      <Modal
        title={`升级冲突 ${escalating?.conflict_id ?? ""}`}
        open={escalating != null}
        onCancel={() => setEscalating(null)}
        onOk={submitEscalate}
        okText="确认升级"
        okButtonProps={{ loading: escalating != null && busyId === escalating.conflict_id }}
      >
        <Input
          placeholder="升级备注（说明协商未决原因，将通知 domain_admin）"
          value={escalateNote}
          onChange={(e) => setEscalateNote(e.target.value)}
        />
      </Modal>

      {/* 历史裁决记录弹窗（知识库） */}
      <Modal
        title={`裁决记录 ${rulingsFor?.conflict_id ?? ""}`}
        open={rulingsFor != null}
        onCancel={() => setRulingsFor(null)}
        footer={<Button onClick={() => setRulingsFor(null)}>关闭</Button>}
        width={760}
      >
        {rulingsFor ? (
          <>
            <ConflictSummary c={rulingsFor} />
            {rulingsLoading ? (
              <Card loading style={{ minHeight: 160 }} />
            ) : rulings.length === 0 ? (
              <p className="muted">暂无裁决记录（仲裁后的知识库条目将沉淀于此）。</p>
            ) : (
              <Table
                rowKey="id"
                size="small"
                dataSource={rulings}
                pagination={false}
                columns={[
                  { title: "ID", dataIndex: "id", key: "id", width: 70 },
                  {
                    title: "决策",
                    dataIndex: "decision",
                    key: "decision",
                    width: 140,
                    render: (v: string | null) => v ?? "—",
                  },
                  {
                    title: "理由",
                    dataIndex: "reason",
                    key: "reason",
                    render: (v: string | null) => v ?? "—",
                  },
                  {
                    title: "仲裁人",
                    dataIndex: "arbitrator_id",
                    key: "arbitrator",
                    width: 90,
                    render: (v: number | null) => (v != null ? <span className="mono">#{v}</span> : "—"),
                  },
                  {
                    title: "裁决时间",
                    dataIndex: "decided_at",
                    key: "decided_at",
                    width: 170,
                    render: (v: string | null) => (v ? formatCnTime(v) : "—"),
                  },
                ]}
              />
            )}
          </>
        ) : null}
      </Modal>
    </div>
  );
}
