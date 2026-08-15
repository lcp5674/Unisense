import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Card, Button, List, Tag, Space, message } from "antd";
import { listConflicts, listMetrics, listQualityEvents, UnisenseApiError } from "../api";
import { useTracking } from "../hooks/useTracking";

const CONFLICT_TYPE_LABEL: Record<string, string> = {
  same_name_diff_def: "同名不同义",
  same_def_diff_name: "同义不同名",
  grain_unit: "粒度/单位冲突",
  cross_domain_same_def: "跨域同口径异源",
  version_conflict: "口径版本冲突",
  pii: "PII 冲突",
};

interface Todo {
  kind: "conflict" | "draft" | "review" | "quality";
  title: string;
  meta: string;
  code?: string;
}

// 各类待办的中文名 / 标签色 / 跳转目标 / 操作按钮文案（生产业务术语）
const KIND_META: Record<Todo["kind"], { label: string; color: string; action: string; target: (t: Todo) => string }> = {
  conflict: {
    label: "冲突",
    color: "red",
    action: "去仲裁",
    target: () => "/review",
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
    target: () => "/metrics/review",
  },
  quality: {
    label: "质量告警",
    color: "orange",
    action: "去处理",
    target: () => "/quality",
  },
};

export function TodoCenter() {
  const [todos, setTodos] = useState<Todo[]>([]);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const { track } = useTracking();

  async function load() {
    setLoading(true);
    try {
      const [conflicts, drafts, reviews, qualityAlerts] = await Promise.all([
        listConflicts({ status: "OPEN", page_size: 50 }),
        listMetrics({ status: "DRAFT", page_size: 50 }),
        listMetrics({ status: "REVIEW", page_size: 50 }),
        listQualityEvents({ status: "OPEN", page_size: 50 }),
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
        list.push({
          kind: "draft",
          title: `草稿待完善/发布：${m.name}`,
          meta: `${m.metric_code} · ${m.domain}`,
          code: m.metric_code,
        });
      }
      for (const m of reviews.items) {
        list.push({
          kind: "review",
          title: `指标待审核：${m.name}`,
          meta: `${m.metric_code} · ${m.domain}`,
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
      setTodos(list);
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

  // 各类计数（用于分类汇总展示）
  const countByKind = (kind: Todo["kind"]) => todos.filter((t) => t.kind === kind).length;

  return (
    <Card title="待办中心">
      <Space size={[8, 8]} wrap style={{ marginBottom: 16 }}>
        {(["conflict", "draft", "review", "quality"] as const).map((kind) => (
          <Tag key={kind} color={KIND_META[kind].color} data-testid={`todo-count-${kind}`}>
            {KIND_META[kind].label} {countByKind(kind)}
          </Tag>
        ))}
      </Space>
      <List
        loading={loading}
        dataSource={todos}
        locale={{ emptyText: "暂无待办" }}
        renderItem={(t) => (
          <List.Item
            actions={[
              <Button
                type="link"
                key="open"
                onClick={() => navigate(KIND_META[t.kind].target(t))}
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
        )}
      />
    </Card>
  );
}
