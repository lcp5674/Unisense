import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { Tabs, Card, Spin, Alert, Descriptions, Typography, Tag } from "antd";
import { fetchConsumptionGuide } from "../api";
import { useTracking } from "../hooks/useTracking";

const { Paragraph, Title } = Typography;

interface ConsumptionGuideData {
  metric_code: string;
  definition: string;
  calculation_logic: string;
  dimensions: Array<{ name: string; description: string; type: string }>;
  usage_examples: Array<{ title: string; sql: string; description: string }>;
  related_metrics: string[];
  faq: Array<{ question: string; answer: string }>;
}

export function ConsumptionGuide() {
  const { metricCode } = useParams<{ metricCode: string }>();
  const [guide, setGuide] = useState<ConsumptionGuideData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { track } = useTracking();

  useEffect(() => {
    if (!metricCode) return;

    async function load() {
      setLoading(true);
      setError(null);
      try {
        const res = await fetchConsumptionGuide(metricCode!);
        setGuide(res);
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
        <Spin size="large" tip="加载消费指南..." />
      </div>
    );
  }

  if (error) {
    return <Alert type="error" message="加载失败" description={error} showIcon />;
  }

  if (!guide || !metricCode) return null;

  const tabItems = [
    {
      key: "definition",
      label: "口径定义",
      children: (
        <Card>
          <Descriptions column={1} bordered>
            <Descriptions.Item label="指标编码">{guide.metric_code}</Descriptions.Item>
            <Descriptions.Item label="口径定义">
              <Paragraph>{guide.definition}</Paragraph>
            </Descriptions.Item>
          </Descriptions>
        </Card>
      ),
    },
    {
      key: "calculation",
      label: "计算逻辑",
      children: (
        <Card>
          <Paragraph>{guide.calculation_logic}</Paragraph>
        </Card>
      ),
    },
    {
      key: "dimensions",
      label: "维度说明",
      children: (
        <Card>
          {guide.dimensions && guide.dimensions.length > 0 ? (
            <Descriptions column={1} bordered>
              {guide.dimensions.map((d) => (
                <Descriptions.Item key={d.name} label={d.name}>
                  <Tag>{d.type}</Tag> {d.description}
                </Descriptions.Item>
              ))}
            </Descriptions>
          ) : (
            <Paragraph type="secondary">暂无维度信息</Paragraph>
          )}
        </Card>
      ),
    },
    {
      key: "examples",
      label: "使用示例",
      children: (
        <Card>
          {guide.usage_examples && guide.usage_examples.length > 0 ? (
            guide.usage_examples.map((ex, i) => (
              <Card.Grid key={i} style={{ width: "100%", padding: 16 }}>
                <Title level={5}>{ex.title}</Title>
                <Paragraph>{ex.description}</Paragraph>
                <pre style={{ background: "#f5f5f5", padding: 12, borderRadius: 4, overflow: "auto" }}>
                  {ex.sql}
                </pre>
              </Card.Grid>
            ))
          ) : (
            <Paragraph type="secondary">暂无使用示例</Paragraph>
          )}
        </Card>
      ),
    },
    {
      key: "related",
      label: "关联指标",
      children: (
        <Card>
          {guide.related_metrics && guide.related_metrics.length > 0 ? (
            guide.related_metrics.map((code) => (
              <Tag key={code} style={{ marginBottom: 8 }}>{code}</Tag>
            ))
          ) : (
            <Paragraph type="secondary">暂无关联指标</Paragraph>
          )}
        </Card>
      ),
    },
    {
      key: "faq",
      label: "FAQ",
      children: (
        <Card>
          {guide.faq && guide.faq.length > 0 ? (
            guide.faq.map((item, i) => (
              <div key={i} style={{ marginBottom: 16 }}>
                <Title level={5}>Q: {item.question}</Title>
                <Paragraph>A: {item.answer}</Paragraph>
              </div>
            ))
          ) : (
            <Paragraph type="secondary">暂无常见问题</Paragraph>
          )}
        </Card>
      ),
    },
  ];

  return (
    <div>
      <Title level={3}>消费指南 — {metricCode}</Title>
      <Tabs items={tabItems} />
    </div>
  );
}
