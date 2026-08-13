import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { Card, Spin, Alert, Descriptions, Tag, Empty, Space } from "antd";
import { InfoCircleOutlined, WarningOutlined, LinkOutlined } from "@ant-design/icons";
import { fetchConsumptionGuide, getMetric } from "../api";
import type { ConsumptionGuideResponse, MetricResponse } from "../types";
import { useTracking } from "../hooks/useTracking";
import { ObjectView, DEF_FIELD_LABEL } from "../utils/display";
import { enumLabel, METRIC_TYPE_LABEL, AGGREGATION_LABEL, TIME_SEMANTICS_LABEL, SERVING_MODE_LABEL } from "../utils/enums";

export function ConsumptionGuide() {
  const { metricCode } = useParams<{ metricCode: string }>();
  const navigate = useNavigate();
  const [guide, setGuide] = useState<ConsumptionGuideResponse | null>(null);
  const [metric, setMetric] = useState<MetricResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { track } = useTracking();

  useEffect(() => {
    if (!metricCode) return;

    async function load() {
      setLoading(true);
      setError(null);
      try {
        const [g, m] = await Promise.all([
          fetchConsumptionGuide(metricCode!),
          getMetric(metricCode!).catch(() => null),
        ]);
        setGuide(g);
        setMetric(m);
        track("consumption_guide_view", metricCode, "metric");
      } catch (err) {
        setError(err instanceof Error ? err.message : "加载消费指南失败");
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [metricCode, track]);

  if (loading) {
    return (
      <div style={{ textAlign: "center", padding: 48 }}>
        <Spin size="large" tip="加载消费指南…" />
      </div>
    );
  }

  if (error) return <Alert type="error" message="加载失败" description={error} showIcon />;
  if (!guide || !metricCode) return null;

  const metricFields = metric ?? {
    metric_code: guide.metric_code,
    name: guide.name,
    domain: guide.domain,
    type: guide.type,
    granularity: guide.granularity,
    unit: guide.unit,
    aggregation: guide.aggregation,
    time_semantics: guide.time_semantics,
    serving_mode: guide.serving_mode,
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Consumption / Guide</div>
          <h2>消费指南 — <span className="mono">{metricCode}</span></h2>
          <p>推荐的查询方式、注意事项与关联指标——基于指标语义自动生成。</p>
        </div>
        <Space>
          <Tag color="orange">{guide.domain}</Tag>
          <Tag>{enumLabel(METRIC_TYPE_LABEL, guide.type)}</Tag>
          <Tag>{enumLabel(SERVING_MODE_LABEL, guide.serving_mode)}</Tag>
        </Space>
      </div>

      <Card title="指标基本信息" style={{ marginBottom: 20 }}>
        <Descriptions column={3} bordered size="small">
          <Descriptions.Item label="编码">{metricFields.metric_code}</Descriptions.Item>
          <Descriptions.Item label="名称">{metricFields.name}</Descriptions.Item>
          <Descriptions.Item label="域">{metricFields.domain}</Descriptions.Item>
          <Descriptions.Item label="类型">{enumLabel(METRIC_TYPE_LABEL, metricFields.type)}</Descriptions.Item>
          <Descriptions.Item label="粒度">{metricFields.granularity}</Descriptions.Item>
          <Descriptions.Item label="单位">{metricFields.unit}</Descriptions.Item>
          <Descriptions.Item label="聚合">{enumLabel(AGGREGATION_LABEL, metricFields.aggregation)}</Descriptions.Item>
          <Descriptions.Item label="时间语义">{enumLabel(TIME_SEMANTICS_LABEL, metricFields.time_semantics)}</Descriptions.Item>
          <Descriptions.Item label="服务模式">{enumLabel(SERVING_MODE_LABEL, metricFields.serving_mode)}</Descriptions.Item>
        </Descriptions>
      </Card>

      <div className="grid-2" style={{ marginBottom: 20 }}>
        <Card title={<span><InfoCircleOutlined /> 推荐使用方式</span>}>
          {guide.recommended_usage && guide.recommended_usage.length > 0 ? (
            <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 2 }}>
              {guide.recommended_usage.map((u, i) => (
                <li key={i}>{u}</li>
              ))}
            </ul>
          ) : (
            <Empty description="暂无推荐用法" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          )}
        </Card>
        <Card title={<span><WarningOutlined /> 注意事项</span>}>
          {guide.cautions && guide.cautions.length > 0 ? (
            <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 2 }}>
              {guide.cautions.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          ) : (
            <Empty description="无特殊注意事项" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          )}
        </Card>
      </div>

      <Card
        title={<span><LinkOutlined /> 关联指标</span>}
        extra={<a onClick={() => navigate(`/detail/${metricCode}`)}>查看完整定义</a>}
      >
        {guide.related_metrics && guide.related_metrics.length > 0 ? (
          <Space wrap>
            {guide.related_metrics.map((code) => (
              <Tag key={code} style={{ cursor: "pointer", padding: "4px 12px" }} onClick={() => navigate(`/detail/${code}`)}>
                {code}
              </Tag>
            ))}
          </Space>
        ) : (
          <Empty description="暂无关联指标" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        )}
      </Card>

      {metric && metric.definition_json && Object.keys(metric.definition_json).length > 0 && (
        <Card title="口径定义" style={{ marginTop: 20 }}>
          <ObjectView data={metric.definition_json} labels={DEF_FIELD_LABEL} />
        </Card>
      )}
    </div>
  );
}
