import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card, Select, Space, Table, Tag, message, Modal, Input } from "antd";
import { arbitrateConflict, escalateConflict, listConflicts, UnisenseApiError } from "../api";
import type { ConflictResponse } from "../types";
import { useTracking } from "../hooks/useTracking";

const STATUS_LABEL: Record<string, string> = {
  OPEN: "待处理",
  ARBITRATED: "已仲裁",
  RULED: "已裁决",
  CLOSED: "已关闭",
  ESCALATED: "已升级",
};

const STATUS_COLOR: Record<string, string> = {
  OPEN: "warning",
  ARBITRATED: "success",
  RULED: "success",
  CLOSED: "default",
  ESCALATED: "error",
};

export function ReviewWorkbench() {
  const [items, setItems] = useState<ConflictResponse[]>([]);
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const navigate = useNavigate();
  const { track } = useTracking();

  async function load() {
    setLoading(true);
    try {
      const res = await listConflicts({ status, page_size: 50 });
      setItems(res.items);
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
  }, [status]);

  async function handleArbitrate(c: ConflictResponse) {
    let canonical = "";
    Modal.confirm({
      title: `仲裁冲突 ${c.conflict_id}`,
      content: (
        <div>
          <p>候选：{c.candidate_metric_code}</p>
          <p>现有：{c.existing_metric_code}</p>
          <Input
            defaultValue={c.existing_metric_code}
            onChange={(e) => {
              canonical = e.target.value;
            }}
            placeholder="请输入采纳为权威的编码"
          />
        </div>
      ),
      onOk: async () => {
        if (!canonical) return;
        setBusyId(c.conflict_id);
        try {
          await arbitrateConflict(c.conflict_id, "ACCEPT", canonical);
          message.success(`已仲裁：${c.conflict_id}`);
          track("review_arbitrate", c.conflict_id, "conflict");
          load();
        } catch (err) {
          message.error(
            err instanceof UnisenseApiError
              ? `${err.message}（${err.codeZh}）`
              : "仲裁失败（仅 compliance_officer/domain_admin）",
          );
        } finally {
          setBusyId(null);
        }
      },
    });
  }

  async function handleEscalate(c: ConflictResponse) {
    let note = "";
    Modal.confirm({
      title: "升级冲突",
      content: (
        <Input
          placeholder="升级备注"
          onChange={(e) => {
            note = e.target.value;
          }}
        />
      ),
      onOk: async () => {
        setBusyId(c.conflict_id);
        try {
          await escalateConflict(c.conflict_id, note);
          message.success(`已升级：${c.conflict_id}`);
          track("review_escalate", c.conflict_id, "conflict");
          load();
        } catch (err) {
          message.error(
            err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "升级失败",
          );
        } finally {
          setBusyId(null);
        }
      },
    });
  }

  const columns = [
    { title: "冲突ID", dataIndex: "conflict_id", key: "conflict_id" },
    { title: "类型", dataIndex: "type", key: "type" },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      render: (s: string) => <Tag color={STATUS_COLOR[s]}>{STATUS_LABEL[s] ?? s}</Tag>,
    },
    { title: "严重度", dataIndex: "severity", key: "severity" },
    {
      title: "候选",
      dataIndex: "candidate_metric_code",
      key: "candidate",
      render: (code: string) => (
        <Button type="link" size="small" onClick={() => navigate(`/detail/${code}`)}>
          {code}
        </Button>
      ),
    },
    {
      title: "现有",
      dataIndex: "existing_metric_code",
      key: "existing",
      render: (code: string) => (
        <Button type="link" size="small" onClick={() => navigate(`/detail/${code}`)}>
          {code}
        </Button>
      ),
    },
    { title: "描述", dataIndex: "description", key: "description", ellipsis: true },
    {
      title: "操作",
      key: "actions",
      render: (_: unknown, c: ConflictResponse) =>
        c.status === "OPEN" ? (
          <Space>
            <Button
              size="small"
              disabled={busyId === c.conflict_id}
              onClick={() => handleArbitrate(c)}
            >
              仲裁
            </Button>
            <Button
              size="small"
              danger
              disabled={busyId === c.conflict_id}
              onClick={() => handleEscalate(c)}
            >
              升级
            </Button>
          </Space>
        ) : null,
    },
  ];

  return (
    <div>
      <Card
        title="审核工作台（冲突仲裁）"
        extra={
          <Select
            value={status || undefined}
            onChange={(v) => setStatus(v || "")}
            style={{ width: 140 }}
            allowClear
            placeholder="全部状态"
            options={[
              { value: "OPEN", label: "待处理" },
              { value: "ARBITRATED", label: "已仲裁" },
              { value: "ESCALATED", label: "已升级" },
              { value: "CLOSED", label: "已关闭" },
            ]}
          />
        }
      >
        <Table
          dataSource={items}
          columns={columns}
          rowKey="conflict_id"
          loading={loading}
          pagination={false}
          locale={{ emptyText: "暂无冲突" }}
        />
      </Card>
    </div>
  );
}
