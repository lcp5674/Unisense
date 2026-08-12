import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Button,
  Card,
  Descriptions,
  message,
  Space,
  Tag,
  Table,
  Typography,
  Popconfirm,
} from "antd";
import {
  deprecateMetric,
  getMetric,
  listVersions,
  piiReview,
  publishMetric,
  UnisenseApiError,
  fetchCurrentUser,
} from "../api";
import type { MetricResponse, MetricVersionResponse, CurrentUser } from "../types";
import { useTracking } from "../hooks/useTracking";

const { Title, Paragraph } = Typography;

const STATUS_COLOR: Record<string, string> = {
  DRAFT: "default",
  EXPERIMENTAL: "processing",
  PUBLISHED: "success",
  DEPRECATED: "error",
};

const STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  EXPERIMENTAL: "实验",
  PUBLISHED: "已发布",
  DEPRECATED: "已废弃",
};

export function MetricDetail() {
  const { code } = useParams<{ code: string }>();
  const navigate = useNavigate();
  const [metric, setMetric] = useState<MetricResponse | null>(null);
  const [versions, setVersions] = useState<MetricVersionResponse[]>([]);
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);
  const { track } = useTracking();

  async function load() {
    if (!code) return;
    setLoading(true);
    try {
      const [m, vs, me] = await Promise.all([
        getMetric(code),
        listVersions(code),
        fetchCurrentUser(),
      ]);
      setMetric(m);
      setVersions(vs);
      setCurrentUser(me);
      track("metric_detail_view", code, "metric");
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "加载失败",
      );
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

  async function handlePublish() {
    if (!code) return;
    try {
      await publishMetric(code, { change_reason: "首次发布说明" });
      message.success("已发布");
      load();
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "发布失败",
      );
    }
  }

  async function handlePiiReview() {
    if (!code) return;
    try {
      await piiReview(code);
      message.success("PII 合规复核已完成");
      load();
    } catch (err) {
      const e = err as UnisenseApiError;
      message.error(`${e.message} (${e.code})`);
    }
  }

  async function handleDeprecate(successor: string) {
    if (!code || !successor) return;
    try {
      await deprecateMetric(code, successor);
      message.success("已废弃");
      load();
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "废弃失败",
      );
    }
  }

  if (!metric) {
    return loading ? <Card loading /> : <Card><Paragraph type="secondary">指标不存在</Paragraph></Card>;
  }

  const role = currentUser?.role || "";
  const canPiiReview = (role === "platform_admin" || role === "domain_admin") && metric.pii_flag;
  const canPublish = metric.status !== "PUBLISHED" && metric.status !== "DEPRECATED";

  const versionColumns = [
    { title: "版本", dataIndex: "version", key: "version", render: (v: number) => `v${v}` },
    { title: "变更类型", dataIndex: "change_type", key: "change_type" },
    { title: "说明", dataIndex: "change_reason", key: "change_reason" },
    { title: "状态", dataIndex: "status", key: "status" },
    { title: "时间", dataIndex: "created_at", key: "created_at" },
  ];

  return (
    <div>
      <Button type="link" onClick={() => navigate("/catalog")} style={{ marginBottom: 16 }}>
        ← 返回
      </Button>

      <Title level={3}>
        {metric.name}{" "}
        <Tag color={STATUS_COLOR[metric.status]}>{STATUS_LABEL[metric.status]}</Tag>
      </Title>

      <Card style={{ marginBottom: 16 }}>
        <Descriptions column={3} bordered size="small">
          <Descriptions.Item label="编码">{metric.metric_code}</Descriptions.Item>
          <Descriptions.Item label="域">{metric.domain}</Descriptions.Item>
          <Descriptions.Item label="类型">{metric.type}</Descriptions.Item>
          <Descriptions.Item label="分级">{metric.metric_tier}</Descriptions.Item>
          <Descriptions.Item label="聚合">{metric.aggregation}</Descriptions.Item>
          <Descriptions.Item label="粒度">{metric.granularity}</Descriptions.Item>
          <Descriptions.Item label="单位">{metric.unit}</Descriptions.Item>
          <Descriptions.Item label="PII">
            {metric.pii_flag ? (
              <Tag color={metric.compliance_reviewed ? "green" : "orange"}>
                {metric.compliance_reviewed ? "已复核" : "待复核"}
              </Tag>
            ) : (
              "否"
            )}
          </Descriptions.Item>
          <Descriptions.Item label="版本">v{metric.version}</Descriptions.Item>
        </Descriptions>
      </Card>

      <Card title="口径定义" style={{ marginBottom: 16 }}>
        <pre style={{ background: "#f5f5f5", padding: 12, borderRadius: 4, overflow: "auto" }}>
          {JSON.stringify(metric.definition_json, null, 2)}
        </pre>
      </Card>

      <Space style={{ marginBottom: 16 }}>
        {canPublish && (
          <Button
            type="primary"
            onClick={handlePublish}
            disabled={metric.pii_flag && !metric.compliance_reviewed}
          >
            发布{metric.pii_flag && !metric.compliance_reviewed ? "（需先 PII 复核）" : ""}
          </Button>
        )}
        {canPiiReview && !metric.compliance_reviewed && (
          <Button onClick={handlePiiReview}>PII 合规复核</Button>
        )}
        {metric.status !== "DEPRECATED" && (
          <Popconfirm
            title="废弃指标"
            description="请输入替代指标编码"
            onConfirm={(value) => handleDeprecate(String(value || ""))}
          >
            <Button danger>废弃</Button>
          </Popconfirm>
        )}
      </Space>

      <Card title="版本历史">
        <Table
          dataSource={versions}
          columns={versionColumns}
          rowKey="id"
          pagination={false}
          size="small"
          locale={{ emptyText: "暂无版本" }}
        />
      </Card>
    </div>
  );
}
