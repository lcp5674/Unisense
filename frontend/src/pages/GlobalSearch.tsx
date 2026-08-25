import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Card, List, Tag, Typography, Spin, Empty, Input, Button, Space } from "antd";
import { SearchOutlined, ReloadOutlined } from "@ant-design/icons";
import { fetchGlobalSearch, UnisenseApiError } from "../api";
import type { GlobalSearchItem, GlobalSearchType } from "../types";
import { navigateToSearchItem } from "../utils/searchNavigate";

const TYPE_LABEL: Record<GlobalSearchType, string> = {
  metric: "指标",
  dimension: "维度",
  term: "术语",
  template: "模板",
  data_source: "数据源",
  catalog: "采集目录",
  field: "字段",
  subject_domain: "主题域",
  measure: "度量目录",
};

const TYPE_COLOR: Record<GlobalSearchType, string> = {
  metric: "blue",
  dimension: "purple",
  term: "geekblue",
  template: "cyan",
  data_source: "green",
  catalog: "orange",
  field: "gold",
  subject_domain: "magenta",
  measure: "volcano",
};

const TYPE_ORDER: GlobalSearchType[] = [
  "metric",
  "dimension",
  "term",
  "template",
  "data_source",
  "catalog",
  "field",
  "subject_domain",
  "measure",
];

function emptyGroups(): Record<GlobalSearchType, GlobalSearchItem[]> {
  return {
    metric: [],
    dimension: [],
    term: [],
    template: [],
    data_source: [],
    catalog: [],
    field: [],
    subject_domain: [],
    measure: [],
  };
}

/** 条目副标题：按类型展示有意义的上下文（域/源/表/敏感度等）。 */
function itemSubtitle(item: GlobalSearchItem): string {
  const parts: string[] = [];
  if (item.domain) parts.push(item.domain);
  if (item.type === "catalog" || item.type === "field") {
    if (item.source_id) parts.push(item.source_id);
    if (item.type === "field" && item.table_name) parts.push(`表：${item.table_name}`);
    if (item.sensitivity_level) parts.push(item.sensitivity_level);
  }
  if (item.type === "data_source" && item.source_type) parts.push(item.source_type);
  if (item.status) parts.push(item.status);
  return parts.join(" · ");
}

export function GlobalSearch() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const urlQ = searchParams.get("q") ?? "";
  const [q, setQ] = useState(urlQ);
  const [groups, setGroups] = useState<Record<GlobalSearchType, GlobalSearchItem[]>>(emptyGroups);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  // 记录上一次实际执行的查询，避免重复触发相同请求
  const lastQ = useRef(urlQ);

  async function load(keyword: string) {
    const trimmed = keyword.trim();
    if (!trimmed) {
      setGroups(emptyGroups());
      setTotal(0);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const res = await fetchGlobalSearch(trimmed, 10);
      setGroups(res.groups);
      setTotal(res.total);
    } catch (err) {
      setError(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "搜索失败");
      setGroups(emptyGroups());
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }

  // URL ?q= 变化时同步输入框并触发搜索
  useEffect(() => {
    if (urlQ !== lastQ.current) {
      lastQ.current = urlQ;
      setQ(urlQ);
      load(urlQ);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlQ]);

  useEffect(() => {
    load(q);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleSearch() {
    const trimmed = q.trim();
    if (!trimmed) return;
    lastQ.current = trimmed;
    navigate(`/search?q=${encodeURIComponent(trimmed)}`, { replace: true });
    load(trimmed);
  }

  const hasAny = TYPE_ORDER.some((t) => (groups[t] ?? []).length > 0);

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Search / Global</div>
          <h2>全局搜索</h2>
          <p>一次搜索覆盖指标、维度、术语、模板、数据源、采集目录表与字段、主题域。</p>
        </div>
      </div>

      <Card style={{ marginBottom: 16 }}>
        <Space>
          <Input
            prefix={<SearchOutlined style={{ color: "rgba(0,0,0,0.45)" }} />}
            placeholder="输入关键词，跨类型搜索"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onPressEnter={handleSearch}
            allowClear
            style={{ width: 360 }}
            aria-label="全局搜索关键词"
          />
          <Button type="primary" icon={<SearchOutlined />} onClick={handleSearch}>
            搜索
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => load(q)} loading={loading}>
            刷新
          </Button>
          {total > 0 && <Typography.Text type="secondary">共 {total} 条结果</Typography.Text>}
        </Space>
      </Card>

      {error && (
        <Card style={{ marginBottom: 16 }}>
          <Typography.Text type="danger">{error}</Typography.Text>
        </Card>
      )}

      {loading && !hasAny && (
        <Card>
          <div style={{ textAlign: "center", padding: 40 }}>
            <Spin />
          </div>
        </Card>
      )}

      {!loading && !error && !hasAny && (
        <Card>
          <Empty description={q.trim() ? "未找到匹配结果" : "输入关键词开始搜索"} />
        </Card>
      )}

      {hasAny &&
        TYPE_ORDER.map((type) => {
          const items = groups[type] ?? [];
          if (items.length === 0) return null;
          return (
            <Card
              key={type}
              title={
                <Space size={8}>
                  <Tag color={TYPE_COLOR[type]}>{TYPE_LABEL[type]}</Tag>
                  <span style={{ fontSize: 13, color: "rgba(0,0,0,0.45)" }}>{items.length} 条</span>
                </Space>
              }
              style={{ marginBottom: 16 }}
              styles={{ body: { paddingTop: 8, paddingBottom: 8 } }}
            >
              <List
                dataSource={items}
                renderItem={(item) => (
                  <List.Item
                    style={{ cursor: "pointer", padding: "8px 4px" }}
                    onClick={() => navigateToSearchItem(navigate, item, q)}
                  >
                    <List.Item.Meta
                      title={
                        <Space size={8}>
                          <span className="mono">{item.code}</span>
                          <Typography.Text type="secondary">{item.name}</Typography.Text>
                        </Space>
                      }
                      description={itemSubtitle(item)}
                    />
                  </List.Item>
                )}
              />
            </Card>
          );
        })}
    </div>
  );
}
