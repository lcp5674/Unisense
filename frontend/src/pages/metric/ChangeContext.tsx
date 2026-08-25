import { useEffect, useRef, useState } from "react";
import { Descriptions, Spin, Space, Tag } from "antd";
import { listVersions } from "../../api";
import type { MetricResponse, MetricVersionResponse } from "../../types";
import { CHANGE_TYPE_LABEL } from "../../utils/enums";
import { DEF_FIELD_LABEL } from "../../utils/display";

// 指标变更上下文（新增/变更/破坏性变更/废弃恢复重评审）：
// 供审批页（MetricReview）与指标详情页（MetricDetail）复用——审批时展示本次是新增还是变更、
// 变更时展示 diff_json 前后对比。判定逻辑与展示组件内聚于此，避免两页重复实现。

export type ChangeInfo = {
  kind: "new" | "update" | "breaking" | "resubmit" | "unknown";
  tag: string;
  color: string;
  diff: Record<string, unknown> | null;
  prev: number | null;
  cur: number;
  note: string;
};

// 最近一个已发布/实验生效版本号（versions 为降序，取首个即最新）
export function lastPublishedVersion(versions: MetricVersionResponse[]): number | null {
  for (const v of versions) {
    if (v.status === "PUBLISHED" || v.status === "EXPERIMENTAL") return v.version;
  }
  return null;
}

// 判定本次审批是新增/变更/破坏性变更/废弃恢复重评审。
// 主判据为当前版本记录的 change_type（CREATE/UPDATE/BREAKING）——不可用 metric.status 判定：
// 「我审过的」回看里 approve 后 status 已转正 PUBLISHED，须靠版本记录区分新增/变更。
// 重评审唯一可靠信号：待审（REVIEW）时当前版本记录仍为已发布（published_at 非空），
// 须 gating metric.status==="REVIEW"，否则回看视图会把已转正的变更版本全部误判为重评审。
export function buildChangeInfo(metric: MetricResponse, versions: MetricVersionResponse[]): ChangeInfo {
  const cur = versions.find((v) => v.version === metric.version) ?? versions[0] ?? null;
  if (!cur) {
    return { kind: "unknown", tag: "—", color: "default", diff: null, prev: null, cur: metric.version, note: "暂无版本记录" };
  }
  const curPublished = cur.status === "PUBLISHED" || cur.status === "EXPERIMENTAL" || Boolean(cur.published_at);
  if (metric.status === "REVIEW" && curPublished) {
    return {
      kind: "resubmit",
      tag: "废弃恢复重评审",
      color: "purple",
      diff: cur.diff_json,
      prev: null,
      cur: cur.version,
      note: "该指标此前已发布，本次为废弃后重新提交评审",
    };
  }
  const hasPublished =
    metric.effective_version != null ||
    versions.some((v) => v.status === "PUBLISHED" || v.status === "EXPERIMENTAL");
  const prev = metric.effective_version ?? lastPublishedVersion(versions);
  if (cur.change_type === "CREATE") {
    return { kind: "new", tag: "新增指标", color: "blue", diff: null, prev: null, cur: cur.version, note: "首次提交评审，无历史口径可对比" };
  }
  if (cur.change_type === "BREAKING") {
    return { kind: "breaking", tag: "破坏性变更", color: "red", diff: cur.diff_json, prev, cur: cur.version, note: "口径发生破坏性变更（如更换逻辑度量/挂载）" };
  }
  if (cur.change_type === "UPDATE") {
    // 从未发布（驳回后修改重提/草稿迭代）仍属新增，不展示 diff
    if (!hasPublished) {
      return { kind: "new", tag: "新增指标", color: "blue", diff: null, prev: null, cur: cur.version, note: "首次提交评审，无历史口径可对比" };
    }
    return { kind: "update", tag: "变更指标", color: "orange", diff: cur.diff_json, prev, cur: cur.version, note: "对已发布指标的口径调整" };
  }
  return { kind: "unknown", tag: cur.change_type || "—", color: "default", diff: cur.diff_json, prev, cur: cur.version, note: "" };
}

