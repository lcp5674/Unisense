import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Card,
  Empty,
  Progress,
  Statistic,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Spin,
  Row,
  Col,
  message,
} from "antd";
import { InfoCircleOutlined } from "@ant-design/icons";
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
  MetricConsistencyStats,
} from "../types";
import {
  METRIC_TYPE_LABEL,
  METRIC_STATUS_LABEL,
  enumLabel,
} from "../utils/enums";

/** 指标运营分析：复用度 / 资产账本 / 口径一致率（后端 stats/reuse、stats/ledger、consistency/stats）。 */

/** 口径一致率统计口径说明（对齐后端 ConflictRepository.consistency_stats）。 */
const CONSISTENCY_TIPS =
  "口径一致率 = 未卷入口径冲突的指标数 ÷ 指标总数 × 100%。" +
  "卷入冲突指标数取自全部未删除冲突记录（candidate/existing 去重）；" +
  "部门间冲突指冲突双方业务域不同的记录；" +
  "平均解决时长为已解决冲突 (resolved_at − created_at) 的小时均值。";

/** 复用度分桶定义（用于分布统计与着色）。 */
const REUSE_BUCKETS = [
  { key: "zero", label: "零复用", min: 0, max: 0, color: "#cf1322" },
  { key: "low", label: "低复用（1-2）", min: 1, max: 2, color: "#d46b08" },
  { key: "mid", label: "中复用（3-5）", min: 3, max: 5, color: "#1677ff" },
  { key: "high", label: "高复用（>5）", min: 6, max: Number.MAX_SAFE_INTEGER, color: "#389e0d" },
];

/** 统一表格分页：可跳页码、可改条数、显示总数。
 *  注意：必须用 defaultPageSize（非受控）而非 pageSize（受控）——
 *  受控 pageSize 缺少 onShowSizeChange 同步时，切换条数会被重置回默认值。 */
export function tablePagination(total: number) {
  return {
    defaultPageSize: 10,
    showSizeChanger: true,
    pageSizeOptions: ["10", "20", "50", "100"],
    showQuickJumper: true,
    showTotal: (t: number) => `共 ${t} 条`,
    total,
  };
}

