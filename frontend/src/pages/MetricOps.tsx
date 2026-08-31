import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Card,
  Empty,
  Statistic,
  Table,
  Tabs,
  Spin,
  Row,
  Col,
  message,
} from "antd";
import { useNavigate } from "react-router-dom";
import type { ColumnsType } from "antd/es/table";
import {
  fetchMetricReuseStats,
  fetchMetricLedger,
  fetchConsistencyStats,
} from "../api";
import type {
  MetricReuseStats,
  MetricReuseItem,
  MetricLedgerStats,
  MetricLedgerZombieItem,
  MetricLedgerDuplicateItem,
} from "../types";
import {
  METRIC_TYPE_LABEL,
  METRIC_STATUS_LABEL,
  enumLabel,
} from "../utils/enums";

/** 指标运营分析：复用度 / 资产账本 / 口径一致率（后端已实现、此前前端无入口的三个统计端点）。 */
export function MetricOps() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [reuse, setReuse] = useState<MetricReuseStats | null>(null);
  const [ledger, setLedger] = useState<MetricLedgerStats | null>(null);
  const [consistency, setConsistency] = useState<Record<string, unknown> | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [r, l, c] = await Promise.all([
        fetchMetricReuseStats(),
        fetchMetricLedger(),
        fetchConsistencyStats(),
      ]);
      setReuse(r);
      setLedger(l);
      setConsistency(c);
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : "加载指标运营分析失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const reuseCols: ColumnsType<MetricReuseItem> = [
    {
      title: "指标",
      key: "metric",
      render: (_, r) => (
        <a onClick={() => navigate(`/detail/${encodeURIComponent(r.metric_code)}`)}>
          {r.metric_code}
          <span className="muted" style={{ marginLeft: 8 }}>{r.name}</span>
        </a>
      ),
    },
    { title: "域", dataIndex: "domain", width: 100, render: (v: string | null) => v || "—" },
    { title: "类型", dataIndex: "type", width: 100, render: (v: string) => enumLabel(METRIC_TYPE_LABEL, v) || v },
    { title: "状态", dataIndex: "status", width: 90, render: (v: string) => enumLabel(METRIC_STATUS_LABEL, v) || v },
    { title: "被派生引用", dataIndex: "derived_by_count", width: 100 },
    { title: "被消费引用", dataIndex: "consumed_by_count", width: 100 },
    { title: "总复用度", dataIndex: "reuse_count", width: 100, sorter: (a, b) => a.reuse_count - b.reuse_count },
  ];

  const zombieCols: ColumnsType<MetricLedgerZombieItem> = [
    {
      title: "指标",
      key: "metric",
      render: (_, r) => (
        <a onClick={() => navigate(`/detail/${encodeURIComponent(r.metric_code)}`)}>
          {r.metric_code}
          <span className="muted" style={{ marginLeft: 8 }}>{r.name}</span>
        </a>
      ),
    },
    { title: "域", dataIndex: "domain", width: 100, render: (v: string | null) => v || "—" },
    { title: "状态", dataIndex: "status", width: 90, render: (v: string) => enumLabel(METRIC_STATUS_LABEL, v) || v },
    { title: "距上次更新", dataIndex: "days_since_update", width: 110, render: (v: number | null) => (v == null ? "—" : `${v} 天`) },
    { title: "被派生引用", dataIndex: "derived_by_count", width: 100 },
    { title: "被消费引用", dataIndex: "consumed_by_count", width: 100 },
    { title: "总复用度", dataIndex: "reuse_count", width: 100 },
  ];

  const dupCols: ColumnsType<MetricLedgerDuplicateItem> = [
    { title: "指标", dataIndex: "metric_code", width: 200 },
    { title: "名称", dataIndex: "name", width: 180 },
    { title: "相似度", dataIndex: "conflict_score", width: 90, render: (v: number | null) => (v == null ? "—" : `${Math.round(v * 100)}%`) },
    { title: "实质相同指标", dataIndex: "existing_code", width: 200 },
    { title: "冲突预检原因", dataIndex: "reason", render: (v: string | null) => v || "—" },
  ];

  const consTotal = Number(consistency?.total ?? 0);
  const consRate = Number(consistency?.consistency_rate ?? consistency?.rate ?? 0);

  return (
    <div>
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Card size="small">
            <Statistic title="指标总数" value={ledger?.total ?? reuse?.total ?? 0} />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic title="活跃指标" value={ledger?.active_count ?? 0} />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic title="僵尸指标" value={ledger?.zombie_count ?? 0} valueStyle={{ color: ledger && ledger.zombie_count > 0 ? "#cf1322" : undefined }} />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic title="重复建设" value={ledger?.duplicate_count ?? 0} valueStyle={{ color: ledger && ledger.duplicate_count > 0 ? "#d46b08" : undefined }} />
          </Card>
        </Col>
      </Row>

      <Card size="small" title="口径一致率" style={{ marginBottom: 16 }}>
        {consistency ? (
          <Row gutter={16}>
            <Col span={6}><Statistic title="参与统计指标" value={consTotal} /></Col>
            <Col span={6}><Statistic title="口径一致率" value={consRate} suffix="%" /></Col>
            <Col span={12}>
              {Number(consistency?.department_conflicts ?? 0) > 0 && (
                <Alert type="warning" showIcon message={`存在 ${consistency?.department_conflicts} 起部门间口径冲突`} />
              )}
            </Col>
          </Row>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无一致率数据" />
        )}
      </Card>

      <Spin spinning={loading}>
        <Tabs
          items={[
            {
              key: "reuse",
              label: `复用度分析（${reuse?.items.length ?? 0}）`,
              children: (
                <Table
                  rowKey="metric_code"
                  size="small"
                  dataSource={reuse?.items ?? []}
                  columns={reuseCols}
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  locale={{ emptyText: "暂无复用度数据" }}
                />
              ),
            },
            {
              key: "zombie",
              label: `僵尸指标（${ledger?.zombies.length ?? 0}）`,
              children: (
                <Table
                  rowKey="metric_code"
                  size="small"
                  dataSource={ledger?.zombies ?? []}
                  columns={zombieCols}
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  locale={{ emptyText: ledger && ledger.zombie_count > 0 ? "暂无" : "无僵尸指标，健康" }}
                />
              ),
            },
            {
              key: "duplicate",
              label: `重复建设（${ledger?.duplicates.length ?? 0}）`,
              children: (
                <Table
                  rowKey="metric_code"
                  size="small"
                  dataSource={ledger?.duplicates ?? []}
                  columns={dupCols}
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  locale={{ emptyText: "无重复建设信号" }}
                />
              ),
            },
          ]}
        />
      </Spin>
    </div>
  );
}