// 变更类型 Tag 的版本后缀：新增/未知不展示；变更且前后版本不同展示 v{prev}→v{cur}
// （prev===cur 表示回看已发布版本——effective_version 已是当前版本，仅展示 v{cur}）；其余展示 v{cur}
export function changeVersionText(info: ChangeInfo): string {
  if (info.kind === "new" || info.kind === "unknown") return "";
  if (info.kind === "update" && info.prev != null && info.prev !== info.cur) return ` v${info.prev}→v${info.cur}`;
  return ` v${info.cur}`;
}

// diff_json 单值渲染：string→代码块（长 SQL 换行防撑破）、array→Tag 列表、object→JSON、空→占位
function DiffValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <span className="muted">（无，新增）</span>;
  }
  if (typeof value === "string") {
    return (
      <pre className="mono" style={{ margin: 0, fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-word", maxWidth: "100%" }}>
        {value}
      </pre>
    );
  }
  if (Array.isArray(value)) {
    return (
      <Space size={4} wrap>
        {value.map((x, i) => (
          <Tag key={i} className="mono">{String(x)}</Tag>
        ))}
      </Space>
    );
  }
  if (typeof value === "object") {
    return (
      <pre style={{ margin: 0, fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-word", maxWidth: "100%" }}>
        {JSON.stringify(value, null, 2)}
      </pre>
    );
  }
  return <span className="mono">{String(value)}</span>;
}

// 变更前后对比：diff_json 结构为 {字段: {before, after, change_type}}（service._compute_diff），
// 字段标签复用 DEF_FIELD_LABEL（口径字段中文名），BREAKING 字段橙边 / UPDATE 蓝边区分
export function MetricDiffView({ diff }: { diff: Record<string, unknown> }) {
  const entries = Object.entries(diff).filter(
    ([, v]) => v != null && typeof v === "object" && !Array.isArray(v) && ("before" in v || "after" in v),
  );
  if (!entries.length) return <span className="muted">—</span>;
  return (
    <div style={{ marginTop: 8 }}>
      {entries.map(([field, raw]) => {
        const d = raw as { before?: unknown; after?: unknown; change_type?: string };
        const breaking = d.change_type === "BREAKING";
        return (
          <div key={field} style={{ marginBottom: 10, borderLeft: `3px solid ${breaking ? "#fa8c16" : "#1677ff"}`, paddingLeft: 8 }}>
            <Space size={8} style={{ marginBottom: 4 }}>
              <span style={{ fontWeight: 600 }}>{DEF_FIELD_LABEL[field] ?? field}</span>
              <Tag color={breaking ? "orange" : "blue"}>{CHANGE_TYPE_LABEL[d.change_type ?? ""] ?? "变更"}</Tag>
            </Space>
            <Descriptions column={1} size="small" bordered>
              <Descriptions.Item label="变更前"><DiffValue value={d.before} /></Descriptions.Item>
              <Descriptions.Item label="变更后"><DiffValue value={d.after} /></Descriptions.Item>
            </Descriptions>
          </div>
        );
      })}
    </div>
  );
}

// 待我审确认弹窗内的变更上下文摘要：自加载版本历史，判定本次审批是新增/变更/破坏性/重评审。
// Modal.confirm content 为普通 ReactNode，组件内 setState 可驱动重渲染；mountedRef 防卸载后 setState。
export function ReviewChangeSummary({ metric }: { metric: MetricResponse }) {
  const [versions, setVersions] = useState<MetricVersionResponse[] | null>(null);
  const [failed, setFailed] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    setVersions(null);
    setFailed(false);
    listVersions(metric.metric_code)
      .then((vs) => { if (mounted.current) setVersions(vs); })
      .catch(() => { if (mounted.current) setFailed(true); });
    return () => { mounted.current = false; };
  }, [metric.metric_code]);
  if (failed) {
    return <div style={{ marginBottom: 12 }}><span className="muted">（无法识别本次变更类型，请以详情页版本历史为准）</span></div>;
  }
  if (versions === null) {
    return <div style={{ marginBottom: 12 }}><Spin size="small" /> <span className="muted">正在识别本次变更…</span></div>;
  }
  const info = buildChangeInfo(metric, versions);
  return (
    <div style={{ marginBottom: 12 }}>
      <Space size={8} wrap>
        <Tag color={info.color}>{info.tag}{changeVersionText(info)}</Tag>
        {info.note ? <span className="muted" style={{ fontSize: 12 }}>{info.note}</span> : null}
      </Space>
    </div>
  );
}
