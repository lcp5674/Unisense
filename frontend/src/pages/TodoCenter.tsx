import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Card, Button, List, Tag, Space, Alert, message } from "antd";
import { fetchCurrentUser, listConflicts, listMetrics, listQualityEvents, UnisenseApiError } from "../api";
import { usePermission } from "../hooks/usePermission";
import { useTracking } from "../hooks/useTracking";
import { parseBackendTime } from "../utils/timeCn";

const CONFLICT_TYPE_LABEL: Record<string, string> = {
  same_name_diff_def: "同名不同义",
  same_def_diff_name: "同义不同名",
  grain_unit: "粒度/单位冲突",
  cross_domain_same_def: "跨域同口径异源",
  version_conflict: "口径版本冲突",
  pii: "PII 冲突",
};

// DRAFT 超期治理（生产就绪审查 P2）：草稿创建超该天数仍未完善/发布 → 待办标记
// "已超期"（治理提醒：长期滞留 DRAFT 占用编码且口径可能失活，需完善或清理）
const DRAFT_STALE_DAYS = 30;

interface Todo {
  kind: "conflict" | "draft" | "review" | "quality" | "dsd";
  title: string;
  meta: string;
  code?: string;
}

// 各类待办的中文名 / 标签色 / 跳转目标 / 操作按钮文案（生产业务术语）
const KIND_META: Record<Todo["kind"], { label: string; color: string; action: string; target: (t: Todo) => string }> = {  conflict: {
    label: "冲突",
    color: "red",
    action: "去仲裁",
    target: () => "/approval?tab=conflict",
  },
  draft: {
    label: "草稿",
    color: "blue",
    action: "查看",
    target: (t) => `/detail/${t.code}`,
  },
  review: {
    label: "待审核",
    color: "gold",
    action: "去审核",
    target: () => "/approval?tab=metrics",
  },
  quality: {
    label: "质量告警",
    color: "orange",
    action: "去处理",
    target: () => "/quality",
  },
  dsd: {
    label: "数据源下线",
    color: "purple",
    action: "去恢复",
    target: (t) => `/detail/${t.code}`,
  },
};

// F3：超 50 条时「查看全部」跳转目标（draft/dsd 单条 target 依赖 code，此处走列表页）
const OVERFLOW_TARGET: Record<Todo["kind"], string> = {
  conflict: "/approval?tab=conflict",
  draft: "/metrics?status=DRAFT",
  review: "/approval?tab=metrics",
  quality: "/quality",
  dsd: "/metrics?status=DATA_SOURCE_DROPPED",
};

