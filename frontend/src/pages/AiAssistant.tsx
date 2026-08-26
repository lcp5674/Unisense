import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  Space,
  Tag,
  message,
} from "antd";
import {
  RobotOutlined,
  ThunderboltOutlined,
  ArrowLeftOutlined,
} from "@ant-design/icons";
import { aiNl2Sql, UnisenseApiError } from "../api";
import type { NL2SQLResult } from "../types";
import { useTracking } from "../hooks/useTracking";
import { usePermission } from "../hooks/usePermission";
import { kvText } from "../utils/display";
import { formatSql } from "../utils/sqlFormat";

const { TextArea } = Input;

const EXAMPLES = [
  "最近 30 天 finance 域收入总额，按日粒度",
  "对比本月与上月 GMV 的环比变化",
  "统计 marketing 域新增用户数，同比上月",
];

export function AiAssistant() {
  const [nlQuery, setNlQuery] = useState("");
  const [metricScope, setMetricScope] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<NL2SQLResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { track } = useTracking();
  const navigate = useNavigate();
  // 按钮级权限点：无 ai:nl2sql 时禁用问数入口（后端强制仍兜底）
  const canNl2Sql = usePermission().can("ai:nl2sql");
  // 生成方式由后端决定：仅生成 SQL，不直接执行（安全加固 X-1——直接执行会绕过
  // consume 统一鉴权管道，改由「查询工作台」走正规 PDP/行级隔离/PII 脱敏执行）。
  const canQueryExecute = usePermission().can("query:execute");

  // 统一返回上一入口：优先回退浏览器历史（总览快捷入口等），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

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
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
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
            <Button
              type="primary"
              icon={<RobotOutlined />}
              loading={loading}
              onClick={handleSubmit}
              disabled={!canNl2Sql}
              style={{ marginLeft: 12 }}
            >
              生成 SQL
            </Button>
            {canQueryExecute && (
              <span className="muted" style={{ fontSize: 12 }}>
                生成后可在「查询工作台」执行（走正规鉴权）
              </span>
            )}
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
                <pre className="code-block">{formatSql(result.sql)}</pre>
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
                <div style={{ marginTop: 12 }}>
                  <Button
                    type="primary"
                    disabled={!canQueryExecute}
                    onClick={() => navigate("/query")}
                  >
                    <ThunderboltOutlined /> 到查询工作台执行
                  </Button>
                  {!canQueryExecute && (
                    <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>
                      无 query:execute 权限，仅可查看 SQL
                    </span>
                  )}
                </div>
              </Card>
            ) : (
              <Alert type="info" message="未生成 SQL，请调整表述后重试" style={{ marginBottom: 16 }} />
            )}
          </div>
        )}
      </Card>
    </div>
  );
}
