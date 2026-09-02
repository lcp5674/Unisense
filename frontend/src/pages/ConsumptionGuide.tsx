import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { Card, Spin, Alert, Descriptions, Tag, Empty, Space, Button, Modal, Input, Typography, message } from "antd";
import {
  InfoCircleOutlined, WarningOutlined, LinkOutlined, ArrowLeftOutlined,
  EditOutlined, PlusOutlined, DeleteOutlined,
} from "@ant-design/icons";
import { fetchConsumptionGuide, getMetric, updateConsumptionGuide } from "../api";
import type { ConsumptionGuideResponse, ConsumptionGuidePayload, MetricResponse } from "../types";
import { useTracking } from "../hooks/useTracking";
import { usePermission } from "../hooks/usePermission";
import { ObjectView, DEF_FIELD_LABEL } from "../utils/display";
import { enumLabel, METRIC_TYPE_LABEL, AGGREGATION_LABEL, TIME_SEMANTICS_LABEL, SERVING_MODE_LABEL } from "../utils/enums";

/** 单组字符串列表编辑器（推荐用法/注意事项/关联指标共用，可增删行）。
 *  供消费指南页（编辑弹窗）与指标创建/编辑页（表单内嵌区块）复用。 */
export function ListEditor({
  label, value, onChange, placeholder, size,
}: {
  label: string;
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  size?: "small" | "middle";
}) {
  return (
    <div style={{ marginBottom: 16 }}>
      <Typography.Text strong>{label}</Typography.Text>
      <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 8 }}>
        {value.map((item, i) => (
          <div key={i} style={{ display: "flex", gap: 8 }}>
            <Input
              size={size}
              value={item}
              placeholder={placeholder}
              onChange={(e) => {
                const next = [...value];
                next[i] = e.target.value;
                onChange(next);
              }}
            />
            <Button
              type="text"
              danger
              icon={<DeleteOutlined />}
              aria-label={`删除 ${label} 第 ${i + 1} 项`}
              onClick={() => onChange(value.filter((_, idx) => idx !== i))}
            />
          </div>
        ))}
        <Button
          type="dashed"
          block
          size={size}
          icon={<PlusOutlined />}
          onClick={() => onChange([...value, ""])}
        >
          添加一项
        </Button>
      </div>
    </div>
  );
}

export function ConsumptionGuide() {
  const { metricCode } = useParams<{ metricCode: string }>();
  const navigate = useNavigate();
  const [guide, setGuide] = useState<ConsumptionGuideResponse | null>(null);
  const [metric, setMetric] = useState<MetricResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { track } = useTracking();
  const { can } = usePermission();
  const canEdit = can("metric:edit") && metric?.status !== "DEPRECATED";
  const isDeprecated = metric?.status === "DEPRECATED";
  // 编辑弹窗状态：三组列表草稿 + 保存中
  const [editOpen, setEditOpen] = useState(false);
  const [editDraft, setEditDraft] = useState<ConsumptionGuidePayload>({
    recommended_usage: [], cautions: [], related_metrics: [],
  });
  const [saving, setSaving] = useState(false);

  // 统一返回上一入口：优先回退浏览器历史（指标详情等入口），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  function openEdit() {
    setEditDraft({
      recommended_usage: [...(guide?.recommended_usage ?? [])],
      cautions: [...(guide?.cautions ?? [])],
      related_metrics: [...(guide?.related_metrics ?? [])],
    });
    setEditOpen(true);
  }

  async function saveEdit() {
    if (!metricCode) return;
    setSaving(true);
    try {
      const updated = await updateConsumptionGuide(metricCode, {
        recommended_usage: editDraft.recommended_usage.filter((s) => s.trim()),
        cautions: editDraft.cautions.filter((s) => s.trim()),
        related_metrics: editDraft.related_metrics.filter((s) => s.trim()),
        row_version: metric?.row_version ?? undefined,
      });
      setGuide((prev) => (prev ? { ...prev, ...updated } : updated));
      setEditOpen(false);
      message.success("消费指南已保存");
      track("consumption_guide_update", metricCode, "metric");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存消费指南失败");
    } finally {
      setSaving(false);
    }
  }

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
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
          <div className="page-kicker">Consumption / Guide</div>
          <h2>消费指南 — <span className="mono">{metricCode}</span></h2>
          <p>推荐的查询方式、注意事项与关联指标——基于指标语义自动生成，可由 Owner/管理员人工维护。</p>
        </div>
        <Space>
          <Tag color="orange">{guide.domain}</Tag>
          <Tag>{enumLabel(METRIC_TYPE_LABEL, guide.type)}</Tag>
          <Tag>{enumLabel(SERVING_MODE_LABEL, guide.serving_mode)}</Tag>
          <Tag color={guide.guide_source === "manual" ? "green" : "default"}>
            {guide.guide_source === "manual" ? "人工维护" : "自动生成"}
          </Tag>
          {canEdit && (
            <Button icon={<EditOutlined />} onClick={openEdit}>
              编辑指南
            </Button>
          )}
        </Space>
      </div>

      {isDeprecated && (
        <Alert
          type="warning"
          showIcon
          message="指标已废弃（DEPRECATED）"
          description="该指标已废弃，不可消费，消费指南仅供审计回溯；如需更新指南，请先将指标重新提交评审恢复。"
          style={{ marginBottom: 20 }}
        />
      )}

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

      <Modal
        title="编辑消费指南"
        open={editOpen}
        onCancel={() => setEditOpen(false)}
        onOk={saveEdit}
        confirmLoading={saving}
        okText="保存"
        width={680}
        destroyOnClose
      >
        <ListEditor
          label="推荐使用方式"
          value={editDraft.recommended_usage}
          onChange={(v) => setEditDraft((d) => ({ ...d, recommended_usage: v }))}
          placeholder="如：适用 sales 域 daily 粒度分析"
        />
        <ListEditor
          label="注意事项"
          value={editDraft.cautions}
          onChange={(v) => setEditDraft((d) => ({ ...d, cautions: v }))}
          placeholder="如：该指标包含 PII 数据"
        />
        <ListEditor
          label="关联指标编码"
          value={editDraft.related_metrics}
          onChange={(v) => setEditDraft((d) => ({ ...d, related_metrics: v }))}
          placeholder="如：sales_uv_daily"
        />
      </Modal>
    </div>
  );
}
