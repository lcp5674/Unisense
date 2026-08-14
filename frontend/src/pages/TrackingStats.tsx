import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Empty,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
} from "antd";
import { Bar } from "@ant-design/charts";
import { ReloadOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import { fetchTrackingStats, listUsers } from "../api";
import type { TrackingGroupBy, TrackingStatsResponse, TrackingStatsRow, UserBrief } from "../types";
import { TRACKING_EVENT_LABEL, TRACKING_TARGET_LABEL } from "../utils/enums";

// 分组字段 → 中文标签（对齐后端 tracking.py _GROUP_BY_ALLOWED 白名单）
const GROUP_BY_LABEL: Record<TrackingGroupBy, string> = {
  event_type: "事件类型",
  target_type: "目标类型",
  actor_id: "操作用户",
};

// 事件类型筛选项：已知事件全量业务标签（未收录值保留原值供展示兜底）
const EVENT_OPTIONS = Object.entries(TRACKING_EVENT_LABEL)
  .map(([value, label]) => ({ value, label }))
  .sort((a, b) => a.label.localeCompare(b.label, "zh"));

function groupLabel(key: string, groupBy: TrackingGroupBy, userMap: Record<string, string>): string {
  if (groupBy === "event_type") return TRACKING_EVENT_LABEL[key] ?? key;
  if (groupBy === "target_type") return TRACKING_TARGET_LABEL[key] ?? key;
  if (groupBy === "actor_id") return userMap[key] ?? key;
  return key;
}

const TABLE_COLUMNS: ColumnsType<TrackingStatsRow> = [
  { title: "分组", dataIndex: "group_key", key: "group_key", ellipsis: true },
  {
    title: "事件数",
    dataIndex: "event_count",
    key: "event_count",
    align: "right",
    sorter: (a, b) => a.event_count - b.event_count,
    render: (v: number) => <span style={{ fontWeight: 600 }}>{v}</span>,
  },
  {
    title: "去重用户数",
    dataIndex: "unique_actors",
    key: "unique_actors",
    align: "right",
    sorter: (a, b) => a.unique_actors - b.unique_actors,
    render: (v: number) => <Tag color="blue">{v}</Tag>,
  },
];

export function TrackingStats() {
  const [groupBy, setGroupBy] = useState<TrackingGroupBy>("event_type");
  const [eventType, setEventType] = useState<string | undefined>(undefined);
  const [startDate, setStartDate] = useState<dayjs.Dayjs | null>(null);
  const [endDate, setEndDate] = useState<dayjs.Dayjs | null>(null);
  const [data, setData] = useState<TrackingStatsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // actor_id → 用户名（业务术语化：操作用户分组不直出数字 ID）
  const [userMap, setUserMap] = useState<Record<string, string>>({});

  // 加载用户名单一次，供「操作用户」分组显示中文名（display_name 优先）
  useEffect(() => {
    listUsers()
      .then((users: UserBrief[]) => {
        const map: Record<string, string> = {};
        for (const u of users) map[String(u.id)] = u.display_name || u.username;
        setUserMap(map);
      })
      .catch(() => {
        // 用户名单加载失败时回落显示原始 ID，不影响统计主流程
      });
  }, []);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchTrackingStats({
        event_type: eventType?.trim() || undefined,
        start_date: startDate ? startDate.format("YYYY-MM-DD") : undefined,
        end_date: endDate ? endDate.format("YYYY-MM-DD") : undefined,
        group_by: groupBy,
      });
      setData(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载埋点统计失败");
      setData(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupBy]);

  // 汇总指标：总事件数 / 去重用户数 / 分组行数（按当前过滤结果实时计算）
  const totals = useMemo(() => {
    const rows = data?.stats ?? [];
    const eventCount = rows.reduce((a, r) => a + r.event_count, 0);
    const uniqueActors = rows.reduce((a, r) => a + r.unique_actors, 0);
    return { eventCount, uniqueActors, rows: rows.length };
  }, [data]);

  const chartData = (data?.stats ?? [])
    .filter((r) => r.event_count > 0)
    .map((r) => ({ type: groupLabel(r.group_key, groupBy, userMap), count: r.event_count }));

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Tracking / Stats</div>
          <h2>埋点统计</h2>
          <p>按事件类型 / 目标类型 / 操作用户聚合埋点上报量（需平台或域管理员权限）。</p>
        </div>
      </div>

      <Card
        size="small"
        style={{ marginBottom: 16 }}
        title="筛选条件"
        extra={
          <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
            刷新
          </Button>
        }
      >
        <Space wrap>
          <span className="muted">分组：</span>
          <Select
            style={{ width: 140 }}
            value={groupBy}
            onChange={setGroupBy}
            options={(Object.keys(GROUP_BY_LABEL) as TrackingGroupBy[]).map((k) => ({
              value: k,
              label: GROUP_BY_LABEL[k],
            }))}
          />
          <span className="muted">事件类型：</span>
          <Select
            style={{ width: 220 }}
            placeholder="全部事件类型"
            allowClear
            showSearch
            optionFilterProp="label"
            value={eventType}
            onChange={(v) => setEventType(v || undefined)}
            options={EVENT_OPTIONS}
          />
          <span className="muted">日期：</span>
          <DatePicker
            value={startDate}
            placeholder="开始日期"
            onChange={setStartDate}
            style={{ width: 140 }}
          />
          <DatePicker
            value={endDate}
            placeholder="结束日期"
            onChange={setEndDate}
            style={{ width: 140 }}
          />
          <Button type="primary" onClick={load} loading={loading}>
            查询
          </Button>
        </Space>
      </Card>

      {error && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="加载失败"
          description={error}
        />
      )}

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} md={8}>
          <Card size="small">
            <Statistic title="事件总数" value={totals.eventCount} />
          </Card>
        </Col>
        <Col xs={12} md={8}>
          <Card size="small">
            <Statistic title="去重用户数" value={totals.uniqueActors} />
          </Card>
        </Col>
        <Col xs={12} md={8}>
          <Card size="small">
            <Statistic title={`分组数（按${GROUP_BY_LABEL[groupBy]}）`} value={totals.rows} />
          </Card>
        </Col>
      </Row>

      {loading ? (
        <Card size="small">
          <div style={{ textAlign: "center", padding: "24px 0" }}>
            <Spin tip="加载埋点统计…">
              <div style={{ height: 24 }} />
            </Spin>
          </div>
        </Card>
      ) : !data || data.stats.length === 0 ? (
        <Card size="small">
          <Empty description={error ? "暂无数据" : "当前条件下暂无埋点统计数据"} />
        </Card>
      ) : (
        <>
          <Card
            size="small"
            title={`按${GROUP_BY_LABEL[groupBy]}分布`}
            style={{ marginBottom: 16 }}
          >
            {chartData.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无分布数据" />
            ) : (
              <Bar
                data={chartData}
                xField="type"
                yField="count"
                height={Math.max(240, chartData.length * 36 + 60)}
                axis={{
                  x: { title: GROUP_BY_LABEL[groupBy] },
                  y: { title: "事件数" },
                }}
                interactions={[{ type: "element-active" }] as any}
              />
            )}
          </Card>
          <Card size="small" title="明细">
            <Table
              dataSource={data.stats}
              rowKey={(r) => r.group_key}
              size="small"
              pagination={{ pageSize: 20 }}
              columns={[
                {
                  title: "分组",
                  dataIndex: "group_key",
                  key: "group_key",
                  ellipsis: true,
                  render: (v: string) => groupLabel(v, groupBy, userMap),
                },
                ...TABLE_COLUMNS.slice(1),
              ]}
            />
          </Card>
        </>
      )}
    </div>
  );
}
