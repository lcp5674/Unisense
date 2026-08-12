# Runbook：AI 问数服务（ai）

> 模块状态：`released`（门禁 13/13 绿：lint/type/unit/integration/security_reverse/chaos/observability/contract/doc_sync/perf_baseline/runbook/secret/supply_chain；§1.5 人工 ratify 待补）
> 关联文档：TD §12.7、FR-14、docs/CHANGELOG_MODULES.md

## 1. 职责边界

AI 问数服务负责：

- **NL2SQL（真实 LLM + 关键词降级）**：优先调用 OpenAI 协议兼容 LLM 将自然语言转换为对 `unified_metric` 的受限 SELECT；LLM 不可达/未配置时降级为词汇锚定关键词匹配。
- **语义锚定**：将查询锚定到已注册词汇（发布术语名 + 指标编码/名称），未锚定且未生成 SQL 时返回 `safe=False` 拒绝。
- **注入防护**：禁用词（`select *`/`delete`/`drop`/`;`/`--` 等）+ 生成 SQL 二次安全校验（UNSAFE_QUERY 拒）。
- **执行委托**：`execute=True` 时标注委托 consume 服务执行（`/api/v1/consume/query`）。

**依赖**：MySQL（只读 `term` / `metric` 取词汇表）、LLM 网关（OpenAI 协议兼容，可选）、audit 服务（写审计）。

## 2. LLM 配置（OpenAI 协议兼容，含国内主流大模型）

通过环境变量配置（开发/测试示例为 kilo.ai 网关）：

| 环境变量 | 说明 | 示例 |
|----------|------|------|
| `UNISENSE_LLM_BASE_URL` | OpenAI 兼容网关 base_url | `https://api.kilo.ai/api/gateway` / `https://api.deepseek.com` |
| `UNISENSE_LLM_API_KEY` | API 密钥（运行时注入，勿入库） | `eyJhbGciOi...` |
| `UNISENSE_LLM_DEFAULT_MODEL` | 默认模型 | `poolside/laguna-m.1:free` / `deepseek-chat` |

支持的提供商（`services/llm/client.py` `_PROVIDER_DEFAULTS`）：`deepseek` / `qwen`（通义千问）/ `ernie`（文心一言）/ `kilo`（测试网关）等，均可通过 OpenAI 协议 `POST /v1/chat/completions` 访问。

**结构化输出**：chat 统一返回 `LlmStructuredOutput`（`content`/`confidence`/`reasoning`/`candidates`），经 Pydantic 校验；LLM 输出非法 JSON 时包装为默认结构（confidence=0.5 标记需人工复核），不中断主流程。

**验证**（本地容器）：
```bash
docker compose exec backend sh -c 'echo "key_len=${#UNISENSE_LLM_API_KEY}"'  # 应 > 0
curl -s -X POST http://localhost:8100/api/v1/ai/nl2sql \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"nl_query":"查询上月销售额"}'   # method=llm 表示真实调用 LLM
```

## 3. 关键端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/ai/nl2sql` | 自然语言转 SQL（write，body `nl_query` / `execute` / `metric_scope`） |

## 4. 业务语义（关键）

`nl2sql` 执行顺序：

1. 危险语义预检：`nl_query` 含禁用词 → `UNSAFE_QUERY` 拒绝。
2. 取词汇表（term.name + metric.metric_code/name），`metric_scope` 可收窄。
3. **LLM 生成**（`llm.enabled` 时）：构造含词汇表的 prompt → `chat(temperature=0)` → 提取 SQL（剥离 markdown 代码块，要求以 `SELECT` 开头）。
4. **降级**：LLM 未配置/失败/输出非 SELECT → 关键词锚定生成参数化 SQL（`:param` 占位符，无字符串拼接）。
5. **安全校验**：生成 SQL 再跑禁用词检测，命中 → `UNSAFE_QUERY` 拒绝。
6. **锚定判定**：无锚定词且 SQL 为空 → `safe=False` + 提示使用已注册名称。

> **降级语义**：LLM 网关不可达时自动降级关键词匹配（`method=keyword`），`nl2sql` 不抛异常、不阻塞。

## 5. 依赖与故障

| 依赖 | 故障表现 | 处置 |
|------|----------|------|
| MySQL（term / metric 只读） | 5xx | 确认上游迁移 `upgrade head`；连接池 |
| LLM 网关不可达 | `method=keyword` 降级（日志 `LLM SQL 生成失败`） | 检查 `UNISENSE_LLM_BASE_URL` / 网络；不影响主流程 |
| LLM 未配置 | 直接关键词降级 | 开发测试按 §2 配置环境变量 |
| 词汇表为空 | 返回 `safe=False` | 确认术语/指标已 PUBLISHED |
| RBAC（write=analyst/viewer） | 403 | 确认调用方角色 |

## 6. 迁移与回滚

- 本服务**无自有迁移**（仅只读 term/metric）。
- 回滚：代码用 K8s 版本回退；无 schema 变更。

## 7. 可观测性

- 结构化日志：`LLM SQL 生成成功` / `LLM SQL 生成失败，降级为关键词匹配` / `关键词匹配 SQL 生成（参数化）`。
- 审计：`nl2sql` 写 audit（含 safe 标志）。
- 监控重点：`method=llm` 命中率（LLM 生效占比）、拒绝率（UNSAFE_QUERY）、降级率（LLM 不可达）。

## 8. 已知限制

- LLM 生成的 SQL 仅支持单条 SELECT；复杂 join/聚合由 LLM 能力决定，未做二次改写。
- 词汇表取前 20 项注入 prompt（防超长），超大词汇表场景锚定精度受限。
- `execute=True` 委托 consume 执行，需 OLAP 可达（不可达时 consume 返回 503 降级）。
- 无结果缓存、无查询复杂度上限；极端长查询受 LLM max_tokens 与超时（默认 30s）约束。
