import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card, Input, Modal, Space, Table, Tag, message } from "antd";
import { ArrowLeftOutlined } from "@ant-design/icons";
import { listMetrics, reviewMetric, UnisenseApiError } from "../api";
import type { MetricResponse } from "../types";

function openReviewModal(
  metric: MetricResponse,
  approved: boolean,
  onOk: (reason: string) => Promise<void>,
) {
  let reason = "";
  Modal.confirm({
    title: approved ? `通过评审：${metric.metric_code}` : `驳回：${metric.metric_code}`,
    content: (
      <div>
        <p style={{ marginBottom: 12 }}>
          {approved
            ? "通过后该指标将进入已发布状态。"
            : "驳回后该指标将退回草稿状态。"}
        </p>
        <Input.TextArea
          rows={3}
          placeholder="变更原因（可选）"
          onChange={(e) => {
            reason = e.target.value;
          }}
        />
      </div>
    ),
    okText: approved ? "通过" : "驳回",
    cancelText: "取消",
    okButtonProps: approved ? { type: "primary" as const } : { danger: true },
    onOk: () => onOk(reason),
  });
}

export function MetricReview() {
  const [items, setItems] = useState<MetricResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busyCode, setBusyCode] = useState<string | null>(null);
  const navigate = useNavigate();

  // 统一返回上一入口：优先回退浏览器历史（总览告警带等入口），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  async function load() {
    setLoading(true);
    try {
      const res = await listMetrics({ status: "REVIEW", page_size: 100 });
      setItems(res.items);
      setTotal(res.total);
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

  async function handleReview(metric: MetricResponse, approved: boolean, reason: string) {
    setBusyCode(metric.metric_code);
    try {
      await reviewMetric(metric.metric_code, approved, reason);
      message.success(approved ? `已通过：${metric.metric_code}` : `已驳回：${metric.metric_code}`);
      load();
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败",
      );
    } finally {
      setBusyCode(null);
    }
  }

  const columns = [
    {
      title: "编码",
      dataIndex: "metric_code",
      key: "metric_code",
      render: (code: string) => (
        <Button type="link" size="small" onClick={() => navigate(`/detail/${code}`)}>
          {code}
        </Button>
      ),
    },
    { title: "名称", dataIndex: "name", key: "name" },
    { title: "域", dataIndex: "domain", key: "domain" },
    {
      title: "PII",
      key: "pii",
      render: (_: unknown, r: MetricResponse) =>
        r.pii_flag ? (
          <Tag color={r.compliance_reviewed ? "green" : "orange"}>
            {r.compliance_reviewed ? "已复核" : "待复核"}
          </Tag>
        ) : (
          <Tag>否</Tag>
        ),
    },
    {
      title: "版本",
      dataIndex: "version",
      key: "version",
      render: (v: number) => `v${v}`,
    },
    { title: "更新时间", dataIndex: "updated_at", key: "updated_at", render: (v: string | null) => (v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : <span className="muted">—</span>) },
    {
      title: "操作",
      key: "actions",
      render: (_: unknown, r: MetricResponse) => (
        <Space>
          <Button
            size="small"
            type="primary"
            disabled={busyCode === r.metric_code}
            onClick={() => openReviewModal(r, true, (reason) => handleReview(r, true, reason))}
          >
            通过
          </Button>
          <Button
            size="small"
            danger
            disabled={busyCode === r.metric_code}
            onClick={() => openReviewModal(r, false, (reason) => handleReview(r, false, reason))}
          >
            驳回
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 8 }}>
        返回
      </Button>
      <Card
        title="指标审批"
        extra={
          <Button size="small" onClick={load} loading={loading}>
            刷新
          </Button>
        }
      >
        <Table
          dataSource={items}
          columns={columns}
          rowKey="metric_code"
          loading={loading}
          pagination={false}
          locale={{ emptyText: "暂无待评审指标" }}
          footer={() => `共 ${total} 条待评审`}
        />
      </Card>
    </div>
  );
}
