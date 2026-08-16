import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Table, Tag } from "antd";
import { listMetricDimensions } from "../../api";
import type { MetricDimension } from "../../types";

// 指标-维度绑定角色中文标签（对齐后端 MetricDimensionRole 枚举，与 Dimensions.tsx 保持一致）
const ROLE_LABEL: Record<string, string> = {
  PARTITION: "分区",
  SPLICE: "拼接",
  FILTER: "过滤",
};

const ROLE_COLOR: Record<string, string> = {
  PARTITION: "blue",
  SPLICE: "purple",
  FILTER: "cyan",
};

// 维度状态 → 中文/颜色（对齐 Dimensions.tsx 的 STATUS_LABEL/STATUS_COLOR）
const DIM_STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  PUBLISHED: "已发布",
  DEPRECATED: "已废弃",
};
const DIM_STATUS_COLOR: Record<string, string> = {
  DRAFT: "default",
  PUBLISHED: "success",
  DEPRECATED: "error",
};

/** 指标详情页「关联维度」：展示该指标已绑定的维度及角色（治理追溯，TD §12.6） */
export function RelatedDimensions({ metricId }: { metricId: number }) {
  const navigate = useNavigate();
  const [items, setItems] = useState<MetricDimension[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const res = await listMetricDimensions(metricId);
      setItems(res.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "维度绑定加载失败");
      setItems([]);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [metricId]);

  const columns = [
    {
      title: "维度编码",
      dataIndex: "dim_code",
      key: "dim_code",
      render: (v: string) => <span className="mono">{v}</span>,
    },
    {
      title: "角色",
      dataIndex: "role",
      key: "role",
      width: 180,
      render: (v: string) => <Tag color={ROLE_COLOR[v] ?? "default"}>{ROLE_LABEL[v] ?? v}</Tag>,
    },
    {
      title: "默认成员",
      dataIndex: "default_member",
      key: "default_member",
      render: (v: string | null) => (v ? <span className="mono">{v}</span> : <span className="muted">—</span>),
    },
    {
      title: "维度状态",
      dataIndex: "dim_status",
      key: "dim_status",
      width: 110,
      render: (v: string | null | undefined) =>
        v ? (
          <Tag color={DIM_STATUS_COLOR[v] ?? "default"}>{DIM_STATUS_LABEL[v] ?? v}</Tag>
        ) : (
          <span className="muted">—</span>
        ),
    },
  ];

  return (
    <div>
      <div style={{ marginBottom: 12 }}>
        <Button type="link" size="small" onClick={() => navigate("/dimensions")}>
          前往维度管理 →
        </Button>
      </div>
      <Table
        dataSource={items}
        columns={columns}
        rowKey="id"
        size="small"
        pagination={{ pageSize: 8, hideOnSinglePage: true }}
        loading={loading}
        locale={{ emptyText: error ?? "该指标暂未绑定维度（可在维度管理绑定）" }}
      />
    </div>
  );
}