/** 指标运营分析：复用度 / 资产账本 / 口径一致率（后端 stats/reuse、stats/ledger、consistency/stats）。 */
export function MetricOps() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [reuse, setReuse] = useState<MetricReuseStats | null>(null);
  const [ledger, setLedger] = useState<MetricLedgerStats | null>(null);
  const [consistency, setConsistency] = useState<MetricConsistencyStats | null>(null);

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

  /** 复用度分桶分布（零/低/中/高复用指标数）。 */
  const reuseDistribution = useMemo(() => {
    const items = reuse?.items ?? [];
    return REUSE_BUCKETS.map((b) => ({
      ...b,
      count: items.filter((it) => it.reuse_count >= b.min && it.reuse_count <= b.max).length,
    }));
  }, [reuse]);

  /** 口径一致率字段（对齐后端 consistency_stats 返回）。 */
  const consTotal = consistency?.total_definitions ?? 0;
  const consRate = consistency?.consistency_rate_pct ?? 0;
  const consConflicted = consistency?.conflicted_metrics ?? 0;
  const consConflicts = consistency?.total_conflicts ?? 0;
  const consCrossDept = consistency?.cross_department_conflicts ?? 0;
  const consAvgHours = consistency?.avg_resolve_hours ?? 0;
  const rateColor = consRate >= 90 ? "#52c41a" : consRate >= 70 ? "#faad14" : "#cf1322";

  const metricLink = (code: string, name?: string | null) => (
    <a onClick={() => navigate(`/detail/${encodeURIComponent(code)}`)}>
      {code}
      {name ? <span className="muted" style={{ marginLeft: 8 }}>{name}</span> : null}
    </a>
  );

  const reuseCols: ColumnsType<MetricReuseItem> = [
    {
      title: "指标",
      key: "metric",
      render: (_, r) => metricLink(r.metric_code, r.name),
    },
    { title: "域", dataIndex: "domain", width: 110, render: (v: string | null) => (v ? <Tag>{v}</Tag> : "—") },
    { title: "类型", dataIndex: "type", width: 100, render: (v: string) => enumLabel(METRIC_TYPE_LABEL, v) || v },
    { title: "状态", dataIndex: "status", width: 90, render: (v: string) => enumLabel(METRIC_STATUS_LABEL, v) || v },
    { title: "被派生引用", dataIndex: "derived_by_count", width: 100 },
    { title: "被消费引用", dataIndex: "consumed_by_count", width: 100 },
    {
      title: "总复用度",
      dataIndex: "reuse_count",
      width: 120,
      sorter: (a, b) => a.reuse_count - b.reuse_count,
      render: (v: number) => (v === 0 ? <Tag color="red">0（零复用）</Tag> : <Tag color="green">{v}</Tag>),
    },
  ];

  const zombieCols: ColumnsType<MetricLedgerZombieItem> = [
    {
      title: "指标",
      key: "metric",
      render: (_, r) => metricLink(r.metric_code, r.name),
    },
    { title: "域", dataIndex: "domain", width: 110, render: (v: string | null) => (v ? <Tag>{v}</Tag> : "—") },
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

  return (
    <div>
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={4}>
          <Card size="small">
            <Tooltip title="平台全部未删除指标数">
              <Statistic title="指标总数" value={ledger?.total ?? reuse?.total ?? 0} />
            </Tooltip>
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Tooltip title="近 30 天有更新或被引用的指标">
              <Statistic title="活跃指标" value={ledger?.active_count ?? 0} />
            </Tooltip>
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Tooltip title="近 30 天无更新且零引用的指标（存在下线/治理风险）">
              <Statistic
                title="僵尸指标"
                value={ledger?.zombie_count ?? 0}
                valueStyle={{ color: ledger && ledger.zombie_count > 0 ? "#cf1322" : undefined }}
              />
            </Tooltip>
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Tooltip title="命中「同名不同定义」冲突预检信号的指标（建议合并治理）">
              <Statistic
                title="重复建设"
                value={ledger?.duplicate_count ?? 0}
                valueStyle={{ color: ledger && ledger.duplicate_count > 0 ? "#d46b08" : undefined }}
              />
            </Tooltip>
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Tooltip title="至少被一个派生指标或消费报表引用的指标（核心资产）">
              <Statistic title="被引用指标" value={reuse?.referenced ?? 0} />
            </Tooltip>
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Tooltip title="没有任何派生/消费引用的指标（高僵尸风险）">
              <Statistic
                title="零复用指标"
                value={reuse?.zero_reuse ?? 0}
                valueStyle={{ color: reuse && reuse.zero_reuse > 0 ? "#cf1322" : undefined }}
              />
            </Tooltip>
          </Card>
        </Col>
      </Row>

      <Card
        size="small"
        style={{ marginBottom: 16 }}
        title={
          <span>
            口径一致率
            <Tooltip title={CONSISTENCY_TIPS}>
              <InfoCircleOutlined style={{ marginLeft: 6, color: "#8c8c8c" }} />
            </Tooltip>
          </span>
        }
      >
        {consistency ? (
          <div>
            <Row gutter={16} align="middle">
              <Col span={4} style={{ textAlign: "center" }}>
                <Progress type="circle" percent={consRate} size={96} strokeColor={rateColor} />
                <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>口径一致率</div>
              </Col>
              <Col span={4}><Statistic title="参与统计指标" value={consTotal} /></Col>
              <Col span={4}><Statistic title="卷入冲突指标" value={consConflicted} /></Col>
              <Col span={4}><Statistic title="冲突记录数" value={consConflicts} /></Col>
              <Col span={4}>
                <Statistic
                  title="部门间冲突"
                  value={consCrossDept}
                  valueStyle={{ color: consCrossDept > 0 ? "#cf1322" : undefined }}
                />
              </Col>
              <Col span={4}><Statistic title="平均解决时长" value={consAvgHours} suffix="小时" /></Col>
            </Row>
            {consCrossDept > 0 && (
              <Alert
                style={{ marginTop: 12 }}
                type="warning"
                showIcon
                message={`存在 ${consCrossDept} 起部门间口径冲突，建议优先治理跨域一致性问题`}
              />
            )}
          </div>
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
                <div>
                  <Row gutter={16} style={{ marginBottom: 12 }}>
                    {reuseDistribution.map((b) => (
                      <Col span={6} key={b.key}>
                        <Card size="small">
                          <Statistic title={b.label} value={b.count} valueStyle={{ color: b.color }} />
                        </Card>
                      </Col>
                    ))}
                  </Row>
                  <Table
                    rowKey="metric_code"
                    size="small"
                    dataSource={reuse?.items ?? []}
                    columns={reuseCols}
                    pagination={tablePagination(reuse?.items.length ?? 0)}
                    locale={{ emptyText: "暂无复用度数据" }}
                  />
                </div>
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
                  pagination={tablePagination(ledger?.zombies.length ?? 0)}
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
                  pagination={tablePagination(ledger?.duplicates.length ?? 0)}
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
