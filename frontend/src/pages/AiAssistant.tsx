import { useState } from "react";
import { Card, Input, Button, Form, Switch, Tag, Alert, Space, Table, message } from "antd";
import { RobotOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { aiNl2Sql, UnisenseApiError } from "../api";
import type { NL2SQLResult } from "../types";
import { useTracking } from "../hooks/useTracking";
import { kvText } from "../utils/display";

const { TextArea } = Input;

const EXAMPLES = [
  "最近 30 天 finance 域收入总额，按日粒度",
  "对比本月与上月 GMV 的环比变化",
  "统计 marketing 域新增用户数，同比上月",
];

// 执行结果行：对象数组 → 动态列表格；非行结构 → 可读文本
function ExecuteResultTable({ rows }: { rows: unknown[] }) {
  const rowObjects = rows.filter((r): r is Record<string, unknown> => typeof r === "object" && r !== null);
  if (rowObjects.length === 0) {
    return <span className="muted">无结构化行数据</span>;
  }
  const cols = Object.keys(rowObjects[0]).map((k) => ({
    title: k,
    dataIndex: k,
    key: k,
    ellipsis: true,
    render: (v: unknown) =>
      typeof v === "object" && v !== null ? (
        <span className="mono" style={{ fontSize: 12 }}>{JSON.stringify(v)}</span>
      ) : (
        String(v ?? "")
      ),
  }));
  return <Table size="small" dataSource={rowObjects} columns={cols} rowKey={(_, i) => String(i)} pagination={{ pageSize: 10 }} />;
}

export function AiAssistant() {
  const [nlQuery, setNlQuery] = useState("");
  const [metricScope, setMetricScope] = useState("");
  const [execute, setExecute] = useState(false);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<NL2SQLResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { track } = useTracking();

  async function handleSubmit() {
    if (!nlQuery.trim()) {
      message.warning("请输入自然语言查询");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const scope = metricScope
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const res = await aiNl2Sql({
        nl_query: nlQuery.trim(),
        metric_scope: scope.length ? scope : null,
        execute,
      });
      setResult(res);
      track("ai_nl2sql", undefined, "ai", { method: res.method });
    } catch (err) {
      setError(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "调用失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Intelligence / NL2SQL</div>
          <h2>AI 助手</h2>
          <p>自然语言查询 → 锚定指标口径 → 生成安全 SQL（keyword / LLM 双通道）。</p>
        </div>
      </div>

      <Card>
        <Form layout="vertical">
          <Form.Item label="自然语言查询">
            <TextArea
              rows={4}
              value={nlQuery}
              onChange={(e) => setNlQuery(e.target.value)}
              placeholder="如：最近 30 天 finance 域收入总额，按日粒度"
              style={{ fontSize: 14 }}
            />
          </Form.Item>
          <Form.Item label="指标范围（逗号分隔，可选）">
            <Input
              className="mono"
              value={metricScope}
              onChange={(e) => setMetricScope(e.target.value)}
              placeholder="finance_revenue_sum_d, finance_cost_sum_d"
            />
          </Form.Item>
          <Space style={{ marginBottom: 12 }}>
            <Form.Item label="执行查询" valuePropName="checked" style={{ marginBottom: 0 }}>
              <Switch checked={execute} onChange={setExecute} />
            </Form.Item>
            <Button
              type="primary"
              icon={<RobotOutlined />}
              loading={loading}
              onClick={handleSubmit}
              style={{ marginLeft: 12 }}
            >
              生成 SQL
            </Button>
          </Space>
        </Form>

        <div style={{ marginBottom: 16 }}>
          <span className="muted" style={{ fontSize: 12, marginRight: 8 }}>示例：</span>
          {EXAMPLES.map((ex) => (
            <Tag
              key={ex}
              style={{ cursor: "pointer", marginBottom: 4 }}
              onClick={() => setNlQuery(ex)}
            >
              {ex}
            </Tag>
          ))}
        </div>

        {error && <Alert type="error" message={error} showIcon style={{ marginBottom: 16 }} />}

        {result && (
          <div>
            <Alert
              type={result.safe ? "success" : "warning"}
              showIcon
              icon={<ThunderboltOutlined />}
              style={{ marginBottom: 16 }}
              message={`生成方式：${result.method === "llm" ? "LLM" : result.method === "keyword" ? "关键词匹配" : "未生成"}`}
              description={result.notes?.join("；")}
            />

            {result.sql ? (
              <Card title="生成的 SQL" size="small" style={{ marginBottom: 16 }}>
                <pre className="code-block">{result.sql}</pre>
                {Object.keys(result.params ?? {}).length > 0 && (
                  <div style={{ marginTop: 8 }}>
                    <span className="muted" style={{ fontSize: 12 }}>参数：</span>
                    <span className="mono" style={{ fontSize: 12 }}>{kvText(result.params)}</span>
                  </div>
                )}
                {result.anchored?.length > 0 && (
                  <div style={{ marginTop: 8 }}>
                    <span className="muted" style={{ fontSize: 12 }}>锚定指标：</span>
                    {result.anchored.map((a) => (
                      <Tag key={a} color="orange" style={{ marginBottom: 4 }}>{a}</Tag>
                    ))}
                  </div>
                )}
              </Card>
            ) : (
              <Alert type="info" message="未生成 SQL，请调整表述后重试" style={{ marginBottom: 16 }} />
            )}

            {result.execute_result && (
              <Card title={`执行结果（${result.execute_result.elapsed_ms} ms，共 ${result.execute_result.total} 行）`} size="small">
                <ExecuteResultTable rows={result.execute_result.rows} />
              </Card>
            )}
            {result.execute_error && (
              <Alert type="error" message="执行失败" description={result.execute_error} showIcon style={{ marginTop: 16 }} />
            )}
          </div>
        )}
      </Card>
    </div>
  );
}
