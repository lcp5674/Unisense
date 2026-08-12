import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Table, Input, Select, Button, Space, Tag, message } from "antd";
import { SearchOutlined } from "@ant-design/icons";
import { listMetrics, UnisenseApiError } from "../api";
import type { MetricResponse } from "../types";
import { useTracking } from "../hooks/useTracking";

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

export function MetricCatalog() {
  const [items, setItems] = useState<MetricResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [keyword, setKeyword] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const { track } = useTracking();

  async function load() {
    setLoading(true);
    try {
      const res = await listMetrics({ keyword, status, page, page_size: 20 });
      setItems(res.items);
      setTotal(res.total);
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
  }, [page, status]);

  function handleSearch() {
    if (keyword) {
      track("metric_search", undefined, "metric", { keyword });
    }
    setPage(1);
    load();
  }

  const columns = [
    {
      title: "编码",
      dataIndex: "metric_code",
      key: "metric_code",
      render: (text: string) => (
        <Button type="link" onClick={() => navigate(`/detail/${text}`)}>
          {text}
        </Button>
      ),
    },
    { title: "名称", dataIndex: "name", key: "name" },
    { title: "域", dataIndex: "domain", key: "domain" },
    { title: "类型", dataIndex: "type", key: "type" },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      render: (s: string) => (
        <Tag color={STATUS_COLOR[s]}>{STATUS_LABEL[s] ?? s}</Tag>
      ),
    },
    { title: "分级", dataIndex: "metric_tier", key: "metric_tier" },
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
  ];

  return (
    <div>
      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          placeholder="搜索指标名/编码"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          onPressEnter={handleSearch}
          prefix={<SearchOutlined />}
          style={{ width: 240 }}
        />
        <Button type="primary" onClick={handleSearch} icon={<SearchOutlined />}>
          搜索
        </Button>
        <Select
          value={status || undefined}
          onChange={(v) => {
            setStatus(v || "");
            setPage(1);
          }}
          style={{ width: 140 }}
          allowClear
          placeholder="全部状态"
          options={[
            { value: "DRAFT", label: "草稿" },
            { value: "EXPERIMENTAL", label: "实验" },
            { value: "PUBLISHED", label: "已发布" },
            { value: "DEPRECATED", label: "已废弃" },
          ]}
        />
        <span style={{ color: "#999" }}>共 {total} 条</span>
      </Space>

      <Table
        dataSource={items}
        columns={columns}
        rowKey="metric_code"
        loading={loading}
        pagination={{
          current: page,
          pageSize: 20,
          total,
          onChange: (p) => setPage(p),
          showTotal: (t) => `共 ${t} 条`,
        }}
        onRow={(record) => ({
          onClick: () => navigate(`/detail/${record.metric_code}`),
          style: { cursor: "pointer" },
        })}
      />
    </div>
  );
}