export function TodoCenter() {
  const [todos, setTodos] = useState<Todo[]>([]);
  const [loading, setLoading] = useState(false);
  // F3（审查修复）：每类只取前 50 条，计数标签此前按已加载条数统计（只统计前 50，
  // 超 50 后运维误判无积压）。现计数走后端 total，超限给「查看全部」入口。
  const [totals, setTotals] = useState<Record<string, number>>({});
  const navigate = useNavigate();
  const { track } = useTracking();
  // P3（审查修复）：无审批权限的用户不展示「指标待审核」待办（避免点入
  // 审批中心只见空态的死链式体验）
  const { can } = usePermission();

  async function load() {
    setLoading(true);
    try {
      // DSD 待办按当前登录用户的 Owner 维度收敛（源表下线 7 天处理期）
      const me = await fetchCurrentUser();
      const canReview = can("metric:review");
      // 草稿待办按职责收敛：平台/域管理员治理全域草稿；metric_owner/普通用户
      // 只看「自己负责的草稿」（owner_id=me.id）——避免把他人跨域草稿列进待办，
      // 点入后 Owner 责任链/快照被 PDP 拒绝（403）的断链体验。
      // domain_admin 须绑定业务域才视为治理管理员——未绑定域（domain 为空）的
      // 域管理员无治理范围，退化为个人视角（与后端 visibility 收敛一致）。
      const isGovernanceAdmin =
        me.role === "platform_admin" || (me.role === "domain_admin" && !!me.domain);
      const draftReq = isGovernanceAdmin
        ? { status: "DRAFT", page_size: 50 }
        : { status: "DRAFT", owner_id: me.id, page_size: 50 };
      const [conflicts, drafts, reviews, qualityAlerts, dropped] = await Promise.all([
        // 个人工作台收敛：非治理角色只看「与我相关的冲突」（冲突任一指标 Owner/副 Owner
        // 或本人仲裁），避免把全平台 OPEN 冲突（他人指标）混入个人待办；治理管理员
        // 保持全域列表（治理工作台视角）。
        listConflicts({
          status: "OPEN",
          page_size: 50,
          related_only: isGovernanceAdmin ? undefined : true,
        }),
        listMetrics(draftReq),
        canReview ? listMetrics({ status: "REVIEW", page_size: 50 }) : Promise.resolve({ items: [], total: 0 }),
        // 个人工作台收敛：非治理角色只看本人名下指标的质量告警（后端按 Owner/副 Owner
        // 过滤），避免本域他人指标告警混入；治理管理员按域收敛保持现状。
        listQualityEvents({
          status: "OPEN",
          page_size: 50,
          mine_only: isGovernanceAdmin ? undefined : true,
        }),
        listMetrics({ status: "DATA_SOURCE_DROPPED", owner_id: me.id, page_size: 50 }),
      ]);
      const list: Todo[] = [];
      for (const c of conflicts.items) {
        list.push({
          kind: "conflict",
          title: `冲突待仲裁：${c.candidate_metric_code} vs ${c.existing_metric_code}`,
          meta: `${CONFLICT_TYPE_LABEL[c.type] ?? c.type} · ${c.status} · ${c.conflict_id}`,
        });
      }
      for (const m of drafts.items) {
        // TZ（审查）：后端 created_at 为 naive UTC，按 UTC 解析后再判超期——
        // 原 new Date(naive) 当北京本地，超期判定最多提前 8 小时误报。
        const created = parseBackendTime(m.created_at);
        const stale =
          created != null && created.getTime() < Date.now() - DRAFT_STALE_DAYS * 86400000;
        list.push({
          kind: "draft",
          title: `草稿待完善/发布：${m.name}`,
          meta: `${m.metric_code} · ${m.domain}${stale ? ` · 已超期（创建超 ${DRAFT_STALE_DAYS} 天）` : ""}`,
          code: m.metric_code,
        });
      }
      for (const m of reviews.items) {
        // 语义区分：reviewer 角色的「待审核」= 需要本人审核的指标；metric_owner 等
        // 非评审角色看到的 REVIEW 列表是「自己提交、待平台/域管理员审核」的指标
        // （后端对非 reviewer 已按 owner 收敛，不会混入他人指标）。
        const isReviewer = me.role === "reviewer";
        list.push({
          kind: "review",
          title: isReviewer ? `指标待审核：${m.name}` : `我提交的审核中：${m.name}`,
          meta: `${m.metric_code} · ${m.domain}${isReviewer ? "" : " · 待平台/域管理员审核"}`,
          code: m.metric_code,
        });
      }
      for (const q of qualityAlerts.items) {
        list.push({
          kind: "quality",
          title: `质量告警待处理：${q.rule_type}（${q.level}）`,
          meta: `事件 #${q.id} · 指标 #${q.metric_id} · ${q.status}`,
        });
      }
      for (const m of dropped.items) {
        list.push({
          kind: "dsd",
          title: `数据源下线待处理：${m.name}`,
          meta: `${m.metric_code} · ${m.domain} · 7 天内恢复或确认退役`,
          code: m.metric_code,
        });
      }
      setTodos(list);
      setTotals({
        conflict: conflicts.total ?? conflicts.items.length,
        draft: drafts.total ?? drafts.items.length,
        review: reviews.total ?? reviews.items.length,
        quality: qualityAlerts.total ?? qualityAlerts.items.length,
        dsd: dropped.total ?? dropped.items.length,
      });
      track("todo_center_view", undefined, "todo");
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败",
      );
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 各类计数：走后端 total（F3：不再用已加载条数，避免超 50 时误导）
  const countByKind = (kind: Todo["kind"]) => totals[kind] ?? 0;
  // 每类最多加载条数（超出给「查看全部」入口）
  const TODO_PAGE_SIZE = 50;
  // 有超限类时展示的提示：列出超限类及查看全部入口
  const overflowKinds = (Object.keys(KIND_META) as unknown as Todo["kind"][]).filter(
    (k) => (totals[k] ?? 0) > TODO_PAGE_SIZE,
  );

  return (
    <div>
      <Card title="待办中心">
      <Space size={[8, 8]} wrap style={{ marginBottom: 16 }}>
        {(["conflict", "draft", "review", "quality", "dsd"] as const).map((kind) => (
          <Tag key={kind} color={KIND_META[kind].color} data-testid={`todo-count-${kind}`}>
            {KIND_META[kind].label} {countByKind(kind)}
          </Tag>
        ))}
      </Space>
      {/* F3：超 50 条时给出「查看全部」入口，避免积压不可达 */}
      {overflowKinds.length > 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="部分待办较多，当前仅展示每类前 50 条"
          description={overflowKinds.map((k) => `${KIND_META[k].label}共 ${totals[k]} 条`).join("；")}
          action={
            <Button
              size="small"
              onClick={() => {
                const first = overflowKinds[0];
                navigate(OVERFLOW_TARGET[first], { state: { from: "todo" } });
              }}
            >
              前往查看
            </Button>
          }
        />
      )}
      <List
        loading={loading}
        dataSource={todos}
        locale={{ emptyText: "暂无待办" }}
        renderItem={(t) => {
          const target = KIND_META[t.kind].target(t);
          return (
          <List.Item
            style={{ cursor: "pointer" }}
            onClick={() => {
              // 整行可点击：传 from="todo" 让目标页（如 MetricDetail）知道来源，便于返回
              navigate(target, { state: { from: "todo" } });
            }}
            data-testid={`todo-item-${t.kind}`}
            actions={[
              <Button
                type="link"
                key="open"
                onClick={(e) => {
                  // 按钮点击时不触发整行导航（避免双重跳转）
                  e.stopPropagation();
                  navigate(target, { state: { from: "todo" } });
                }}
              >
                {KIND_META[t.kind].action}
              </Button>,
            ]}
          >
            <List.Item.Meta
              avatar={<Tag color={KIND_META[t.kind].color}>{KIND_META[t.kind].label}</Tag>}
              title={t.title}
              description={t.meta}
            />
          </List.Item>
          );
        }}
      />
      </Card>
    </div>
  );
}
