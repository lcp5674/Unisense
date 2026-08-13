import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Card, Button, List, Tag, message } from "antd";
import { listConflicts, listMetrics, UnisenseApiError } from "../api";
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
  kind: "conflict" | "draft";
  title: string;
  meta: string;
  code?: string;
}

export function TodoCenter() {
  const [todos, setTodos] = useState<Todo[]>([]);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const { track } = useTracking();

  async function load() {
    setLoading(true);
    try {
      const [conflicts, drafts] = await Promise.all([
        listConflicts({ status: "OPEN", page_size: 50 }),
        listMetrics({ status: "DRAFT", page_size: 50 }),
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

  return (
    <Card title="待办中心">
      <List
        loading={loading}
        dataSource={todos}
        locale={{ emptyText: "暂无待办" }}
        renderItem={(t) => (
          <List.Item
            actions={
              t.code
                ? [
                    <Button
                      type="link"
                      key="open"
                      onClick={() => navigate(`/detail/${t.code}`)}
                    >
                      查看
                    </Button>,
                  ]
                : []
            }
          >
            <List.Item.Meta
              avatar={
                <Tag color={t.kind === "conflict" ? "red" : "blue"}>
                  {t.kind === "conflict" ? "冲突" : "草稿"}
                </Tag>
              }
              title={t.title}
              description={t.meta}
            />
          </List.Item>
        )}
      />
    </Card>
  );
}
