import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert,
  AutoComplete,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  ApiOutlined,
  ArrowDownOutlined,
  ArrowUpOutlined,
  CheckCircleOutlined,
  CloudDownloadOutlined,
  CloseCircleOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  LinkOutlined,
  PlusOutlined,
  ThunderboltOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import {
  createLlmConfig,
  deleteLlmConfig,
  fetchLlmModels,
  getLlmConfigs,
  getLlmConfigSecret,
  testLlmConfig,
  UnisenseApiError,
  updateLlmConfig,
} from "../api";
import type {
  LlmConfigItem,
  LlmConfigList,
  LlmConfigPayload,
  LlmConfigTestResult,
} from "../types";
import { usePermission } from "../hooks/usePermission";

// OpenAI 协议兼容提供商的预设（对齐后端 services/llm/config_service.py PROVIDER_DEFAULTS）
const PROVIDER_PRESETS: Record<string, { label: string; base_url: string; model: string }> = {
  openai: { label: "OpenAI", base_url: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  deepseek: { label: "DeepSeek", base_url: "https://api.deepseek.com", model: "deepseek-chat" },
  qwen: {
    label: "阿里云百炼（通义千问）",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode",
    model: "qwen-turbo",
  },
  ernie: {
    label: "文心一言",
    base_url: "https://aip.baidubce.com/rpc/2.0/ai_custom",
    model: "ernie-bot-turbo",
  },
  kilo: {
    label: "kilo.ai",
    base_url: "https://api.kilo.ai/api/gateway",
    model: "poolside/laguna-m.1:free",
  },
  // 火山方舟 Coding Plan：OpenAI 兼容网关不提供 GET /models，「获取模型」回退内置常用模型目录
  ark: {
    label: "火山方舟（Coding Plan）",
    base_url: "https://ark.cn-beijing.volces.com/api/coding/v3",
    model: "deepseek-v3.1",
  },
  // 腾讯云混元（Coding Plan 订阅开放更多模型），同样无 /models 端点
  tencent: {
    label: "腾讯云混元",
    base_url: "https://api.hunyuan.cloud.tencent.com/v1",
    model: "hunyuan-turbos-latest",
  },
  custom: { label: "自定义（任意 OpenAI 兼容端点）", base_url: "", model: "" },
};

const SOURCE_LABEL: Record<string, string> = {
  db: "数据库配置",
  env: "环境变量",
  none: "未配置",
};

function TestResultBadge({ result }: { result: LlmConfigTestResult }) {
  if (result.ok) {
    const modelInfo = result.models?.length
      ? ` · 可用模型 ${result.models.length} 个`
      : "";
    // 网关未实现 GET /models（如火山方舟/腾讯混元）时连通由真实推理验证，明示避免困惑
    const modelsNote =
      result.models_supported === false ? " · 网关无 /models，已用真实推理验证连通" : "";
    return (
      <Tag icon={<CheckCircleOutlined />} color="success">
        连通成功 · 推理正常 · {result.latency_ms} ms · {result.model}
        {modelInfo}
        {modelsNote}
      </Tag>
    );
  }
  // chat=false：网关可达但模型真实推理失败（后端 error 已含具体原因）
  const prefix = result.chat === false ? "推理失败" : "连通失败";
  return (
    <Tag icon={<CloseCircleOutlined />} color="error">
      <span>
        {prefix}：{result.error || "未知错误"}
        {result.detail?.request_url ? (
          <span className="muted" style={{ fontSize: 11, marginLeft: 8 }}>
            （{String(result.detail.request_url)}）
          </span>
        ) : null}
      </span>
    </Tag>
  );
}

/** 轮询位次：enabled 优先 → priority 升序 → id 升序；env 兜底不计入。返回 1-based 位次。 */
function computeRank(items: LlmConfigItem[], targetId: number): number {
  const ranked = [...items]
    .filter((i) => i.id != null && i.source !== "env")
    .sort((a, b) => {
      if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
      if (a.priority !== b.priority) return a.priority - b.priority;
      return (a.id ?? 0) - (b.id ?? 0);
    });
  const idx = ranked.findIndex((i) => i.id === targetId);
  return idx === -1 ? 0 : idx + 1;
}

/**
 * 构造同优先级相邻交换的更新序列（严格「与相邻交换、其余位次不动」）。
 *
 * 排序键 (enabled, priority, id) 下，同优先级区间内顺序由 id 决定——只改被移动的
 * 两个实例无法表达交换且保持其余实例位次（数学上不可能）。因此对「同 enabled 分组
 * + 同 priority」的连续区间整体重排：段内执行相邻交换后，把段内 priority 重写为从
 * 区间起点 p 开始的连续递增值（p, p+1, ...），段内其他实例相对位次不变（仅数值变化）、
 * 段外实例完全不动；若重排终点撞上同组下一区间起点，连锁并入下一区间，直到终点落在
 * priority 空隙或分组尾。返回 ``{id, priority}`` 更新列表（值未变的实例跳过）；
 * 优先级空间不足（段内唯一化超出后端 le=100）时返回 null。
 */
function buildSamePrioritySwapUpdates(
  ranked: LlmConfigItem[],
  idx: number,
  dir: -1 | 1,
): { id: number; priority: number }[] | null {
  const MAX_PRIORITY = 100; // 对齐后端 LlmConfigPayload.priority le=100
  const cur = ranked[idx];
  const p = cur.priority;
  const groupEnabled = cur.enabled;
  // 同 enabled 分组内连续同 priority 区间的左/右边界
  let start = idx;
  while (
    start > 0 &&
    ranked[start - 1].enabled === groupEnabled &&
    ranked[start - 1].priority === p
  ) {
    start--;
  }
  let end = idx;
  while (
    end < ranked.length - 1 &&
    ranked[end + 1].enabled === groupEnabled &&
    ranked[end + 1].priority === p
  ) {
    end++;
  }
  // 连锁扩展：段重排终点 (p + len - 1) 若 >= 同组下一区间起点，并入下一区间
  while (end < ranked.length - 1 && ranked[end + 1].enabled === groupEnabled) {
    const nextP = ranked[end + 1].priority;
    const len = end - start + 1;
    if (p + len - 1 < nextP) break;
    while (
      end < ranked.length - 1 &&
      ranked[end + 1].enabled === groupEnabled &&
      ranked[end + 1].priority === nextP
    ) {
      end++;
    }
  }
  const len = end - start + 1;
  if (p + len - 1 > MAX_PRIORITY) return null; // 优先级空间不足
  // 段内执行相邻交换（cur 与 target 互换位置）
  const segment = ranked.slice(start, end + 1);
  const ci = idx - start;
  const ti = ci + dir;
  [segment[ci], segment[ti]] = [segment[ti], segment[ci]];
  // 重写段内 priority = p, p+1, ...；值未变的实例跳过（减少无效更新）
  const updates: { id: number; priority: number }[] = [];
  for (let i = 0; i < segment.length; i++) {
    const item = segment[i];
    const newP = p + i;
    if (item.id != null && item.priority !== newP) {
      updates.push({ id: item.id, priority: newP });
    }
  }
  return updates;
}

/** 路由状态概览条（P0-1）：当前路由选中谁 · 启用几个 · 已验证连通几个。 */
function RoutingOverview({
  data,
  testResults,
}: {
  data: LlmConfigList;
  testResults: Record<number, LlmConfigTestResult>;
}) {
  const items = data.items ?? [];
  const enabledCount = items.filter((i) => i.enabled).length;
  const verifiedCount = Object.values(testResults).filter((r) => r?.ok).length;
  const effectiveItem =
    items.find((i) => i.base_url === data.effective.base_url) ?? null;
  return (
    <div
      style={{
        display: "flex",
        gap: 20,
        flexWrap: "wrap",
        alignItems: "center",
        padding: "8px 12px",
        marginBottom: 12,
        background: "var(--bg-elevated, #fafafa)",
        borderRadius: 6,
        fontSize: 13,
        color: "var(--text-2)",
      }}
      data-testid="routing-overview"
    >
      <span>
        当前路由：
        <b style={{ color: "var(--text-1)" }}>
          {effectiveItem?.name || data.effective.provider || "未配置"}
        </b>
        <Tag style={{ marginLeft: 6 }}>
          {data.effective.source === "env" ? "环境变量" : "数据库"}
        </Tag>
      </span>
      <span>
        启用 <b style={{ color: "var(--text-1)" }}>{enabledCount}</b> 个实例
      </span>
      <span>
        已验证连通{" "}
        <b style={{ color: verifiedCount > 0 ? "#2e7d32" : "var(--text-1)" }}>
          {verifiedCount}
        </b>{" "}
        个
      </span>
      {items.some((i) => i.enabled && i.priority === 0) ? (
        <span style={{ color: "#2e7d32" }}>● 有最高优先级（0）实例</span>
      ) : null}
    </div>
  );
}

export function SystemConfig() {
  const [form] = Form.useForm();
  // F2（审查修复）：审计记录跳转改 SPA 内导航（window.open 新标签既破坏
  // SPA 内导航，又让 ?entity_type= 深链参数在无痕会话中丢失）
  const navigate = useNavigate();
  const [data, setData] = useState<LlmConfigList | null>(null);
  const [loading, setLoading] = useState(true);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<LlmConfigItem | null>(null);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState<number | null>(null);
  const [testResults, setTestResults] = useState<Record<number, LlmConfigTestResult>>({});
  const [deleteTarget, setDeleteTarget] = useState<LlmConfigItem | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [revealingKey, setRevealingKey] = useState(false);
  const [revealedKey, setRevealedKey] = useState<string | null>(null);
  const [modelOptions, setModelOptions] = useState<{ value: string }[]>([]);
  const [modelOpen, setModelOpen] = useState(false);
  // 用户是否正在手动输入过滤词（区别于程序化 value 如获取模型自动选中/已有模型值）：
  // 为 true 时下拉按输入过滤，为 false 时显示全部 options（修复"获取模型后看不到全部模型"）
  const [modelSearching, setModelSearching] = useState(false);
  const [fetchingModels, setFetchingModels] = useState(false);
  const [reordering, setReordering] = useState<{ id: number; dir: -1 | 1 } | null>(null);
  const [testingAll, setTestingAll] = useState(false);
  const hideTimerRef = useRef<number | null>(null);
  const [countdownSec, setCountdownSec] = useState(0);

  function clearReveal() {
    if (hideTimerRef.current != null) {
      window.clearInterval(hideTimerRef.current);
      hideTimerRef.current = null;
    }
    setCountdownSec(0);
    setRevealedKey(null);
    form.setFieldValue("api_key", "");
  }

  // 校验 API Key 格式（仅新建时，按提供商类型）
  function validateApiKey(_: unknown, value: string | undefined) {
    if (!value || editing) return Promise.resolve();
    const provider = form.getFieldValue("provider") as string;
    if (provider === "openai" && !value.startsWith("sk-") && !value.startsWith("sk-proj-")) {
      return Promise.reject(new Error("OpenAI API Key 通常以 sk- 开头"));
    }
    if (provider === "deepseek" && !value.startsWith("sk-")) {
      return Promise.reject(new Error("DeepSeek API Key 通常以 sk- 开头"));
    }
    if (provider === "kilo" && !value.startsWith("poolside-")) {
      return Promise.reject(new Error("kilo.ai API Key 通常以 poolside- 开头"));
    }
    return Promise.resolve();
  }

  async function handleRevealKey() {
    if (editing?.id == null) return;
    setRevealingKey(true);
    try {
      const secret = await getLlmConfigSecret(editing.id);
      form.setFieldValue("api_key", secret.api_key);
      setRevealedKey(secret.api_key);
      setCountdownSec(15);
      if (hideTimerRef.current != null) window.clearInterval(hideTimerRef.current);
      hideTimerRef.current = window.setInterval(() => {
        setCountdownSec((prev) => {
          if (prev <= 1) {
            if (hideTimerRef.current != null) {
              window.clearInterval(hideTimerRef.current);
              hideTimerRef.current = null;
            }
            form.setFieldValue("api_key", "");
            setRevealedKey(null);
            message.info("密钥已自动隐藏（未保存则不会改动）");
            return 0;
          }
          return prev - 1;
        });
      }, 1000);
      message.success("明文仅在前端保留 15 秒，已写入审计日志");
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "读取密钥失败",
      );
    } finally {
      setRevealingKey(false);
    }
  }

  async function handleCopyKey() {
    if (!revealedKey) return;
    try {
      await navigator.clipboard.writeText(revealedKey);
      message.success("已复制到剪贴板，请尽快粘贴", 2);
    } catch {
      message.error("复制失败，请手动选中密钥文本复制");
    }
  }

  async function load(): Promise<LlmConfigList | null> {
    setLoading(true);
    try {
      const d = await getLlmConfigs();
      setData(d);
      return d;
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败",
      );
      return null;
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    load();
  }, []);

  function handleProviderChange(provider: string) {
    const preset = PROVIDER_PRESETS[provider];
    if (preset) {
      form.setFieldsValue({ base_url: preset.base_url, model: preset.model });
    }
  }

  function openCreate() {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ provider: "custom", timeout: 30, enabled: true, priority: 0 });
    clearReveal();
    setModelOptions([]);
    setModelOpen(false);
    setModelSearching(false);
    setModalOpen(true);
  }

  function openEdit(item: LlmConfigItem) {
    setEditing(item);
    form.resetFields();
    form.setFieldsValue({
      name: item.name,
      provider: item.provider || "custom",
      base_url: item.base_url,
      model: item.model,
      timeout: item.timeout,
      enabled: item.enabled,
      priority: item.priority,
      // api_key 不回填明文：留空表示保持原密钥，按需经「显示密钥」按钮解密回显
    });
    clearReveal();
    setModelOptions([]);
    setModelOpen(false);
    setModelSearching(false);
    setModalOpen(true);
  }

  async function handleFetchModels() {
    try {
      const values = await form.validateFields(["base_url"]);
      const apiKey = (form.getFieldValue("api_key") as string) || "";
      setFetchingModels(true);
      setModelOptions([]);
      const res = await fetchLlmModels({
        instance_id: editing?.id ?? undefined,
        base_url: values.base_url,
        api_key: apiKey || undefined,
        timeout: (form.getFieldValue("timeout") as number) ?? 30,
        provider: (form.getFieldValue("provider") as string) || "custom",
      });
      if (res.supported && res.models.length > 0) {
        setModelOptions(res.models.map((m) => ({ value: m })));
        // 当前模型为空时自动选中第一个，并展开下拉供点选/改选（方案 C：像选项框一样可交互）
        const curModel = (form.getFieldValue("model") as string) || "";
        if (!curModel) form.setFieldValue("model", res.models[0]);
        // 展开时显示全部模型（不按自动选中/已有值过滤）——否则 filterOption 会按输入框值把列表滤成子集
        setModelSearching(false);
        setModelOpen(true);
        const preview = res.models.slice(0, 5).join("、");
        const suffix = res.models.length > 5 ? ` 等 ${res.models.length} 个` : "";
        if (res.source === "catalog") {
          // 火山方舟/腾讯云混元等兼容网关不提供 /models：展示内置常用模型目录
          message.info(
            res.note || "该网关不支持 /models 接口，已列出平台常用模型，可从中选择或手动输入",
          );
        } else {
          message.success(`获取到 ${res.models.length} 个可用模型：${preview}${suffix}`);
        }
      } else {
        message.warning(
          res.error || "该网关不支持 /models 接口，请手动输入模型名称",
        );
      }
    } catch (err) {
      if (err instanceof Error && "errorFields" in err) return; // 表单校验错误，已高亮
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "获取模型失败",
      );
    } finally {
      setFetchingModels(false);
    }
  }

  async function handleSave() {
    try {
      const values = await form.validateFields();
      setSaving(true);
      const payload: LlmConfigPayload = {
        name: values.name || "",
        provider: values.provider,
        base_url: values.base_url,
        model: values.model,
        api_key: values.api_key || "",
        timeout: values.timeout,
        enabled: values.enabled,
        priority: values.priority ?? 0,
      };
      // P0-4 生效反馈：记录保存前位次（编辑场景），保存后对比给出"新位次"
      const prevRank = editing?.id != null ? computeRank(data?.items ?? [], editing.id) : -1;
      let savedId: number;
      if (editing && editing.id != null) {
        await updateLlmConfig(editing.id, payload);
        savedId = editing.id;
      } else {
        const created = await createLlmConfig(payload);
        savedId = created.id;
      }
      setModalOpen(false);
      clearReveal();
      const fresh = await load();
      const newRank = fresh ? computeRank(fresh.items ?? [], savedId) : -1;
      // P0-4：Toast 明确生效反馈与位次变化
      if (fresh && newRank > 0) {
        if (prevRank > 0 && prevRank !== newRank) {
          message.success(
            `实例已保存，轮询位次 第 ${prevRank} 位 → 第 ${newRank} 位（下次请求起效）`,
          );
        } else {
          message.success(`实例已保存，当前轮询位次 第 ${newRank} 位（下次请求起效）`);
        }
      } else {
        message.success(editing ? "LLM 实例已更新" : "LLM 实例已新增");
      }
      // P0-2 保存后一键启用流：自动跑连通性测试，失败给"去编辑密钥"引导
      await autoTestAfterSave(savedId);
    } catch (err) {
      if (err instanceof Error && "errorFields" in err) return; // 表单校验错误，已高亮
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "保存失败",
      );
    } finally {
      setSaving(false);
    }
  }

  /** P0-2：保存后自动测试该实例，结果写入 testResults 供徽标展示。 */
  async function autoTestAfterSave(id: number) {
    setTestingId(id);
    setTestResults((prev) => ({ ...prev, [id]: undefined as unknown as LlmConfigTestResult }));
    try {
      const res = await testLlmConfig({ instance_id: id });
      setTestResults((prev) => ({ ...prev, [id]: res }));
      if (res.ok) {
        message.success(`实例已启用 · 连通正常（${res.latency_ms}ms）`);
      } else {
        message.warning(`实例已保存但连通失败：${res.error || "未知错误"}`);
      }
    } catch (err) {
      setTestResults((prev) => ({
        ...prev,
        [id]: {
          ok: false,
          latency_ms: 0,
          model: "",
          error: err instanceof UnisenseApiError ? err.message : "测试失败",
        },
      }));
      message.warning(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "连通测试失败",
      );
    } finally {
      setTestingId(null);
    }
  }

  async function handleDelete() {
    if (deleteTarget?.id == null) return;
    setDeleting(true);
    try {
      await deleteLlmConfig(deleteTarget.id);
      message.success(`已删除实例「${deleteTarget.name || deleteTarget.provider}」`);
      setDeleteTarget(null);
      load();
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "删除失败",
      );
    } finally {
      setDeleting(false);
    }
  }

  async function handleTest(item: LlmConfigItem) {
    if (item.id == null) return;
    const id: number = item.id;
    setTestingId(id);
    setTestResults((prev) => ({ ...prev, [id]: undefined as unknown as LlmConfigTestResult }));
    try {
      const res = await testLlmConfig({ instance_id: id });
      setTestResults((prev) => ({ ...prev, [id]: res }));
    } catch (err) {
      setTestResults((prev) => ({
        ...prev,
        [id]: {
          ok: false,
          latency_ms: 0,
          model: item.model || "",
          error: err instanceof UnisenseApiError ? err.message : "测试失败",
        },
      }));
    } finally {
      setTestingId(null);
    }
  }

  /** P1-5：把实例转成更新载荷（api_key 留空 = 保持原密钥，不参与位次调整）。 */
  function payloadFromItem(item: LlmConfigItem): LlmConfigPayload {
    return {
      name: item.name || "",
      provider: item.provider,
      base_url: item.base_url,
      model: item.model,
      api_key: "",
      timeout: item.timeout,
      enabled: item.enabled,
      priority: item.priority,
    };
  }

  /** P1-5：上移/下移一位——严格与相邻实例交换位次、其余实例位次不动。
   *
   * 排序键 (enabled, priority, id)：
   * - 相邻优先级不同：直接交换两者 priority（其余实例 priority 数值与位次均不动）。
   * - 相邻优先级相同（按 ID 并列）：同优先级连续区间整体重排（见
   *   ``buildSamePrioritySwapUpdates``）——段内相邻交换后重写为连续递增 priority，
   *   段内其他实例相对位次不变（仅数值变化），段外实例完全不动。
   */
  async function moveRank(id: number, dir: -1 | 1) {
    const ranked = (data?.items ?? [])
      .filter((i) => i.id != null && i.source !== "env")
      .sort((a, b) => {
        if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
        if (a.priority !== b.priority) return a.priority - b.priority;
        return (a.id ?? 0) - (b.id ?? 0);
      });
    const idx = ranked.findIndex((i) => i.id === id);
    if (idx === -1) return;
    const cur = ranked[idx];
    const target = ranked[idx + dir];
    if (!cur || !target || cur.id == null || target.id == null) return;
    setReordering({ id, dir });
    try {
      if (cur.priority !== target.priority) {
        // 相邻优先级不同：直接交换两个 priority（严格相邻交换，其余完全不动）
        await updateLlmConfig(cur.id, { ...payloadFromItem(cur), priority: target.priority });
        await updateLlmConfig(target.id, { ...payloadFromItem(target), priority: cur.priority });
      } else {
        // 同优先级：区间整体重排（严格相邻交换 + 段外不动）
        const updates = buildSamePrioritySwapUpdates(ranked, idx, dir);
        if (updates == null) {
          message.error("位次调整失败：优先级空间不足（实例过多），请先调整部分实例的优先级");
          return;
        }
        for (const u of updates) {
          const item = ranked.find((i) => i.id === u.id);
          if (item) {
            await updateLlmConfig(u.id, { ...payloadFromItem(item), priority: u.priority });
          }
        }
      }
      message.success(`已将「${cur.name || cur.provider}」${dir === -1 ? "上移" : "下移"}一位（下次请求起效）`);
      await load();
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "调整位次失败",
      );
    } finally {
      setReordering(null);
    }
  }

  /** P1-7：并行测试全部数据库实例，生成集群健康报告。 */
  async function handleTestAll() {
    const targets = (data?.items ?? []).filter((i) => i.id != null && i.source !== "env");
    if (targets.length === 0) {
      message.info("没有可测试的数据库实例（env 兜底不计入）");
      return;
    }
    setTestingAll(true);
    setTestResults({});
    const results: Record<number, LlmConfigTestResult> = {};
    try {
      await Promise.all(
        targets.map(async (t) => {
          const id = t.id as number;
          try {
            const res = await testLlmConfig({ instance_id: id });
            results[id] = res;
          } catch (err) {
            results[id] = {
              ok: false,
              latency_ms: 0,
              model: t.model || "",
              error: err instanceof UnisenseApiError ? err.message : "测试失败",
            };
          }
        }),
      );
    } finally {
      setTestResults(results);
      setTestingAll(false);
      const ok = Object.values(results).filter((r) => r?.ok).length;
      const fail = targets.length - ok;
      if (fail === 0) {
        message.success(`集群健康：全部 ${targets.length} 个实例连通正常`);
      } else {
        message.warning(`集群健康：${ok} 可用 / ${fail} 失败`);
      }
    }
  }

  const { can } = usePermission();
  // 保留后端 can_edit 字段兜底 + 叠加 system-config:edit 权限点（自定义角色被授权后也能看到写按钮）
  const canEdit = (data?.can_edit ?? false) || can("system-config:edit");
  // P1-5：同优先级冲突计数（仅数据库实例）
  const priorityCount = new Map<number, number>();
  (data?.items ?? [])
    .filter((i) => i.id != null && i.source !== "env")
    .forEach((i) => priorityCount.set(i.priority, (priorityCount.get(i.priority) ?? 0) + 1));
  // P1-5：真实轮询排序（供位次列判断是否可上移/下移）
  const rankedItems = (data?.items ?? [])
    .filter((i) => i.id != null && i.source !== "env")
    .sort((a, b) => {
      if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
      if (a.priority !== b.priority) return a.priority - b.priority;
      return (a.id ?? 0) - (b.id ?? 0);
    });
  const columns: ColumnsType<LlmConfigItem> = [
    {
      title: "名称",
      dataIndex: "name",
      key: "name",
      width: 140,
      render: (v: string, r) =>
        v || (r.source === "env" ? <Tag>环境变量</Tag> : <span className="muted">未命名</span>),
    },
    {
      title: "提供商",
      dataIndex: "provider",
      key: "provider",
      width: 130,
      render: (v: string) => PROVIDER_PRESETS[v]?.label ?? v,
    },
    {
      title: "接口地址",
      dataIndex: "base_url",
      key: "base_url",
      render: (v: string) => <span className="mono">{v || "—"}</span>,
    },
    {
      title: "模型",
      dataIndex: "model",
      key: "model",
      width: 160,
      render: (v: string) => <span className="mono">{v || "—"}</span>,
    },
    {
      title: "优先级",
      dataIndex: "priority",
      key: "priority",
      width: 90,
      render: (v: number, r) => {
        if (r.source === "env") return "—";
        const conflict = (priorityCount.get(v) ?? 0) > 1;
        return (
          <Space size={4}>
            <span className="mono">{v}</span>
            {conflict ? (
              <Tooltip title={`有 ${priorityCount.get(v)} 个实例优先级相同，轮询顺序按创建顺序（ID）决定`}>
                <WarningOutlined style={{ color: "#faad14" }} data-testid="priority-conflict" />
              </Tooltip>
            ) : null}
          </Space>
        );
      },
    },
    {
      title: "轮询位次",
      key: "rank",
      width: 130,
      render: (_: unknown, r) => {
        if (r.source === "env") return <Tag>兜底</Tag>;
        const rank = r.id != null ? computeRank(data?.items ?? [], r.id) : 0;
        if (rank <= 0) return <span className="muted">—</span>;
        return (
          <Space size={2}>
            <span className="mono" style={{ fontWeight: 600 }}>
              第 {rank} 位
            </span>
            {canEdit && r.id != null ? (
              <>
                <Tooltip title="上移一位（提升优先级）">
                  <Button
                    size="small"
                    type="text"
                    icon={<ArrowUpOutlined />}
                    disabled={rank <= 1 || (reordering?.id === r.id && reordering.dir !== -1)}
                    loading={reordering?.id === r.id && reordering.dir === -1}
                    onClick={() => moveRank(r.id as number, -1)}
                    aria-label={`上移 ${r.name || r.provider}`}
                  />
                </Tooltip>
                <Tooltip title="下移一位（降低优先级）">
                  <Button
                    size="small"
                    type="text"
                    icon={<ArrowDownOutlined />}
                    disabled={rank >= rankedItems.length || (reordering?.id === r.id && reordering.dir !== 1)}
                    loading={reordering?.id === r.id && reordering.dir === 1}
                    onClick={() => moveRank(r.id as number, 1)}
                    aria-label={`下移 ${r.name || r.provider}`}
                  />
                </Tooltip>
              </>
            ) : null}
          </Space>
        );
      },
    },
    {
      title: "启用",
      dataIndex: "enabled",
      key: "enabled",
      width: 90,
      render: (v: boolean, r) =>
        r.source === "env" ? (
          <Tag color="blue">env</Tag>
        ) : v ? (
          <Tag color="green">已启用</Tag>
        ) : (
          <Tag>未启用</Tag>
        ),
    },
    ...(canEdit
      ? [
          {
            title: "操作",
            key: "actions",
            width: 240,
            render: (_: unknown, r: LlmConfigItem) => (
              <Space size={4}>
                <Button
                  size="small"
                  icon={<ThunderboltOutlined />}
                  loading={testingId === r.id}
                  onClick={() => handleTest(r)}
                >
                  测试
                </Button>
                <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)}>
                  编辑
                </Button>
                <Button
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => setDeleteTarget(r)}
                >
                  删除
                </Button>
              </Space>
            ),
          },
        ]
      : []),
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">系统管理 / Settings</div>
          <h2>系统配置</h2>
          <p>平台级配置项：配置多个 LLM 实例后按优先级轮询路由，单实例不可用时自动切换。</p>
        </div>
      </div>

      <Card
        title={
          <Space>
            <ApiOutlined />
            <span>LLM 路由配置</span>
            <Tag color={data?.effective?.source === "none" ? "default" : "green"}>
              {SOURCE_LABEL[data?.effective?.source ?? "none"]}
            </Tag>
            <Tag color="geekblue">轮询 · 故障转移</Tag>
          </Space>
        }
        size="small"
        style={{ marginBottom: 16 }}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="多实例高可用"
          description="请求按优先级轮询选择实例；某实例调用失败时自动切换到下一个可用实例，连续失败的实例进入冷却（约 30 秒）自动恢复，避免单个 LLM 不可用导致 AI 问数 / 指标命名推断不可用。"
        />
        {data && <RoutingOverview data={data} testResults={testResults} />}
        <Table<LlmConfigItem>
          rowKey={(r) => String(r.id ?? `env-${r.base_url}`)}
          columns={columns}
          dataSource={data?.items ?? []}
          loading={loading}
          pagination={false}
          size="small"
          locale={{ emptyText: "尚未配置 LLM 实例" }}
        />
        {(() => {
          const entries = Object.values(testResults).filter((r) => r != null);
          if (entries.length >= 2) {
            const ok = entries.filter((r) => r.ok).length;
            const fail = entries.length - ok;
            return (
              <div style={{ marginTop: 10 }} data-testid="cluster-health">
                <Tag color={fail === 0 ? "success" : "warning"} icon={fail === 0 ? <CheckCircleOutlined /> : <WarningOutlined />}>
                  集群健康：{ok} 可用 / {fail} 失败（共 {entries.length}）
                </Tag>
              </div>
            );
          }
          return null;
        })()}
        {Object.entries(testResults).map(([id, res]) =>
          res ? (
            <div key={id} style={{ marginTop: 8 }}>
              <Space size={8}>
                <TestResultBadge result={res} />
                {!res.ok && canEdit ? (
                  <Button
                    size="small"
                    type="link"
                    icon={<EditOutlined />}
                    onClick={() => {
                      const item = (data?.items ?? []).find((i) => i.id === Number(id));
                      if (item) openEdit(item);
                    }}
                  >
                    去编辑密钥
                  </Button>
                ) : null}
              </Space>
            </div>
          ) : null,
        )}
        <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center" }}>
          {canEdit ? (
            <>
              <Button
                type="primary"
                icon={<PlusOutlined />}
                onClick={openCreate}
              >
                新增 LLM 实例
              </Button>
              <Button
                icon={<ThunderboltOutlined />}
                loading={testingAll}
                disabled={(data?.items ?? []).filter((i) => i.id != null && i.source !== "env").length === 0}
                onClick={handleTestAll}
              >
                全部测试
              </Button>
              <span className="muted" style={{ fontSize: 12 }}>
                并行测试所有实例，一键生成集群健康报告
              </span>
            </>
          ) : (
            <span className="muted" style={{ fontSize: 12 }}>
              仅平台管理员 / 域管理员可配置 LLM 实例。
            </span>
          )}
        </div>
      </Card>

      <Modal
        title={editing ? `编辑 LLM 实例${editing.name ? `（${editing.name}）` : ""}` : "新增 LLM 实例"}
        open={modalOpen}
        onCancel={() => {
          clearReveal();
          setModalOpen(false);
        }}
        onOk={handleSave}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        width={560}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="实例名称" style={{ marginBottom: 12 }}>
            <Input placeholder="如：主用 DeepSeek / 备用通义" maxLength={64} />
          </Form.Item>
          <Form.Item
            name="provider"
            label="提供商"
            rules={[{ required: true, message: "请选择提供商" }]}
            style={{ marginBottom: 12 }}
          >
            <Select
              options={Object.entries(PROVIDER_PRESETS).map(([value, p]) => ({
                value,
                label: p.label,
              }))}
              onChange={handleProviderChange}
            />
          </Form.Item>
          <Form.Item
            name="base_url"
            label="接口地址（OpenAI 兼容）"
            rules={[{ required: true, message: "请输入接口地址" }]}
            style={{ marginBottom: 12 }}
          >
            <Input placeholder="https://api.deepseek.com" className="mono" />
          </Form.Item>
          <Form.Item label="模型名称" required style={{ marginBottom: 12 }}>
            <Space.Compact style={{ width: "100%" }}>
              <Form.Item
                name="model"
                noStyle
                rules={[{ required: true, message: "请输入模型名称" }]}
              >
                <AutoComplete
                  aria-label="模型名称"
                  options={modelOptions}
                  open={modelOpen}
                  onDropdownVisibleChange={setModelOpen}
                  onSearch={(v) => setModelSearching(v.length > 0)}
                  onFocus={() => {
                    // 已有模型列表时聚焦即展开（空值聚焦也可见全部选项）
                    if (modelOptions.length > 0) setModelOpen(true);
                  }}
                  placeholder="deepseek-chat"
                  className="mono"
                  style={{ width: "100%" }}
                  filterOption={(inputValue, option) => {
                    // 仅在用户主动输入时按输入过滤；程序化 value（获取模型自动选中/已有模型值）不过滤，展示全部
                    if (!modelSearching) return true;
                    return String(option?.value ?? "")
                      .toLowerCase()
                      .includes(inputValue.toLowerCase());
                  }}
                />
              </Form.Item>
              <Button
                icon={<CloudDownloadOutlined />}
                loading={fetchingModels}
                onClick={handleFetchModels}
              >
                获取模型
              </Button>
            </Space.Compact>
          </Form.Item>
          <div style={{ marginTop: -8, marginBottom: 12 }}>
            <span className="muted" style={{ fontSize: 12 }}>
              点击「获取模型」从当前接口拉取可用模型列表；网关不支持 /models 时（如火山方舟/腾讯混元）自动列出平台常用模型，也可手动输入。
            </span>
          </div>
          <Form.Item
            name="api_key"
            label={editing ? "API Key（留空保持原密钥）" : "API Key"}
            rules={
              editing
                ? []
                : [
                    { required: true, message: "请输入 API Key" },
                    { validator: validateApiKey },
                  ]
            }
            style={{ marginBottom: 12 }}
            extra={
              editing ? (
                <span className="muted" style={{ fontSize: 12 }}>
                  留空保持原密钥；格式示例：<span className="mono">sk-...</span>。密钥加密存储，编辑后不会泄露。
                </span>
              ) : (
                <span className="muted" style={{ fontSize: 12 }}>
                  不同提供商密钥格式不同（OpenAI/DeepSeek 以 <span className="mono">sk-</span> 开头，kilo.ai 以 <span className="mono">poolside-</span> 开头）。密钥加密存储。
                </span>
              )
            }
          >
            <Input.Password
              placeholder={
                editing && editing.has_api_key ? "已配置（留空保持不变）" : "sk-..."
              }
              autoComplete="new-password"
              visibilityToggle={false}
              suffix={
                revealedKey ? (
                  <Button
                    size="small"
                    type="text"
                    icon={<CopyOutlined />}
                    onClick={handleCopyKey}
                    aria-label="复制密钥"
                  />
                ) : editing && form.getFieldValue("api_key") ? (
                  <Button
                    size="small"
                    type="text"
                    icon={<CloseCircleOutlined />}
                    onClick={() => {
                      form.setFieldValue("api_key", "");
                      message.info("已清空密钥输入");
                    }}
                    aria-label="清空密钥"
                  />
                ) : undefined
              }
            />
          </Form.Item>
          {editing?.has_api_key ? (
            <div style={{ marginTop: -8, marginBottom: 12 }}>
              <Space size={8}>
                <Button
                  size="small"
                  icon={<EyeOutlined />}
                  loading={revealingKey}
                  onClick={handleRevealKey}
                >
                  {revealedKey
                    ? `已显示（${countdownSec} 秒后自动隐藏）`
                    : "显示密钥"}
                </Button>
                <span className="muted" style={{ fontSize: 12 }}>
                  密钥加密存储，仅按需解密显示（查看记录会写入审计日志）
                </span>
                {revealedKey ? (
                  <Button
                    size="small"
                    type="link"
                    icon={<CopyOutlined />}
                    onClick={handleCopyKey}
                  >
                    复制
                  </Button>
                ) : null}
                {revealedKey ? (
                  <Button
                    size="small"
                    type="link"
                    icon={<LinkOutlined />}
                    onClick={() => {
                      navigate("/audit?entity_type=llm_config");
                    }}
                  >
                    审计记录
                  </Button>
                ) : null}
              </Space>
            </div>
          ) : null}
          <Space size={24}>
            <Form.Item
              name="timeout"
              label="超时（秒）"
              rules={[{ required: true, message: "请输入超时" }]}
              style={{ marginBottom: 12 }}
            >
              <InputNumber min={1} max={300} />
            </Form.Item>
            <Form.Item
              name="priority"
              label="路由优先级"
              tooltip="数值小者优先轮询（0 为最高优先级）"
              style={{ marginBottom: 12 }}
            >
              <InputNumber min={0} max={100} />
            </Form.Item>
            <Form.Item name="enabled" label="启用" valuePropName="checked" style={{ marginBottom: 12 }}>
              <Switch />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      <Modal
        title="删除 LLM 实例"
        open={deleteTarget != null}
        onCancel={() => setDeleteTarget(null)}
        onOk={handleDelete}
        confirmLoading={deleting}
        okText="删除"
        okButtonProps={{ danger: true }}
        cancelText="取消"
        width={480}
      >
        {deleteTarget ? (
          (() => {
            const items = data?.items ?? [];
            const dbItems = items.filter((i) => i.id != null && i.source !== "env");
            const isEffective =
              data?.effective?.base_url != null && data.effective.base_url === deleteTarget.base_url;
            const remaining = dbItems.filter((i) => i.id !== deleteTarget.id);
            const remainingEnabled = remaining.filter((i) => i.enabled).length;
            const willFallback = remainingEnabled === 0;
            const hasEnv = items.some((i) => i.source === "env");
            const rank = deleteTarget.id != null ? computeRank(items, deleteTarget.id) : 0;
            return (
              <>
                <p>
                  确认删除实例「
                  <strong>{deleteTarget.name || deleteTarget.provider}</strong>
                  」？
                </p>
                <ul style={{ margin: "8px 0 0 18px", padding: 0, fontSize: 13, lineHeight: 2 }}>
                  <li>
                    当前轮询位次：<b>第 {rank} 位</b>
                  </li>
                  <li>
                    {isEffective ? (
                      <Tag color="orange" data-testid="delete-effective">
                        当前路由正在使用该实例
                      </Tag>
                    ) : (
                      <span className="muted">非当前路由实例</span>
                    )}
                  </li>
                  <li>
                    删除后剩余 <b>{remaining.length}</b> 个数据库实例（{remainingEnabled} 个启用）
                  </li>
                  <li>
                    {willFallback ? (
                      hasEnv ? (
                        <span>
                          将<b>回落到环境变量</b>配置（{data?.effective?.provider}）
                        </span>
                      ) : (
                        <b style={{ color: "#cf1322" }}>
                          LLM 将处于未配置状态，AI 问数 / 指标命名推断会失效
                        </b>
                      )
                    ) : (
                      <span className="muted">其余启用实例继续参与轮询路由</span>
                    )}
                  </li>
                </ul>
                <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
                  删除后该实例不再参与轮询路由；此操作将保留审计记录。
                </p>
              </>
            );
          })()
        ) : null}
      </Modal>

      <div className="muted" style={{ fontSize: 12 }}>
        配置优先使用数据库存储；未配置任何实例时回落到环境变量（UNISENSE_LLM_*）。保存后 AI
        问数与指标命名推断立即使用新配置（轮询路由）。
      </div>
    </div>
  );
}
