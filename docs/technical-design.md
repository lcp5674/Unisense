# Unisense 统一指标语义平台 · 技术设计文档（TD）

> 配套文档：`proposal.md`（PRD）。本 TD 为 PRD 第 2/6 章技术约束的细化落地，作为立项评审后技术设计与编码的直接输入。
> 约束基线：Web 浏览器形态、面向桌面大屏、私有化部署；不重造 LLM / BI / 数据源；平台不持有源数据副本。

---

## 0. 设计总览

| 项 | 决策（最小粒度） |
|----|------|
| 交付形态 | Web 浏览器（React 19 + TypeScript 5 + Ant Design 5），桌面大屏优先（≥1280px 断点），无移动端；构建产物经 Nginx 静态托管 + CDN（私有化内网可省） |
| 前端框架细节 | Vite 5 构建；React Router 6 路由；TanStack Query（缓存/重试/降级态）、Zustand（轻态）；血缘图 Cytoscape.js / AntV G6；ECharts 看板；Axios 拦截统一 `error_code`/`retry_after` |
| 后端 | Python 3.11 + FastAPI（Pydantic v2 强校验、OpenAPI 自动文档）；ASGI 运行于 `uvicorn` + `gunicorn`（多 worker，`--workers=N`，N=CPU×2+1） |
| 后端并发模型 | 纯 I/O（HTTP/DB/Redis）全异步 `async def`；Neo4j 用官方驱动**异步 session（async Bolt）**，事件循环内直接 await；若环境强制同步驱动则包 `run_in_threadpool` 隔离（避免阻塞事件循环）；CPU 密集（口径 AST 翻译）走进程池 |
| 关系存储 | MySQL 8.0（业务/配置/审计主库），InnoDB；连接池 `SQLAlchemy async` `pool_size=20`/`max_overflow=10`/`pool_pre_ping=true` |
| 图存储 | Neo4j 5.x（血缘 L1/L2、影响面、资产地图图）；Bolt 协议，连接池 `max_connection_lifetime=1h` |
| 检索 | Elasticsearch 8.x（指标/术语全文检索、同义词匹配、找数推荐召回）；中文 IK 分词器；索引 `metric_idx`/`term_idx` |
| 缓存/队列 | Redis 7（会话、查询缓存、LLM 批量任务队列、限流滑动窗口计数）；连接池 `max_connections=50`；查询缓存 TTL 按 `metric_version` 绑定失效（§12.0.2），会话 TTL=8h |
| OLAP 下推引擎 | **部署期指定其一**（平台不重造）：Doris（MPP，MySQL 兼容 JDBC 直连 FE:9030，归入 MySQL 采集通道）/ Kylin/Kyligence（预聚合加速）/ Hive+SparkSQL（批路径）；接入经 `data_source.type` 适配，方言由口径翻译层生成（§12.3） |
| 数据源采集通道 | 只读连接（不改动生产）；通道 A：平台任务库/MySQL 直连 `information_schema`；通道 B：ETL SQL + 字段注释 + 数据示例（供 LLM 解析）；分区感知（`dt`/`event_month`），增量以"新增/变更分区"为最小单位 |
| LLM 层 | LLM 适配层（模型服务网关，不重造 LLM）统一封装多供应商（智谱/通义/文心/星火/DeepSeek/Kimi + OpenAI 兼容私有化）调用/限流/熔断/降级/审计（§1.4） |
| 密钥 | Secret Manager（KMS 托管数据源凭证、API Secret、LLM `api_key`）；平台**不持有源库密钥**，脱敏还原走源权限 |
| 安全合规 | 静态加密 AES-256/KMS、传输 TLS 1.3；PII 不出域（私有化部署数据不出域，仅读取与语义编排）；导出受权限 + 行数上限 + 脱敏 + 审计（§4.11.9）；Agent 所见维度值受脱敏 |
| 口径生命周期 | 状态机 `DRAFT→PENDING_REVIEW→PUBLISHED→DEPRECATED` + `EXPERIMENTAL`（灰度，白名单可见）；F2 Owner 门禁、F11 级联校验；破坏性变更经 `PENDING_VERSION` 子状态（消费方确认 14d 超时默认接受）+ 灰度发布 + 回滚；`DEPRECATED` 须指定 `successor`；回收站软删（保留 30d，期满 F11 校验后硬删） |
| 性能预算 | 查询下推 P95<3s（小表）/ 血缘影响面 P95<500ms（深度≤5 跳）/ 图渲染>2s 降级表级概览 / 注册解析批量任务异步队列；指标分 Tier-1/2/3（SLA 与质量规则递增） |
| 部署 | 容器化（Docker）+ K8s 编排；后端无状态（HPA 按 CPU/QPS，`目标CPU=70%`）、Neo4j/ES/MySQL 有状态（StatefulSet + PV）；Secret 经 `Secret Manager` 注入 env；Ingress + TLS；一期可 docker-compose 单节点起步 |

**分层架构（对齐 PRD 2.1 三层 + 14 领域服务）**

```
┌──────────────────────────────────────────────────────────────────┐
│ Web 前端 (React19/TS/AntD5)  ·  三栏布局 ·  三态反馈 ·  溯源优先      │
│   指标目录 · 注册向导 · 血缘图 · 治理驾驶舱 · 资产地图/热力 · 待办中心  │
├──────────────────────────────────────────────────────────────────┤
│ API 网关层 (FastAPI)  ·  JWT/X-Api-Key 鉴权 · 限流(429+retry_after) │
│   · 降级中间件(舱壁) · 统一错误码/审计埋点(全写操作留痕)              │
├──────────────────────────────────────────────────────────────────┤
│ 采集层    collector(Metadata+敏感) · lineage(血缘构图) · glossary(术语) · dimension(维度映射) │
├──────────────────────────────────────────────────────────────────┤
│ 治理层    semantic(口径真相源/状态机) · conflict(仲裁) · quality(质量) · governance(RBAC/PII门禁) │
├──────────────────────────────────────────────────────────────────┤
│ 消费层    consume(查询/鉴权/限流/meta/dry-run) · ai(NL2SQL/MCP) · recommend(找数) │
│          · assetmap(资产图/热力) · notify(通知/埋点) · observability(审计/运营) │
├──────────────────────────────────────────────────────────────────┤
│ 基础设施层（平台不持有数据，仅语义编排）                             │
│  MySQL · Neo4j · ES(IK) · Redis · Cube(二期:预聚+缓存+API)         │
│  OLAP下推(Doris/Kylin/Hive) · LLM网关(多供应商+OpenAI兼容) · Secret Manager │
└──────────────────────────────────────────────────────────────────┘
```

**完整组件交互架构图（含数据流与降级边界，对应 PRD 2.1 / 4.x）**

```
┌──────────────────────── 外部依赖（平台不持有） ────────────────────────┐
│  数据源(生产库,只读)      OLAP引擎(下推)      QuickBI(嵌入)             │
│  LLM服务网关(多供应商:GLM/通义/文心/星火/DeepSeek/Kimi+OpenAI兼容)│
│    DataAgent(MCP)      邮件/Webhook                                │
└────────────┬───────────────────┬──────────────────┬───────────────────┘
             │ 采集(只读连接)      │ 下推查询           │ 嵌入令牌
             ▼                    ▼                   ▼
┌──────────────────────────── Web 前端 (React/TS) ────────────────────────────┐
│ 三栏布局 · 三态反馈 · 溯源优先 · 指标详情(版本角标+血缘下钻) · 资产地图/热力   │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                    │ HTTPS + JWT / X-Api-Key
                                    ▼
┌──────────────────────── API 网关层 (FastAPI) ──────────────────────────────┐
│ 鉴权(JWT) · 限流(429+retry_after) · 降级中间件(舱壁) · 审计埋点(全留痕)     │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
┌──────────────┐           ┌────────────────┐           ┌──────────────────┐
│ collector    │ 采集/敏感  │ semantic       │ 注册/审核  │ consume          │
│ 采集服务     │──────────▶│ 语义/指标服务   │──────────▶│ 消费/查询服务     │
│ (FR-02/03)   │ 识别+分类  │ (FR-05/06/07)  │ 状态机    │ (FR-12/13)        │
│ 通道A:info_  │          │ 口径真相源     │           │ 鉴权/限流/meta/  │
│ schema直连   │           │ F2 Owner门禁   │           │ dry-run/血缘反填 │
│ 通道B:ETL+   │           │ F11级联校验    │           │                  │
│ 样本(供LLM)  │           │ 灰度+回滚      │           │                  │
└──────┬───────┘           └───────┬────────┘           └─────────┬────────┘
       │ 元数据/敏感                │ 双写(1)                     │ 下推(2)
       ▼                           ▼                            ▼
┌──────────────┐          ┌──────────────────────── 存储/计算 ──────────────┐
│ lineage      │ 血缘构图  │ MySQL(业务/审计) · Neo4j(血缘图) · ES(IK中文检索) │
│ 血缘服务     │─────────▶│ Redis(缓存/队列/限流) · Cube(二期:预聚+缓存+API) │
│ (FR-04)      │ 影响面    │ OLAP下推: Doris(MySQL兼容JDBC:9030)/Kylin/Hive  │
│              │          └─────────────────────────────────────────────────┘
│ glossary 术语│ (FR-08) ──few-shot语料──▶ collector / semantic              │
│ dimension维度│ (FR-05/09) 映射+同源对账 ──▶ governance / consume           │
└──────────────┘
        │                           ▲
        │ CONSUMED_BY 反填          │ 同语境治理服务
        ▼                           │
┌──────────────┐          ┌────────────────┐          ┌──────────────────┐
│ conflict     │ 四类冲突  │ governance     │ RBAC/域   │ quality          │
│ 冲突服务     │─────────▶│ 权限合规服务   │ 授权/PII  │ 质量服务         │
│ (FR-09)      │ 同名/粒度 │ (FR-11)       │ 门禁/分级 │ (FR-10)          │
│ +版本+跨域   │ /跨域/版本│               │ 维度映射  │ Tier1/2/3 SLA    │
│ +PII→403 FORBIDDEN_PII 路由 │          │               │ 同源对账  │                  │
└──────────────┘          └───────┬────────┘          └──────────────────┘
                                  │ 合规复核留痕
                                  ▼
                        ┌────────────────┐
                        │ observability  │ 审计/运营/NPS (FR-16)
                        │ notify         │ 通知/埋点 (FR-17)
                        └────────────────┘
                                  ▲
                                  │ 埋点反哺
                        ┌────────────────┐
                        │ recommend(二期)│ 智能找数推荐 (FR-19)
                        │ assetmap       │ 资产地图/热力 (FR-18)
                        └────────────────┘
        ┌──────────────────────────────────────────────────────────┐
        │ ai 服务 (四期, FR-14/15): NL2SQL(语义锚定) · DataAgent MCP  │
        │   调用 LLM 网关, 不重造 LLM; 流量隔离不参与主查询           │
        └──────────────────────────────────────────────────────────┘

图例:
  (1) 双写最终一致: MySQL写→Redis事件(§12.0.1)→异步写Neo4j+ES; 失败入重试队列(指数退避)
      二期引入Cube后, PUBLISHED口径同步生成Cube定义为第四路(同重试补偿)
  (2) 下推优先: consume一律下推OLAP(部署期指定 Doris/Kylin/Hive, 经 data_source.type 适配), 平台不落地源表;
      OLAP不可达→503+retry_after(不返缓存错数, 除非accept_stale); AI流量隔离独立实例(§5.3舱壁)
  (3) 采集通道: A=直连 information_schema 取库表字段(MySQL兼容库含 Doris 归此通道); B=ETL SQL+字段注释+数据示例(供LLM解析);
      分区感知增量采集(dt/event_month), 只读连接不改动生产
  (4) 状态机门禁: DRAFT→PENDING_REVIEW→PUBLISHED→DEPRECATED; F2无Owner不可发布; F11废弃前下游级联校验;
      PII口径 approve 前须 governance.pii_review(403 FORBIDDEN_PII 路由,非普通仲裁); 变更走 PENDING_VERSION+灰度+回滚
  降级边界(舱壁): LLM✗→取消AI预填 | Neo4j✗→血缘标stale | ES✗→退MySQL LIKE | OLAP✗→503 | Cube✗→回退自研引擎 | 分级引擎✗→标UNKNOWN(§5.5)
  鉴权: 用户态JWT / 消费方X-Api-Key换短效JWT(scope/白名单); 越权→403+审计; PII跨域→403 FORBIDDEN_PII+转交合规
  性能预算: 查询P95<3s(小表) / 血缘P95<500ms(≤5跳) / 图渲染>2s降级表级 / Tier-1指标SLA与质量最严
```

> 注：评审阶段可将该文本图转为 draw.io / C4 组件图（容器级）正式版；本图已覆盖 PRD 2.1 全部模块（采集层/治理层/消费层 + 底座）、四类外部依赖、14 个领域服务、三条核心数据流（采集→双写、注册→下推、血缘反填）与降级边界。完整的**产品级端到端数据流**（12 旅程 × 指标全生命周期 × 字段级流转 × 存储流向）见 **§12.0.4**。

---

## 1. 技术选型细化

### 1.1 后端
- **框架**：FastAPI（OpenAPI 自动文档 + Pydantic v2 强校验）。
- **依赖注入**：使用 FastAPI `Depends` + 应用级容器（轻量，不引第三方 DI）。
- **ORM**：SQLAlchemy 2.0（异步 `asyncpg` 驱动 MySQL），图库用官方 `neo4j` Python 驱动（**异步 session，走 async Bolt**，事件循环内直接 await，无需线程池；若部署环境强制同步驱动，则包 `run_in_threadpool` 隔离，避免阻塞事件循环）。
- **任务队列**：Redis + `arq` 或 `celery`（LLM 批量解析、采集增量、通知推送异步执行）；一期用 `arq`（轻量、与 Redis 复用）。
- **校验**：Pydantic v2（所有入参/出参 schema，杜绝手写 json.loads）。
- **迁移**：`Alembic` 管理 MySQL schema 版本。
- **测试**：`pytest` + `httpx`(ASGI TestClient) + `pytest-asyncio`；图/ES 用 testcontainers 起临时实例。

### 1.2 前端
- **框架**：React 19 + TypeScript + Vite。
- **状态**：Zustand（轻量全局态，避免 Redux 样板）；服务端状态用 TanStack Query（缓存/重试/失效）。
- **路由**：React Router v6。
- **UI 库**：Ant Design 5（企业级组件齐全，契合数据治理大屏场景）+ 自研主题 token。
- **图可视化**：血缘/资产地图用 `@antv/g6`（或 Cytoscape），热力用 ECharts。
- **样式**：CSS-in-JS（Antd `theme` token）+ 设计 token 文件（`tokens.ts`），统一间距/圆角/色板。
- **HTTP**：`axios` 实例（统一拦截 401/403/429/降级码，注入 JWT，错误码→三态反馈）。

### 1.3 存储与检索
- **MySQL**：业务表（指标、术语、用户、权限、审计、待办、通知）。
- **Neo4j**：血缘图（`TABLE`/`FIELD`/`METRIC` 节点 + `LINEAGE_UP`/`LINEAGE_DOWN`/`CONSUMED_BY` 关系）。
- **ES**：指标/术语全文索引（支持中文 IK 分词），找数推荐召回源。
- **Redis**：① 会话 ② 查询缓存（`metric_version + params` 哈希）③ 限流计数 ④ LLM/采集任务队列。

### 1.4 LLM 适配层（呼应 1.5 不重造 LLM）

> 设计目标：**一套抽象、多家可选、私有化友好**。既要覆盖国内主流大模型厂商的官方 API，也要兼容任意 OpenAI 协议接口（含私有化/开源模型网关），让平台不绑定单一供应商、可在内网离线部署。

**架构定位**：LLM 网关是平台内部一个独立适配层（非独立部署服务），对上层（`collector` 的 LLM 解析、`ai` 的 NL2SQL/DataAgent）暴露统一的 `LLMClient` 接口；对下层按供应商分发表征。所有调用经统一拦截器做**限流 / 重试 / 超时 / 降级 / 审计**。

**统一抽象（内部接口）**
```
LLMClient（抽象基类）
 ├─ chat(messages, model, temperature, max_tokens, **kwargs) -> LLMResponse
 ├─ stream(messages, ...) -> AsyncIterator[Chunk]            # 流式（MVP 后可接）
 ├─ embed(texts) -> list[vector]                             # 冲突相似度用
 └─ health() -> bool                                          # 心跳/熔断探测
LLMResponse: { content, model, usage{tokens_in, tokens_out}, finish_reason, raw, vendor, latency_ms }
```
- 请求/响应全部走 Pydantic schema，杜绝手写 `json.loads`。
- 上层业务只依赖 `LLMClient` 与 `model` 标识，**不感知**底层是哪家厂商。

**供应商接入矩阵（一期须支持）**

| 供应商 | 接入方式 | 协议/SDK | 备注 |
|--------|----------|----------|------|
| 智谱 GLM（Zhipu/GLM-4） | 官方 `zhipuai` SDK / REST | 自有协议 | `model=glm-4-plus` 等 |
| 通义千问（Qwen / 阿里云百炼） | 官方 OpenAI 兼容端点 `/v1/chat/completions` | **OpenAI 兼容** | `DASHSCOPE_API_KEY`，模型 `qwen-plus / qwen-max` |
| 文心一言（ERNIE / 百度千帆） | 官方 `qianfan` SDK / REST（需 access_token 换取） | 自有协议 | `model=ernie-4.0` |
| 讯飞星火（iFlytek Spark） | 官方 WebSocket REST | 自有协议 | `app_id + api_key + api_secret` |
| 深度求索（DeepSeek） | 官方 OpenAI 兼容端点 `/v1/chat/completions` | **OpenAI 兼容** | `model=deepseek-chat / deepseek-reasoner` |
| 月之暗面（Kimi / Moonshot） | 官方 OpenAI 兼容端点 | **OpenAI 兼容** | `model=moonshot-v1-8k` |
| OpenAI 兼容协议（通用） | 任意实现 `/v1/chat/completions` 的网关 | **OpenAI 兼容** | 私有化 vLLM / Ollama / OneAPI / 自建代理均走此路 |
| OpenAI 官方（可选） | 官方 `openai` SDK | OpenAI 协议 | 海外/有合规通道时启用 |

**关键设计点**：
1. **双协议适配器**：`OpenAICompatibleAdapter`（覆盖 Qwen/DeepSeek/Kimi/私有化网关）与 `VendorNativeAdapter`（覆盖 GLM/ERNIE/星火等自有协议），二者都实现 `LLMClient`。新增厂商 = 新增一个 Adapter 子类 + 配置项，业务零改。
2. **OpenAI 兼容优先**：凡是支持 `/v1/chat/completions` 的端点（含 vLLM、Ollama、各种私有化推理网关、OneAPI 聚合），统一用 `OpenAICompatibleAdapter` + `base_url` 指向即可，无需写代码——满足"兼容 OpenAI 大模型协议接口"的硬性要求，也最契合**私有化部署**基线（内网起 vLLM/Ollama 即接）。
3. **凭证与密钥**：所有 `api_key` / `access_token` 经 Secret Manager 托管（呼应 §0），配置项 `LLM_PROVIDERS` 为加密 JSON，运行时注入，不落代码、不落 MySQL 明文。
4. **模型路由与默认策略**：`settings.yaml` 配置 `default_chat_model` / `default_embed_model` / 各场景 `model_alias`（如 `brief_parser→deepseek-chat`）；业务侧用语义别名而非硬编码厂商型号，切换供应商改配置即可。
5. **限流/重试/熔断**：按供应商维度独立限流（各家 QPS 不同）→ Redis 滑动窗口；失败指数退避（上游 429/5xx）；连续失败触发熔断，标记 `health=false`，路由切备用供应商或降级。
6. **降级与审计**：模型不可用 → 该供应商降级，若有备用供应商自动 failover；全部不可用 → 返 `503 AI_UNAVAILABLE`，前端走三态降级（取消 AI 预填）。所有调用入审计（谁、何时、对哪个实体、vendor、model、prompt 摘要、response 摘要、token 用量）——呼应隐私与成本控制。
7. **不缓存模型权重、不托管模型服务**（呼应 1.5）；平台仅在私有化场景"接入"内网模型网关，仍属调用方。

**配置示例（加密存储，不落代码）**
```yaml
llm:
  providers:
    - name: deepseek
      adapter: openai_compatible
      base_url: https://api.deepseek.com/v1
      api_key_ref: secret://llm/deepseek_key
      models: [deepseek-chat, deepseek-reasoner]
      rpm_limit: 60
    - name: qwen
      adapter: openai_compatible
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      api_key_ref: secret://llm/qwen_key
      models: [qwen-plus, qwen-max]
    - name: glm
      adapter: vendor_native
      sdk: zhipuai
      api_key_ref: secret://llm/glm_key
    - name: local_vllm          # 私有化：典型 OpenAI 兼容
      adapter: openai_compatible
      base_url: http://10.0.0.10:8000/v1
      api_key_ref: secret://llm/local_key
  default_chat_model: deepseek-chat
  default_embed_model: glm-4-embed
  aliases:
    brief_parser: deepseek-chat
    nl2sql: qwen-max
    similarity: glm-4-embed
```


---

## 2. 架构设计与模块边界

### 2.1 服务划分（与 PRD 4.x 对齐）
| 服务（目录） | 对应 FR | 职责 |
|-------------|--------|------|
| `collector` | FR-02/03 | 数据源连接、pull/import 采集、敏感识别（落 `classification`）、增量触发 LLM 解析 |
| `lineage` | FR-04 | L1/L2 血缘抽取与构图、影响面计算、运行时 `CONSUMED_BY` 反填 |
| `semantic` | FR-05/06/07 | 指标定义（原子/派生/复合）、状态机（口径真相源，含灰度 EXPERIMENTAL / PENDING_VERSION 版本确认）、版本化（change_type 破坏性判定 + diff + 消费方确认 + 回滚）、执行引擎编排（一期自研轻量下推；二期评估引入 Cube 作预聚合/缓存/多消费方 API 层）、口径 AST→方言 SQL 翻译层、注册审核待办、模板、驾驶舱、SLA 例外日历、结果快照存证触发 |
| `conflict` | FR-09 | 四类冲突检测（同名不同义/同义不同名/粒度单位/跨域异源 + 口径版本 + PII 路由）、仲裁工作流、裁决记录（ruling_record 知识库）、**口径漂移巡检（drift detection）** |
| `quality` | FR-10 | 异常分级、告警、SLA（含 SLA 例外日历豁免）、质量规则配置（quality_rule，按 tier/dw_layer 差异化）、**外部基准对账（benchmark 导入比对）** |
| `governance` | FR-11 | RBAC/域授权（grants）、PII 合规门禁（403 FORBIDDEN_PII 路由）、分级分类（落 `classification`）、维度映射与同源对账编排、权限申请/回收/TTL |
| `consume` | FR-12/13 | QuickBI 嵌入、Semantic API（鉴权/token 模型/限流/降级/meta 标注/dry-run/运行时冲突检测/血缘反填）、**结果快照落库**、**口径版本消费方确认回调** |
| `ai` | FR-14/15 | NL2SQL 管线（语义锚定后复用 consume 执行）、DataAgent MCP 工具集（四期） |
| `notify` | FR-16/17 | 通知中心（站内/邮件/Webhook，订阅偏好）、埋点、事件总线消费 |
| `observability` | FR-16 | 审计留痕、运营度量、NPS/反馈 |
| `assetmap` | FR-18 | 资产地图聚合渲染、责任人视图、敏感热力（监听 catalog/metric/classification 事件增量刷新） |
| `recommend` | FR-19 | 智能找数推荐（二期，协同信号加权召回） |
| `glossary` | FR-08 | 术语库（标准名/定义/边界/同义词归一/引用通知/LLM few-shot 语料，L1 补齐） |
| `dimension` | FR-05/09 | 共享维度映射、跨域指标对齐、同源口径对账（L2 补齐） |

### 2.2 跨切面对齐 PRD 约束
- **只读采集**：collector 对生产库仅 `information_schema`/只读连接（连接串强制 `readOnly`）。
- **下推优先**：consume 执行引擎一律下推到 OLAP，平台不落地源表。
- **语义层借鉴 Cube（薄封装，呼应 PRD 1.5）**：平台保留口径状态机/版本溯源/冲突仲裁/PII 门禁/血缘（这些 Cube 不做），作为**语义真相源**；指标审核通过（PUBLISHED）后，由 `semantic` 服务将口径**同步生成 Cube 的 cube 定义（YAML/JS）**，Cube 承担**下推 SQL 生成 + 预聚物化 + 结果缓存 + 多消费方 API**（`/cubejs-api/v1/load`）。`consume` 服务在 Cube 之上包一层鉴权/限流/降级/`meta` 标注，再透出给前端/DataAgent/QuickBI。一期不引入 Cube（先自研轻量执行引擎验证治理闭环），二期随治理精细化评估接入，降低执行引擎自研复杂度。
- **双写最终一致**：指标注册成功 → MySQL 写业务态 + Neo4j 写血缘 + ES 写索引；三者任一失败入重试队列，最终一致（见 §5）。若二期引入 Cube，口径 PUBLISHED 同步为第四路（Cube 定义生成），同样走重试补偿。
- **降级不阻断**：单能力故障只削弱该能力（§5 舱壁 + 降级中间件）。

---

## 3. 接口设计（REST + OpenAPI）

> 统一约定：所有响应包 `{error_code, message, retry_after, trace_id}`；`error_code` 为业务码（见 §5.4 对齐 PRD 附录 A.2）；`degraded=true` 表示命中降级。
> 统一前缀：`/api/v1`。鉴权：`Authorization: Bearer <JWT>`（用户态）或 `X-Api-Key` + JWT（消费方 `api_client`）。响应必含 `X-Request-ID` + `X-Trace-ID`。

### 3.1 采集（collector）
```
POST   /sources                      # 新增数据源（保存即触发全量采集）
GET    /sources                      # 数据源列表（含 last_scan_at/coverage）
POST   /sources/{id}/scan            # 手动触发补采
POST   /sources/{id}/import          # 通道B：提交 DDL/JSON + 样本（人工证据）
GET    /sources/{id}/coverage        # 资产清点与覆盖度看板（FR-02/北极星）
DELETE /sources/{id}                 # 软删（保留血缘历史）
```

### 3.2 指标语义层（semantic）
```
POST   /metrics                      # 注册指标（草稿态，触发冲突预检+合规门禁）
GET    /metrics?domain=&status=&q=   # 列表（ES 检索）
GET    /metrics/{code}               # 详情（含口径版本、血缘入口、meta）
PUT    /metrics/{code}               # 改口径（升版本，旧版保留；破坏性→PENDING_VERSION）
POST   /metrics/{code}/submit        # 提交审核（状态机 DRAFT→REVIEW）
POST   /metrics/{code}/approve       # 审核通过（→PUBLISHED 或 EXPERIMENTAL 灰度，合规门禁在 approve 前）
POST   /metrics/{code}/reject        # 驳回（带原因，回 DRAFT）
POST   /metrics/{code}/deprecate     # 废弃（软删，保留引用方提示）
POST   /metrics/{code}/watch         # 关注（US-DA-3）
POST   /metrics/{code}/promote       # 灰度全量（EXPERIMENTAL → PUBLISHED，观察期满）
POST   /metrics/{code}/rollback      # 灰度回滚（EXPERIMENTAL → 上一 PUBLISHED，异常一键）
GET    /metrics/{code}/versions      # 版本历史（溯源，呼应 US-DA-2）
GET    /metrics/templates            # 模板库（NEW-2）
GET    /metrics/dashboard            # 治理驾驶舱聚合（MO-1）
POST   /metrics/compare              # 指标对比工具（PRD 4.5：两指标并排 diff，同名不同义排查）
GET/POST /sla-calendar               # SLA 例外日历查询/配置（PRD 4.5，domain_admin 维护）
GET    /help                         # 帮助中心内容（静态 + 术语库概念卡关联，PRD 4.5）
```

### 3.3 血缘与资产地图（lineage / assetmap）
```
GET    /lineage/table/{id}           # 表级上下游
GET    /lineage/field/{id}           # 字段级上下游
GET    /lineage/impact/{id}          # 影响面（上游变更影响下游集合）
GET    /asset-map/overview           # 资产总览（域/分级/覆盖率）
GET    /asset-map/heatmap            # 敏感分布热力（CONFIDENTIAL/PII 着色）
GET    /asset-map/owner/{uid}        # 责任人视图（名下资产+健康度）
```

### 3.4 冲突（conflict）
```
GET    /conflicts?status=OPEN        # 待仲裁列表（GOV-1）
POST   /conflicts/{id}/arbitrate     # 裁决（选唯一口径/合并/保留差异，写 GOV-2 记录）
POST   /conflicts/{id}/escalate      # 升级（超时未决）
GET    /drift-scans?level=HIGH       # 口径漂移巡检结果列表（PRD 4.7.8）
POST   /drift-scans/run              # 手动触发漂移巡检（默认定期调度：Tier-1/2 每周 + 源头SQL变更即时）
POST   /drift-scans/{id}/confirm     # Owner 处置：CONFIRMED_INTENT(走4.5变更评审) / CONFIRMED_UNINTENT(通知改SQL) / IGNORED
```

### 3.5 权限与合规（governance）
```
POST   /roles                        # 角色（platform_admin/domain_admin/metric_owner/reviewer/compliance_officer/viewer，对齐 PRD 4.9.2）
POST   /grants                       # 域授权 + 指标白名单
POST   /grants/batch                 # 批量授权/回收（R3-07：dry-run+逐条审计+失败回滚）
POST   /grants/batch/dry-run         # 批量操作影响预览（受影响用户数/指标数）
POST   /pii/review                   # 合规官复核（COMP-1，留痕）
POST   /classification/rescan        # 分级重扫（COMP-2，频率 ≥ 每周1次，R12-09）
GET    /me/permissions               # 当前用户权限快照
```

### 3.6 消费（consume · Semantic API）

> **通用请求/响应规范（R13-12 对齐 PRD 4.11.2）**：请求必带 `Authorization: Bearer <JWT>` + `Content-Type: application/json`；响应必含 `X-Request-ID` + `X-Trace-ID`（审计关联）；API 版本响应 Header `X-API-Version: v1`。

```
POST   /query                       # 口径查询（metrics/dimensions/filters/dateRange/comparison/orderBy/idempotency_key）
POST   /query/dry-run               # 试算沙箱（不计费/不写生产/不进缓存）
GET    /query/{query_id}/cancel     # 取消在途查询
GET    /metrics?domain=&status=&q=&sort=&tier=&dw_layer=&owner=&has_pii=&page=&page_size=  # 指标检索（ES 驱动，R13-01/02 补 sort/filter 参数）
GET    /metrics/{code}               # 指标详情（含口径版本/维度/血缘入口/quality_score/serving_mode）
POST   /metrics/batch                # 批量指标详情（metric_codes 上限 20，R13-13）
GET    /metric-sets                  # 指标集检索（复用分页规范，R13-04）
GET    /metric-sets/{id}             # 指标集详情
GET    /metrics/{code}/snapshots     # 指标结果快照（分页，R13-05）
GET    /lineage/{metric_code}?depth=&direction=&confidence_min=  # 血缘查询（R13-10 补深度/方向/置信度参数）
GET    /metrics/{code}/semantic     # 只读语义拉取（api_client 用，受 scope 约束）
POST   /embed/quickbi               # 获取嵌入令牌（FR-12）
GET    /embed/quickbi/card          # 口径卡片拉取（QuickBI 侧边栏嵌入，PRD 4.11.1：不离开报表查看口径）
POST   /versions/{id}/confirm       # 破坏性变更消费方确认（PENDING_VERSION → CURRENT，PRD 5.5.1）
POST   /versions/{id}/reject        # 消费方拒绝破坏性变更（带理由，PENDING_VERSION 驳回）
GET    /snapshots?metric_code=&date_range=  # 指标结果快照查询（WORM 存证，PRD 4.5，受 4.9 权限）
GET    /me/favorites                # 我的收藏（user_preference pinned_metrics，PRD 4.5）
POST   /me/favorites/{code}         # 收藏指标
DELETE /me/favorites/{code}         # 取消收藏
GET    /me/recent                   # 最近浏览（前 20，前端记录指标详情点击）
```
**查询请求体（核心 schema，对齐 PRD 4.11.2 R13-06/07/08）**
```json
{
  "metrics": ["gmv","order_cnt"],
  "dimensions": ["region","date"],
  "filters": [{"field":"channel","op":"in","value":["app","web"]}],
  "dateRange": {"from":"2026-07-01","to":"2026-07-31","granularity":"day"},
  "comparison": {"type":"yoy","offset":1,"base_date":null},
  "orderBy": {"field":"gmv","direction":"desc"},
  "accept_stale": false,
  "accept_deprecated": false,
  "idempotency_key": "uuid-v4",
  "client_version": "1.0"
}
```
**统一响应 `meta`（呼应 4.11.7 + R13-14 serving_mode）**：`metric_version` / `freshness` / `granularity_bound` / `sample` / `stale` / `source_trace` / `quality_flag` / `serving_mode`(batch|realtime)。

### 3.7 AI（ai · 四期）
```
POST   /nl2sql                       # 自然语言→语义层查询（先锚定再生成）
POST   /mcp/tools/list               # MCP 工具清单
POST   /mcp/tools/call               # MCP 工具调用（list_metrics/query_metric/...）
```

### 3.8 通知与运营（notify / observability）
```
GET    /notifications               # 站内信（待办/冲突/认领/合规）
POST   /feedback                    # 反馈/NPS（NEW-2）
GET    /metrics/ops                 # 运营看板（DAU/搜索量/审核时效/降级率）
GET    /audit/logs                  # 审计检索（谁/何时/对何实体/何操作）
```

### 3.9 术语库（glossary · FR-08）
```
POST   /terms                       # 建术语（DRAFT）
GET    /terms?q=&domain=&status=    # 检索（ES term_idx + 同义词扩展）
GET    /terms/{code}                # 标准词/定义/边界/关联指标
PUT    /terms/{code}                # 编辑（升 term_version）
POST   /terms/{code}/submit         # → REVIEW
POST   /terms/{code}/approve        # → APPROVED（可引用）
POST   /terms/{code}/deprecate      # → DEPRECATED（填替代）
GET    /terms/conflicts             # 术语冲突候选（glossary_conflict）
```

### 3.10 维度与同源对账（dimension · L2）
```
POST   /dimensions                  # 建共享维度
GET    /dimensions                  # 维度列表
POST   /dimension-mappings          # 录源列→标准维度值映射规则
GET    /dimensions/{code}/mappings  # 映射规则查看
POST   /reconciliation/run          # 手动触发同源口径对账
GET    /reconciliation              # 对账结果（OK/WARN/ALERT）
POST   /benchmarks/import           # 外部基准值导入（Excel/CSV/API，幂等，PRD 4.8.8）
GET    /benchmarks                  # 基准绑定列表
POST   /benchmarks/{id}/bind        # 基准绑定目标指标（声明比对口径/维度/币种）
GET    /reconciliation-records      # 对账差异记录（含基准 vs 指标值 vs 差异率）
POST   /reconciliation-records/{id}/confirm  # Owner 确认差异（合理/口径有误→走变更）
```

### 3.11 消费方凭证（api_client · FR-13 token 模型）
```
POST   /api-clients                 # 申请 client_id/secret（绑定域/白名单/配额）
POST   /api-clients/{id}/token      # 换短效 Bearer JWT（含 scope/metric_whitelist/TTL）
POST   /api-clients/{id}/revoke     # 吊销（密钥托管 Secret Manager）
```

---

## 4. 数据库设计

### 4.1 MySQL 核心表（业务态，强一致）
```sql
-- 数据源
CREATE TABLE data_source (
  id BIGINT PK, source_id VARCHAR(64) UNIQUE,  -- 域区分键
  name VARCHAR(128), type ENUM('mysql','postgres','doris','hive','etl_mysql','spark','starrocks'),
  usage ENUM('COLLECT','COMPUTE'),             -- 采集源(读schema) vs 执行源(跑计算)，两通道解耦（PRD 3.4b）
  conn_json JSON,  -- 连接串引用（Secret Manager credential_ref 引用，不落明文）
  read_only BOOLEAN DEFAULT TRUE, last_scan_at DATETIME,
  coverage FLOAT,  -- 资产覆盖率
  quota JSON,      -- {max_concurrency, max_scan_rows} 防打满他人集群（PRD 4.11.9）
  health_status ENUM('healthy','unhealthy','unknown') DEFAULT 'unknown',  -- 心跳探测（PRD 3.4b）
  last_health_check DATETIME NULL,
  created_by BIGINT, created_at DATETIME, deleted_at DATETIME NULL
);

-- 元数据库表/字段（采集落库）
CREATE TABLE db_catalog (
  id BIGINT PK, source_id VARCHAR(64),
  entity_name VARCHAR(256),  -- 库.表
  entity_type ENUM('table','field'),
  schema_json JSON,          -- 字段/类型/注释/索引
  sensitivity_level ENUM('PUBLIC','INTERNAL','CONFIDENTIAL','PII'),
  owner_id BIGINT NULL,      -- 孤儿资产=NULL→待认领
  upstream_signature VARCHAR(64),  -- 幂等键 source_id+entity_name
  UNIQUE KEY uk_entity (source_id, entity_name),
  INDEX idx_owner (owner_id), INDEX idx_sens (sensitivity_level)
);

-- 指标（语义层核心，状态机）
CREATE TABLE metric (
  id BIGINT PK, metric_code VARCHAR(64) UNIQUE,
  name VARCHAR(128), domain VARCHAR(64),
  type ENUM('atomic','derived','composite'),
  -- 治理一等字段结构化（PRD 4.5：粒度/单位/聚合/时间语义/时效/分层/分级/服务模式/可加性）
  granularity VARCHAR(64),                  -- 粒度：一行代表什么（如 day×region×channel）
  unit VARCHAR(32), currency VARCHAR(16) NULL,  -- 单位与币种锚定
  aggregation ENUM('SUM','AVG','COUNT','COUNT_DISTINCT','LAST_VALUE'),
  time_semantics ENUM('PERIOD','YTD','TTM','AVG'),  -- 当期/累计/平均 + 同环比基期规则
  freshness ENUM('REALTIME','T1','HOURLY'),         -- 数据新鲜度（实时/T+1/小时级）
  sla VARCHAR(128),                                 -- 产出 SLA 契约：绑定 task_id + 就绪时间（如 "08:30"）
  dw_layer ENUM('ODS','DWD','DWS','ADS','DM'),      -- 数仓分层（驱动质量SLA/审核流/血缘精度差异化）
  metric_tier ENUM('T1','T2','T3') DEFAULT 'T3',    -- 指标分级（治理与质量规则从严到宽）
  serving_mode ENUM('BATCH_ONLY','REALTIME_ONLY','BATCH_REALTIME_DUAL'),  -- 批流双路
  additivity ENUM('ADDITIVE','SEMI_ADDITIVE','NON_ADDITIVE'),             -- 聚合可加性（下钻/上卷策略）
  non_additive_dimensions JSON NULL,                -- SEMI/NON_ADDITIVE 时的不可加维度列表
  definition_json JSON,  -- 口径：表达式/依赖指标/来源字段/分区键
  version INT DEFAULT 1,
  status ENUM('DRAFT','REVIEW','PUBLISHED','EXPERIMENTAL','DEPRECATED','DATA_SOURCE_DROPPED'),  -- EXPERIMENTAL=灰度; DATA_SOURCE_DROPPED=PUBLISHED异常子态(源表DROP/不可达, PRD 5.5.1/R5-01)
  owner_id BIGINT, backup_owner_id BIGINT NULL,   -- 主/副 Owner（离职交接兜底，PRD 4.9.6）
  approver_id BIGINT NULL,
  pii_flag BOOLEAN, compliance_reviewed BOOLEAN DEFAULT FALSE,
  effective_version INT NULL,      -- 当前生效版本（PENDING_VERSION 场景下默认查询命中的版本）
  consumption_guide JSON NULL,    -- 消费指南（Owner维护：applicable/not_applicable/common_misuse/recommended_usage，PRD R3-20）
  successor_code VARCHAR(64) NULL,-- 替代指标码（DEPRECATED 时必填，PRD 4.5 废弃与替代；消费方请求已废弃指标时返回 successor 迁移指引，E6）
  deprecated_at DATETIME NULL,   -- 废弃时间（DEPRECATED 状态切换时间，用于 Sunset 30d 倒计时，E7/E8）
  sunset_until DATE NULL,        -- Sunset 截止日期（DEPRECATED 后 30 天，期满返回 410 GONE，E7）
  created_at DATETIME, updated_at DATETIME,
  INDEX idx_status (status), INDEX idx_domain (domain), INDEX idx_tier (metric_tier)
);

-- 指标版本（溯源，US-DA-2；含破坏性判定与结构化 diff，PRD 4.5 版本化核心）
CREATE TABLE metric_version (
  id BIGINT PK, metric_id BIGINT, version INT,
  definition_json JSON, change_log VARCHAR(512),
  change_type ENUM('breaking','non_breaking'),  -- 破坏性→PENDING_VERSION 消费方确认；非破坏性→直接生效
  diff_json JSON,   -- 字段级变更列表 [{field, old_value, new_value}]，喂给破坏性判定 F5a
  status ENUM('CURRENT','PENDING_VERSION','HISTORICAL'),  -- 当前生效/待确认/历史
  confirmed_by BIGINT NULL, confirmed_at DATETIME NULL,   -- 消费方确认（破坏性变更）
  reject_reason VARCHAR(512) NULL,   -- 消费方拒绝理由（PENDING_VERSION 被驳回）
  created_by BIGINT, created_at DATETIME
);

-- 指标↔物理表 1:N（PRD 4.4 指标级血缘：替代单表假设，支撑影响面/结构变更联动）
CREATE TABLE metric_lineage_source (
  id BIGINT PK, metric_id BIGINT, source_table VARCHAR(256), source_field VARCHAR(128),
  CONSTRAINT uk_mls UNIQUE(metric_id, source_table, source_field)
);

-- 血缘边变更快照（PRD 4.4/R10-04/R11-04：血缘边本身变更触发旧边快照，供事后回溯）
CREATE TABLE lineage_edge_history (
  id BIGINT PK,
  edge_id VARCHAR(128),            -- Neo4j 边 ID
  before_source VARCHAR(256),      -- 旧源节点
  before_target VARCHAR(256),      -- 旧目标节点
  before_transform_expr TEXT NULL, -- 旧变换表达式
  before_confidence FLOAT NULL,    -- 旧置信度
  after_source VARCHAR(256),       -- 新源节点
  after_target VARCHAR(256),       -- 新目标节点
  after_transform_expr TEXT NULL,  -- 新变换表达式
  after_confidence FLOAT NULL,     -- 新置信度
  change_reason VARCHAR(128),      -- schema_drift / reparse / manual / rename
  trigger_event VARCHAR(128),      -- 触发事件类型
  changed_at DATETIME,
  changed_by BIGINT NULL,          -- 人工变更时为操作者ID
  retention_until DATETIME,        -- 热存保留到期（≥180天，对齐审计保留期）
  INDEX idx_edge (edge_id), INDEX idx_changed_at (changed_at)
);

-- 指标集（PRD 4.5/R3-15/R11-05：指标集定义与消费，版本化）
CREATE TABLE metric_set (
  id BIGINT PK, set_code VARCHAR(64) UNIQUE,
  name VARCHAR(128), domain VARCHAR(64),
  description VARCHAR(512),
  version INT DEFAULT 1,           -- 指标集版本（R11-05）
  default_dimensions JSON,         -- 默认共享维度集
  owner_id BIGINT, status ENUM('DRAFT','PUBLISHED','DEPRECATED'),
  created_at DATETIME, updated_at DATETIME,
  INDEX idx_domain (domain)
);
CREATE TABLE metric_set_item (
  id BIGINT PK, set_id BIGINT,
  metric_code VARCHAR(64),         -- 指标集内指标码
  added_version INT,               -- 加入时的指标集版本（R11-05）
  removed_version INT NULL,        -- 移除时的指标集版本（NULL=仍在集内）
  sort_order INT DEFAULT 0,
  CONSTRAINT uk_msi UNIQUE(set_id, metric_code)
);

-- 逻辑表↔物理表绑定（PRD 3.4b/4.5：批流双路、DataSet 版本、结构变更触发重算的唯一桥）
CREATE TABLE data_set (
  id BIGINT PK, dataset_code VARCHAR(64) UNIQUE,
  logic_table VARCHAR(256),      -- 语义层/血缘引用的逻辑名（口径契约）
  ds_code VARCHAR(64),           -- 绑定数据源（COMPUTE 执行源）
  physical_table VARCHAR(256),   -- 该数据源下的物理表（可因引擎/库不同而异）
  effective_version VARCHAR(64), -- 逻辑表→物理表绑定的生效版本
  serving_path ENUM('BATCH','REALTIME') DEFAULT 'BATCH',  -- 批流双路路径标记
  CONSTRAINT uk_ds UNIQUE(ds_code, physical_table, effective_version)
);

-- 物化表版本绑定（PRD 6.1：版本耦合一号制，物化表版本号=来源指标生效版本号）
CREATE TABLE materialized (
  id BIGINT PK, metric_id BIGINT, version INT,
  table_name VARCHAR(128),       -- 形如 mv_MET_SALES_AMT_v1.2
  status ENUM('ACTIVE','INVALIDATED'), created_at DATETIME
);

-- 冲突与裁决（GOV-2；四类冲突 + 状态机，PRD 4.7.4）
CREATE TABLE conflict (
  id BIGINT PK, metric_a BIGINT, metric_b BIGINT,
  type ENUM('same_name_diff_def','same_def_diff_name','grain_unit','cross_domain_same_def','version_conflict','pii'),  -- 四类+版本+PII（PRD 4.7.1）
  status ENUM('OPEN','NEGOTIATING','ESCALATED','RULED','CLOSED'),
  arbitrator_id BIGINT NULL, decision_json JSON, created_at DATETIME,
  INDEX idx_status (status)
);

-- 裁决记录（GOV-2 裁决知识库，PRD 4.7.5：沉淀为规则复用）
CREATE TABLE ruling_record (
  id BIGINT PK, conflict_id BIGINT, metric_codes JSON,
  dispute_desc TEXT, decision TEXT, reason TEXT,
  arbitrator_id BIGINT, decided_at DATETIME
);

-- 权限（角色枚举对齐 PRD 4.9.2；grants 规避 MySQL 保留字 grant）
CREATE TABLE role (
  id BIGINT PK,
  name ENUM('platform_admin','domain_admin','metric_owner','reviewer','compliance_officer','viewer')
);
CREATE TABLE grants (
  id BIGINT PK, role_id BIGINT, user_id BIGINT,
  domain VARCHAR(64), metric_whitelist JSON, row_level BOOLEAN DEFAULT FALSE,
  grant_type ENUM('READ','WRITE','READ_WRITE'),  -- 跨域只读引用(READ) vs 源域读写(WRITE)
  expires_at DATETIME NULL,   -- 临时授权 TTL，到期自动回收（PRD 4.9.6）
  CONSTRAINT uk_grant UNIQUE(user_id, role_id, domain, metric_whitelist)
);

-- 待办/通知
CREATE TABLE todo (id BIGINT PK, user_id BIGINT, type VARCHAR(32), ref_id BIGINT, status ENUM('PENDING','DONE'));
CREATE TABLE notification (
  id BIGINT PK, user_id BIGINT, channel ENUM('inapp','email','webhook'),
  payload JSON, read_at DATETIME NULL,
  status ENUM('PENDING','SENT','READ','ESCALATED','DONE') DEFAULT 'PENDING',  -- 通知状态机（对齐 §4.2/§12.9 已读回执与升级）
  event_type VARCHAR(64),      -- 关联事件类型（订阅偏好匹配用，§12.9）
  ref_id BIGINT NULL,          -- 关联业务实体（todo/metric/conflict）
  escalated_at DATETIME NULL,  -- 超时升级时间（SLA_UNREAD 后替补 Owner/上级）
  sent_at DATETIME NULL,
  INDEX idx_user_status (user_id, status), INDEX idx_event (event_type)
);

-- 审计（强留痕，呼应 4.10；WORM 只写不删；含结构化 before/after diff）
CREATE TABLE audit_log (
  id BIGINT PK, actor_id BIGINT, action VARCHAR(64),
  entity_type VARCHAR(32), entity_id BIGINT,
  before_json JSON, after_json JSON,  -- 结构化 diff，非全量快照（PRD 4.10.1）
  result ENUM('SUCCESS','FAIL','DENIED'),  -- 操作结果（越权/失败留痕）
  trace_id VARCHAR(64), ip VARCHAR(64), created_at DATETIME,
  INDEX idx_entity (entity_type, entity_id), INDEX idx_time (created_at)
);  -- 触发器禁止 UPDATE/DELETE（WORM）；热存 180d → 冷归档

-- 埋点（4.14 反哺推荐）
CREATE TABLE event_log (
  id BIGINT PK, user_id BIGINT, event VARCHAR(32),
  target_metric VARCHAR(64), ctx_json JSON, created_at DATETIME
);

-- 术语库（FR-08：业务概念标准层）
CREATE TABLE glossary_term (
  id BIGINT PK, term_code VARCHAR(64) UNIQUE, standard_name VARCHAR(128),
  aliases JSON, definition TEXT, boundaries TEXT,
  related_metrics JSON, domain VARCHAR(64), owner BIGINT,
  status ENUM('DRAFT','REVIEW','APPROVED','DEPRECATED'),
  version INT, synonym_group VARCHAR(64), created_at DATETIME, updated_at DATETIME
);
CREATE TABLE glossary_conflict (
  id BIGINT PK, term_id BIGINT, conflict_type VARCHAR(32),
  ref_term_id BIGINT NULL, ref_metric_id BIGINT NULL,
  status ENUM('OPEN','RESOLVED','IGNORED'), resolver BIGINT NULL, created_at DATETIME
);
CREATE TABLE term_version (
  id BIGINT PK, term_id BIGINT, version INT,
  snapshot JSON, changed_by BIGINT, change_note VARCHAR(255), created_at DATETIME
);

-- 共享维度与映射（conformed dimension，FR-05/FR-09 前提）
CREATE TABLE dimension (
  id BIGINT PK, dim_code VARCHAR(64) UNIQUE, standard_name VARCHAR(128),
  dimension_type ENUM('GENERAL','TIME','GEO') DEFAULT 'GENERAL',  -- 时间/地理维度一等公民（PRD 4.5）
  key_column VARCHAR(128),   -- 维度键（如 city_id），对应事实表外键
  source_dim_table VARCHAR(256),  -- 来源维度表（dim_city）
  domain VARCHAR(64), value_enum JSON NULL, owner BIGINT, status ENUM('ACTIVE','DEPRECATED')
);
-- 维度成员（PRD 4.5：维度具体取值/分组，如"华东区"={上海,江苏,浙江}，NL2SQL 上卷依赖）
CREATE TABLE dimension_member (
  id BIGINT PK, dim_id BIGINT, member_code VARCHAR(64), member_name VARCHAR(128),
  parent_member_code VARCHAR(64) NULL,   -- 层级父子链（country 根为 NULL，写入时环检测）
  member_group VARCHAR(64) NULL,         -- 虚拟分组（如"华东区"）
  source_value VARCHAR(128) NULL,        -- 源编码→标准值映射结果
  CONSTRAINT uk_member UNIQUE(dim_id, member_code)
);
CREATE TABLE dimension_mapping (
  id BIGINT PK, dim_id BIGINT, source_table VARCHAR(128), source_column VARCHAR(128),
  map_rule JSON,  -- 源编码→标准维度值（省→大区上卷、缩写→标准等）
  created_at DATETIME
);
CREATE TABLE metric_dimension (
  id BIGINT PK, metric_id BIGINT, dim_id BIGINT,
  CONSTRAINT uk_md UNIQUE(metric_id, dim_id)
);

-- 同源口径对账（FR-09 可信前提）
CREATE TABLE reconciliation (
  id BIGINT PK, fact_table VARCHAR(128), metric_ids JSON,  -- 同源多指标
  last_check DATETIME, diff_pct DECIMAL(5,2), status ENUM('OK','WARN','ALERT'),
  threshold DECIMAL(5,2) DEFAULT 1.00
);

-- 质量事件（FR-10）
CREATE TABLE quality_event (
  id BIGINT PK, metric_id BIGINT, level ENUM('P0','P1','P2'),
  rule_type VARCHAR(32), obs_value DECIMAL(18,4), threshold DECIMAL(18,4),
  status ENUM('OPEN','ACK','RESOLVED','CLOSED'), created_at DATETIME
);

-- 质量规则配置（FR-10：随指标 PUBLISHED 注册，按 tier/dw_layer 差异化，PRD 4.8.2）
CREATE TABLE quality_rule (
  id BIGINT PK, metric_id BIGINT, rule_type ENUM('COMPLETENESS','ACCURACY','TIMELINESS','CONSISTENCY','UNIQUENESS','VALIDITY','WAVE_DIFF','CROSS_SOURCE'),
  threshold JSON,           -- 静态阈值 / 动态基线(σ) / 同环比波动 参数
  rule_mode ENUM('static','dynamic_baseline','yoy_woy','cross_source'),
  severity ENUM('P0','P1','P2'), enabled BOOLEAN DEFAULT TRUE,
  notify_targets JSON,      -- Owner/关注者/domain_admin
  created_by BIGINT, created_at DATETIME
);

-- 指标结果快照存证（PRD 4.5：WORM，供争议仲裁/审计佐证/跨期对账）
CREATE TABLE metric_value_snapshot (
  id BIGINT PK, metric_code VARCHAR(64), version INT,
  dims JSON, date_range VARCHAR(64), value_json JSON,
  quality_flag VARCHAR(32), generated_at DATETIME, generated_by ENUM('QUERY','MATERIALIZE'),
  CONSTRAINT uk_snapshot UNIQUE(metric_code, version, dims, date_range)
);  -- 只写不删（WORM），保留期按合规（热存180d→冷归档）

-- 外部基准对账（PRD 4.8.8：导入权威值如银行对账单/审计数，自动比对）
CREATE TABLE external_benchmark (
  id BIGINT PK, source_id VARCHAR(64), metric_code VARCHAR(64),
  bench_date DATE, dims JSON, bench_value DECIMAL(18,4), provider VARCHAR(128),
  CONSTRAINT uk_bench UNIQUE(source_id, metric_code, bench_date, dims)
);
CREATE TABLE reconciliation_record (
  id BIGINT PK, benchmark_id BIGINT, metric_code VARCHAR(64),
  metric_value DECIMAL(18,4), bench_value DECIMAL(18,4), diff_pct DECIMAL(8,4),
  window VARCHAR(64), status ENUM('OK','WARN','ALERT','CONFIRMED'),
  owner_note VARCHAR(512) NULL, confirmed_by BIGINT NULL, checked_at DATETIME
);

-- 口径漂移巡检（PRD 4.7.8：注册口径 vs 源头 SQL 定期比对）
CREATE TABLE drift_scan_result (
  id BIGINT PK, metric_id BIGINT, scan_time DATETIME,
  actual_definition_json JSON,   -- 源头 SQL 当前实际口径（re-parse 结果）
  registered_version INT,        -- 平台注册版本
  similarity DECIMAL(5,4), drift_level ENUM('HIGH','LOW','NONE'),
  status ENUM('OPEN','CONFIRMED_INTENT','CONFIRMED_UNINTENT','IGNORED'),
  handler_id BIGINT NULL, handled_at DATETIME NULL, note VARCHAR(512) NULL
);

-- SLA 例外日历（PRD 4.5：节假日/大促/窗口期 SLA 放宽，防误报）
CREATE TABLE sla_calendar_exception (
  id BIGINT PK, exception_date DATE, exception_type ENUM('HOLIDAY','PROMO','MAINTENANCE'),
  sla_offset_minutes INT NULL,  -- 例外日 SLA 放宽量（NULL=跳过当天判定）
  domain VARCHAR(64) NULL,      -- 按域豁免（NULL=全局）
  maintainer BIGINT, created_at DATETIME,
  CONSTRAINT uk_sla UNIQUE(exception_date, exception_type, domain)
);

-- metric_code 重命名映射（PRD 3.5：全局映射表，永久生效，formula 解析时替换）
CREATE TABLE metric_code_alias (
  id BIGINT PK, old_code VARCHAR(64), new_code VARCHAR(64),
  renamed_at DATETIME, renamed_by BIGINT,
  CONSTRAINT uk_alias UNIQUE(old_code)
);

-- 分区重算事件（PRD 4.5 数据回刷场景：上游修数→下游感知）
CREATE TABLE partition_rewrite_event (
  id BIGINT PK, table_name VARCHAR(256), partition_value VARCHAR(64),
  rewrite_range VARCHAR(64), trigger_reason VARCHAR(255), triggered_by BIGINT,
  affected_metrics JSON, status ENUM('OPEN','DISPATCHED','DONE'), created_at DATETIME
);

-- 成本核算（PRD 4.10：按域/消费方聚合 LLM 与查询成本；R10-02 补字段结构）
CREATE TABLE ops_cost (
  id BIGINT PK, cost_date DATE, domain VARCHAR(64), consumer_id VARCHAR(64) NULL,
  category ENUM('LLM','QUERY','STORAGE','API_CALL'),    -- R10-02 补 API_CALL
  amount_usd DECIMAL(12,2), amount_cny DECIMAL(12,2) NULL,  -- R10-02 补人民币
  query_count INT DEFAULT 0, llm_token_in INT DEFAULT 0, llm_token_out INT DEFAULT 0,  -- R10-02 补用量
  scan_rows BIGINT DEFAULT 0, compute_seconds INT DEFAULT 0,  -- R10-02 补计算资源
  budget_monthly_usd DECIMAL(12,2) NULL,             -- 月度预算
  budget_alert_pct FLOAT DEFAULT 0.8,                -- 预算预警阈值(R11-17)
  budget_hard_limit_pct FLOAT DEFAULT 1.0,           -- 预算硬限阈值(R11-17)
  detail JSON, created_at DATETIME,
  INDEX idx_domain_date (domain, cost_date), INDEX idx_consumer (consumer_id)
);

-- 消费方凭证（FR-13 token 模型；R10-01/R11-15 补免费额度字段）
CREATE TABLE api_client (
  id BIGINT PK, client_id VARCHAR(64) UNIQUE, client_secret_ref VARCHAR(255),
  scope_domain VARCHAR(64), metric_whitelist JSON, qps INT DEFAULT 20,
  daily_quota INT DEFAULT 100000, scan_row_limit BIGINT,
  free_quota_monthly INT DEFAULT 10000,   -- API 免费额度（次/月，R10-01/R11-15）
  llm_free_quota_monthly INT DEFAULT 1000, -- LLM 免费额度（次/月/域，R11-15）
  budget_reset_at DATE NULL,              -- 预算/额度重置日期（R11-07/R11-09）
  status ENUM('ACTIVE','REVOKED'), created_at DATETIME
);

-- 分级分类结果（FR-11；敏感度枚举统一为 PUBLIC/INTERNAL/CONFIDENTIAL/PII，与 db_catalog 一致，PRD 4.9.3）
CREATE TABLE classification (
  id BIGINT PK, catalog_id BIGINT, sensitivity_level ENUM('PUBLIC','INTERNAL','CONFIDENTIAL','PII'),
  pii_columns JSON, classified_by VARCHAR(32), model_version VARCHAR(32),
  created_at DATETIME
);

-- LLM 模型配置与连通性测试（PRD 4.3a：platform_admin 可视化配置/探测/监控，衔接 4.13 降级）
CREATE TABLE llm_model_config (
  id BIGINT PK, service_name VARCHAR(64) UNIQUE, provider VARCHAR(32),
  adapter ENUM('openai_compatible','vendor_native'), base_url VARCHAR(255),
  api_key_ref VARCHAR(255),       -- Secret Manager 引用，不落明文
  models JSON, rpm_limit INT, enabled BOOLEAN DEFAULT TRUE,
  health_status ENUM('healthy','unhealthy','unknown') DEFAULT 'unknown'
);
CREATE TABLE llm_test_report (
  id BIGINT PK, config_id BIGINT, test_at DATETIME,
  endpoint_ok BOOLEAN, model_ok BOOLEAN, latency_ms INT, error_detail VARCHAR(512)
);
CREATE TABLE prompt_template (
  id BIGINT PK, template_key VARCHAR(64) UNIQUE,  -- 如 brief_parser/nl2sql/similarity
  content TEXT, model_alias VARCHAR(64),  -- 业务用语义别名，不硬编码厂商型号
  version INT, status ENUM('ACTIVE','DRAFT'), updated_at DATETIME
);
CREATE TABLE prompt_template_version (
  id BIGINT PK, template_id BIGINT, version INT, content TEXT, changed_by BIGINT, created_at DATETIME
);
CREATE TABLE golden_set (
  id BIGINT PK, set_version VARCHAR(32), sample_count INT,
  samples JSON, status ENUM('DRAFT','VALIDATED'), calibrated_at DATETIME
);   -- 小样本校准门禁（PRD 4.3 第8点，未达 0.85 不进全量）
CREATE TABLE calibration_result (
  id BIGINT PK, golden_set_id BIGINT, model_version VARCHAR(64), prompt_version VARCHAR(64),
  precision FLOAT, recall FLOAT, field_confidence_hist JSON, threshold_passed BOOLEAN, calibrated_at DATETIME
);

-- 指标树分层（PRD 4.5：一级/二级/三级导航，与主题域正交）
CREATE TABLE metric_tree (
  id BIGINT PK, metric_id BIGINT, l1 VARCHAR(64), l2 VARCHAR(64), l3 VARCHAR(128),
  CONSTRAINT uk_tree UNIQUE(metric_id)
);

-- 指标结果主动投递（PRD 4.5：定时/阈值触发推送指标值到钉钉/邮件/Webhook）
CREATE TABLE metric_delivery (
  id BIGINT PK, metric_code VARCHAR(64), schedule_cron VARCHAR(32) NULL,  -- 定时投递
  trigger_rule JSON NULL,          -- 阈值触发（突破即推送）
  channel ENUM('DINGTALK','EMAIL','WEBHOOK'), target VARCHAR(255),
  receiver_scope VARCHAR(64),      -- 接收方须有该指标可见权限（RBAC 对齐）
  enabled BOOLEAN DEFAULT TRUE, created_by BIGINT, created_at DATETIME
);

-- 依赖健康状态（PRD 4.13.6：每依赖独立健康探测，状态机 HEALTHY→DEGRADED→UNAVAILABLE）
CREATE TABLE dependency_health (
  id BIGINT PK, dependency_type ENUM('LLM','OLAP','GRAPH','ES','DATASOURCE','NOTIFICATION'),
  dependency_id VARCHAR(128),          -- 具体实例标识（如 llm_config_id / data_source_id）
  status ENUM('HEALTHY','DEGRADED','UNAVAILABLE') DEFAULT 'HEALTHY',
  last_check_at DATETIME,              -- 最近一次探测时间
  consecutive_failures INT DEFAULT 0,  -- 连续失败次数（达阈值→UNAVAILABLE）
  latency_p95_ms INT NULL,             -- 最近5分钟 P95 延迟
  error_rate_pct FLOAT DEFAULT 0,      -- 最近5分钟错误率
  circuit_state ENUM('CLOSED','OPEN','HALF_OPEN') DEFAULT 'CLOSED',  -- 熔断器状态
  circuit_opened_at DATETIME NULL,     -- 熔断开启时间
  metadata JSON,                       -- 扩展信息（如 LLM 可用模型列表、OLAP 活跃连接数）
  INDEX idx_dep_type (dependency_type), INDEX idx_dep_id (dependency_id)
);

-- 降级事件记录（PRD 4.13.6：降级开始/恢复事件入审计与看板）
CREATE TABLE degradation_event (
  id BIGINT PK, dependency_type ENUM('LLM','OLAP','GRAPH','ES','DATASOURCE','NOTIFICATION'),
  dependency_id VARCHAR(128),
  event_type ENUM('DEGRADED','UNAVAILABLE','RECOVERED','CIRCUIT_OPENED','CIRCUIT_HALF_OPEN','CIRCUIT_CLOSED'),
  severity ENUM('LIGHT','HEAVY'),      -- 轻降级(功能减退)/重降级(能力关停)
  affected_capabilities JSON,          -- 受影响的能力列表（如 ["ai_prefill","nl2sql"]）
  affected_user_count INT DEFAULT 0,   -- 预估受影响用户数
  started_at DATETIME,                 -- 降级开始时间
  recovered_at DATETIME NULL,          -- 恢复时间（NULL=仍在降级中）
  duration_seconds INT NULL,           -- 降级持续秒数（恢复后回填）
  trigger_reason VARCHAR(512),         -- 触发原因（如 "LLM 连续5次超时 > 30s"）
  resolution_action VARCHAR(512) NULL, -- 恢复动作（如 "自动探测恢复" / "人工重启"）
  INDEX idx_dep (dependency_type, dependency_id), INDEX idx_started (started_at)
);

-- ABAC 策略定义（PRD 4.9.1/4.9.8：RBAC×ABAC 融合，PDP 策略表达式）
CREATE TABLE policy (
  id BIGINT PK, policy_code VARCHAR(64) UNIQUE,
  name VARCHAR(128),
  role_id BIGINT,                       -- 关联 RBAC 角色（role 表 FK）
  resource_type ENUM('DOMAIN','METRIC','DIMENSION','EXPORT','ALL'),
  action ENUM('READ','WRITE','APPROVE','EXPORT','QUERY','ALL'),
  abac_condition JSON NULL,             -- ABAC 属性条件表达式（见下方 PDP 引擎规格）
  decision ENUM('ALLOW','DENY','ALLOW_WITH_MASK','ALLOW_SCOPED'),
  mask_strategy ENUM('MASK','HASH','GENERALIZE','NULLIFY') NULL,  -- 脱敏策略（ALLOW_WITH_MASK 时必填）
  scope_dimensions JSON NULL,           -- ALLOW_SCOPED 时限定可见维度值集
  priority INT DEFAULT 0,               -- 策略优先级（高优先先生效，冲突时按 priority + specificity 裁决）
  enabled BOOLEAN DEFAULT TRUE,
  created_by BIGINT, created_at DATETIME, updated_at DATETIME,
  INDEX idx_role_resource (role_id, resource_type)
);

-- 数据分级标签（PRD 4.9.3/4.9.8：四级分类 + PII 自动传播）
CREATE TABLE data_classification (
  id BIGINT PK,
  resource_type ENUM('TABLE','FIELD','METRIC','DIMENSION_VALUE'),
  resource_id BIGINT,                   -- 对应实体 ID
  sensitivity_level ENUM('PUBLIC','INTERNAL','CONFIDENTIAL','PII'),
  pii_columns JSON NULL,                -- PII 字段列表（仅 TABLE/FIELD 级）
  inherited_from BIGINT NULL,           -- 继承来源 ID（PII 沿血缘传播，NULL=直接标注非继承）
  classified_by VARCHAR(32),            -- 标注来源：regex / type_heuristic / llm / manual / inherited
  model_version VARCHAR(32) NULL,       -- LLM 分级模型版本
  verified_by BIGINT NULL,              -- compliance_officer 复核者 ID
  verified_at DATETIME NULL,            -- 复核时间
  created_at DATETIME, updated_at DATETIME,
  CONSTRAINT uk_dc UNIQUE(resource_type, resource_id),
  INDEX idx_sensitivity (sensitivity_level)
);

-- 合规复核记录（PRD 4.9.5/4.9.8：PII/CONFIDENTIAL 指标发布前合规官复核留痕）
CREATE TABLE compliance_review (
  id BIGINT PK,
  metric_id BIGINT NOT NULL,
  reviewer_id BIGINT NOT NULL,          -- compliance_officer ID
  review_type ENUM('PRE_PUBLISH','RESCAN','DOWNGRADE_REQUEST','PII_ANONYMIZATION'),
  decision ENUM('APPROVED','REJECTED','CONDITIONALLY_APPROVED'),
  conditions JSON NULL,                 -- 条件批准条件（如"脱敏后可降级为 INTERNAL"）
  reject_reason VARCHAR(512) NULL,
  classification_before ENUM('PUBLIC','INTERNAL','CONFIDENTIAL','PII') NULL,
  classification_after ENUM('PUBLIC','INTERNAL','CONFIDENTIAL','PII') NULL,
  pii_handling ENUM('NONE','MASK','HASH','GENERALIZE','NULLIFY') NULL,  -- PII 处理方式
  minimum_necessity_check BOOLEAN DEFAULT FALSE,  -- 最小可用校验是否通过
  reviewed_at DATETIME,
  INDEX idx_metric (metric_id), INDEX idx_reviewer (reviewer_id)
);

-- 用户订阅偏好（PRD 4.14.7：事件类型×渠道偏好矩阵）
CREATE TABLE subscription_pref (
  id BIGINT PK, user_id BIGINT NOT NULL,
  event_type VARCHAR(64) NOT NULL,      -- 事件类型（metric.published / quality.alert / conflict.arbitrated ...）
  channel ENUM('INAPP','EMAIL','WEBHOOK','SMS') NOT NULL,
  enabled BOOLEAN DEFAULT TRUE,
  quiet_hours_start TIME NULL,          -- 免打扰开始时间
  quiet_hours_end TIME NULL,            -- 免打扰结束时间
  digest_mode ENUM('REALTIME','HOURLY','DAILY') DEFAULT 'REALTIME',  -- 汇总模式
  webhook_url VARCHAR(512) NULL,        -- Webhook 回调地址
  CONSTRAINT uk_sub UNIQUE(user_id, event_type, channel)
);

-- 指标间业务关系（PRD 4.6/R3-13：LEADS/CAUSES/BELONGS_TO_PROCESS，与血缘 DERIVED_FROM 正交）
CREATE TABLE metric_business_relation (
  id BIGINT PK,
  source_metric_code VARCHAR(64) NOT NULL,
  target_metric_code VARCHAR(64) NOT NULL,
  relation_type ENUM('LEADS','CAUSES','BELONGS_TO_PROCESS') NOT NULL,
  lag_period VARCHAR(32) NULL,          -- LEADS: 滞后周期（如"1-2周"）
  causal_strength ENUM('HIGH','MEDIUM','LOW') NULL,  -- CAUSES: 因果强度
  process_name VARCHAR(128) NULL,       -- BELONGS_TO_PROCESS: 流程名
  step_order INT NULL,                  -- 流程内步骤序号
  declared_by BIGINT NOT NULL,          -- 声明者 ID
  source_type ENUM('MANUAL','LLM_SUGGESTED') DEFAULT 'MANUAL',  -- 人工声明 / LLM 候选建议
  confirmed_at DATETIME NULL,           -- LLM 建议待人确认时间
  created_at DATETIME,
  CONSTRAINT uk_mbr UNIQUE(source_metric_code, target_metric_code, relation_type),
  INDEX idx_source (source_metric_code), INDEX idx_target (target_metric_code)
);

-- 用户（E3：ER 图与服务引用均依赖，原 DDL 缺失）
CREATE TABLE user (
  id BIGINT PK, emp_id VARCHAR(64) UNIQUE,       -- 工号（HR 系统唯一标识）
  name VARCHAR(128), email VARCHAR(255),
  department VARCHAR(128), domain VARCHAR(64) NULL,  -- 归属域
  status ENUM('ACTIVE','OFFBOARDED') DEFAULT 'ACTIVE',
  offboarded_at DATETIME NULL,                   -- HR 离职时间（触发权限回收）
  created_at DATETIME, updated_at DATETIME,
  INDEX idx_emp (emp_id), INDEX idx_domain (domain)
);

-- 操作审计日志（E1：PRD 4.10/4.11/R11-01 统一消费管线审计，与 audit_log 互补）
-- audit_log = 治理操作审计（WORM）；operation_audit = 消费侧操作审计（API/QuickBI/MCP 调用留痕）
CREATE TABLE operation_audit (
  id BIGINT PK,
  trace_id VARCHAR(64),                          -- 全链路追踪 ID
  client_id VARCHAR(64) NULL,                    -- API 消费方 client_id（api_client FK）
  user_id BIGINT NULL,                           -- 操作用户 ID（UI 操作时）
  channel ENUM('api','quickbi','mcp','ui'),      -- 消费管线来源（R11-01）
  action VARCHAR(64),                            -- 操作：QUERY / EXPORT / METADATA / SCHEMA / EMBED
  resource_type VARCHAR(32),                     -- 资源：metric / metric_set / domain
  resource_id VARCHAR(128),                      -- 资源标识（metric_code / set_code / domain）
  request_params JSON NULL,                      -- 请求参数摘要（脱敏后）
  response_status INT,                           -- HTTP 状态码
  latency_ms INT NULL,                           -- 响应耗时
  llm_tokens_in INT DEFAULT 0,                   -- LLM token 消耗（NL2SQL/MCP 场景）
  llm_tokens_out INT DEFAULT 0,
  scan_rows BIGINT DEFAULT 0,                    -- OLAP 扫描行数
  ip VARCHAR(64), user_agent VARCHAR(512) NULL,
  created_at DATETIME,
  INDEX idx_client (client_id), INDEX idx_user (user_id),
  INDEX idx_channel_time (channel, created_at), INDEX idx_trace (trace_id)
);

-- Schema 变更检测事件（E2：PRD 4.4/R3-04 Schema Drift 检测，B6 PENDING_VERSION 联动）
CREATE TABLE schema_drift_event (
  id BIGINT PK,
  data_source_id BIGINT,                         -- 检测到的数据源 FK
  metric_version_id BIGINT NULL,                 -- 受影响的指标版本 FK
  table_name VARCHAR(256),                       -- 变更表名
  change_type ENUM('COLUMN_ADDED','COLUMN_REMOVED','COLUMN_TYPE_CHANGED','COLUMN_RENAMED','TABLE_RENAMED','TABLE_DROPPED'),
  before_schema JSON NULL,                       -- 变更前 schema 快照
  after_schema JSON NULL,                        -- 变更后 schema 快照
  diff_json JSON,                                -- 结构化差异 [{field, change_type, before, after}]
  severity ENUM('BREAKING','NON_BREAKING','INFO'),  -- 对指标的影响严重度
  affected_metrics JSON,                         -- 受影响指标列表 [{metric_code, version, impact}]
  status ENUM('DETECTED','PENDING_CONFIRMATION','CONFIRMED_INTENT','CONFIRMED_UNINTENT','ROLLBACK_TRIGGERED','IGNORED'],
  confirmed_by BIGINT NULL,                      -- 确认人 ID
  confirmed_at DATETIME NULL,
  detected_at DATETIME,
  INDEX idx_source (data_source_id), INDEX idx_metric_version (metric_version_id),
  INDEX idx_status (status), INDEX idx_detected (detected_at)
);

-- 血缘边主表（E4：PRD 4.4 指标级/字段级血缘主表，lineage_edge_history 为其变更快照）
CREATE TABLE lineage_edge (
  id BIGINT PK,
  edge_id VARCHAR(128) UNIQUE,                   -- Neo4j 边 ID 映射
  source_type ENUM('TABLE','FIELD','METRIC'),    -- 源节点类型
  source_id VARCHAR(256),                        -- 源节点标识（表名/字段全路径/指标码）
  target_type ENUM('TABLE','FIELD','METRIC'),    -- 目标节点类型
  target_id VARCHAR(256),                        -- 目标节点标识
  edge_type ENUM('LINEAGE_UP','LINEAGE_DOWN','DERIVED_FROM','DEFINED_BY','HAS','MAPPED_TO'),
  transform_expr TEXT NULL,                      -- 变换表达式
  confidence FLOAT DEFAULT 1.0,                  -- 置信度（1.0=Parser确认/人工，<1.0=LLM推断）
  source_enum ENUM('PARSER','MANUAL','LLM_SUGGESTED') DEFAULT 'PARSER',
  confirmed BOOLEAN DEFAULT TRUE,                -- 是否已确认（未确认边不参与影响面计算）
  active BOOLEAN DEFAULT TRUE,                   -- 软删除标记
  created_at DATETIME, updated_at DATETIME,
  CONSTRAINT uk_edge UNIQUE(source_type, source_id, target_type, target_id, edge_type),
  INDEX idx_source (source_type, source_id), INDEX idx_target (target_type, target_id)
);

-- 采集任务（E5：PRD 3.4b 采集调度，data_source 下周期性元数据采集任务）
CREATE TABLE collector_job (
  id BIGINT PK,
  data_source_id BIGINT NOT NULL,                -- 关联数据源 FK
  job_type ENUM('SCHEMA_SCAN','SAMPLE_FETCH','LINEAGE_PARSE','SENSITIVITY_SCAN'),  -- 采集类型
  schedule_cron VARCHAR(64),                     -- 调度周期（NULL=手动触发）
  last_run_at DATETIME NULL,                     -- 最近执行时间
  last_status ENUM('SUCCESS','PARTIAL','FAILED','RUNNING') NULL,
  last_duration_ms INT NULL,                     -- 执行耗时
  next_run_at DATETIME NULL,                     -- 下次计划执行时间
  enabled BOOLEAN DEFAULT TRUE,
  config_json JSON NULL,                         -- 采集参数（采样行数/并发度等）
  created_by BIGINT, created_at DATETIME, updated_at DATETIME,
  INDEX idx_source (data_source_id), INDEX idx_next_run (next_run_at)
);

-- 用户偏好（E22：PRD 前端设计引用——导航/域上下文/收藏/⌘K 搜索缓存）
CREATE TABLE user_preference (
  id BIGINT PK, user_id BIGINT NOT NULL,
  preference_key VARCHAR(64) NOT NULL,           -- 偏好键：default_domain / pinned_metrics / search_scope / theme / kbd_shortcuts
  preference_value JSON NOT NULL,                -- 偏好值（JSON，结构由 key 决定）
  updated_at DATETIME,
  CONSTRAINT uk_pref UNIQUE(user_id, preference_key),
  INDEX idx_user (user_id)
);

-- 反馈/NPS（E23：PRD OP-04 / POST /feedback，用户满意度与建议收集）
CREATE TABLE feedback (
  id BIGINT PK,
  user_id BIGINT NOT NULL,
  type ENUM('NPS','BUG','SUGGESTION','RATING'),  -- 反馈类型
  metric_code VARCHAR(64) NULL,                  -- 关联指标（NPS/评分场景）
  score INT NULL,                                -- NPS 分数 / 1-5 星评分
  content TEXT NULL,                             -- 文字反馈
  context JSON NULL,                             -- 上下文（页面/操作/trace_id）
  status ENUM('SUBMITTED','ACKNOWLEDGED','ADOPTED','REJECTED') DEFAULT 'SUBMITTED',
  handler_id BIGINT NULL,                        -- 处理人 ID
  handler_note VARCHAR(512) NULL,                -- 处理备注
  handled_at DATETIME NULL,
  created_at DATETIME,
  INDEX idx_user (user_id), INDEX idx_type_status (type, status), INDEX idx_metric (metric_code)
);
```

### 4.2 Neo4j 血缘图
```
(:Table {id, source_id, name, sensitivity, dw_layer})
(:Field {id, table_id, name, type, pii})
(:Metric {code, version, status, tier, domain})
(:Dimension {code, standard_name})       -- 维度节点（PRD 4.4 USES_DIMENSION）
(:Report {id, name})   -- 消费方（QuickBI/API）
(:APIClient {id, client_id, status})   -- API 消费方（R11-06 ops_cost 归属）
(:OpsCost {id, cost_date, category, amount})  -- 成本节点（R11-06）
(:Dependency {id, type, status})       -- 依赖健康节点（PRD 4.13.6）
(:DegradationEvent {id, type, severity, started_at})  -- 降级事件（PRD 4.13.6）
(:Policy {id, code, decision})         -- ABAC 策略节点（PRD 4.9.8）
(:DataClassification {id, level})      -- 分级标签节点（PRD 4.9.8）
关系：
  (Field)-[:LINEAGE_UP]->(Field)          -- 字段级血缘（L2）
  (Field)-[:LINEAGE_DOWN]->(Field)
  (Table)-[:HAS]->(Field)
  (Table)-[:LINEAGE_UP]->(Table)          -- 表级血缘（L1）
  (Metric)-[:DEFINED_BY]->(Field|Table)   -- 指标定义来源
  (Metric)-[:DERIVED_FROM]->(Metric)      -- 指标级依赖边（派生/复合→被依赖指标，PRD 4.4 关键）
  (Metric)-[:USES_DIMENSION]->(Dimension) -- 指标依赖维度（含时间维度，维度漂移→指标重审，PRD 4.4）
  (Dimension)-[:PARENT]->(Dimension)      -- 维度层级（上滚/下钻）
  (Report)-[:CONSUMED_BY]->(:Metric)      -- 运行时反填（消费方下游）
  (Field)-[:MAPPED_TO]->(:Dimension)      -- 源字段→标准维度映射（conformed dimension）
  (Metric)-[:COST_OF]->(:OpsCost)         -- 指标级成本归属（R11-06）
  (APIClient)-[:COST_OF]->(:OpsCost)      -- 消费方成本归属（R11-06）
  (:Role)-[:COST_OF]->(:OpsCost)          -- 域级成本归属（R11-06）
  (Metric)-[:LEADS]->(:Metric)            -- 先行指标关系（R3-13）
  (Metric)-[:CAUSES]->(:Metric)           -- 因果关系（R3-13）
  (Metric)-[:BELONGS_TO_PROCESS]->(:Process) -- 同流程关系（R3-13）
  (Metric)-[:INHERITS_CLASSIFICATION]->(:Table|:Field) -- PII 沿血缘自动传播（PRD 4.15.2）
  (Metric)-[:REQUIRES_COMPLIANCE]->(:Policy) -- PII 指标关联合规策略（PRD 4.9.8）
  (Dependency)-[:AFFECTS]->(:Metric|:Capability) -- 依赖降级影响（PRD 4.13.6）
  (DegradationEvent)-[:TRIGGERS]->(:Notification) -- 降级事件触发通知
  (:Notification {id, type, status, created_at})   -- 通知节点（PRD 4.14.7，状态机 PENDING→SENT→READ/ESCALATED→DONE）
  (:Event {id, type, payload})                     -- 事件节点（PRD 4.14.7）
  (:SubscriptionPref {user_id, event_type, channel}) -- 订阅偏好节点（PRD 4.14.7）
  (:Notification)-[:NOTIFIES]->(:User)             -- 通知→用户（PRD 4.14.7）
  (:User)-[:SUBSCRIBES]->(:Metric|:Event)          -- 用户订阅（PRD 4.14.7）
  (:Notification)-[:ESCALATES_TO]->(:User)         -- 升级通知（PRD 4.14.7）
```
**影响面查询（表级 + 指标级双通道，PRD 4.4 关键）**：
- 表/字段影响面：`MATCH (f:Field {id:$id})<-[:LINEAGE_UP*..5]-(up) RETURN up` + 反向 `LINEAGE_DOWN*`
- **指标影响面（改原子指标口径 → 下游指标）**：`MATCH (m:Metric {code:$code})<-[:DERIVED_FROM*]-(down:Metric) RETURN down`（递归展开，含跨域传递；维度漂移经 `USES_DIMENSION` 反向边 `<-[:USES_DIMENSION]-(metric)` 同样触发）
- **循环依赖检测**：`DERIVED_FROM` 上做环检测，成环在注册/更新时拦截（F3/F16）
> 准确性门禁：进入影响面/废弃阻断/版本级联等关键链路的边**仅限已确认边**（Parser 确认 + 人工补全 + LLM 推断经人工确认）；实验性/待确认边显式排除（PRD 4.4 质量闭环）。

### 4.3 ES 索引
```
metric_idx：{metric_code, name(ik_max_word), domain, status, tags, definition_text}  -- 全文+中文分词
term_idx：{term, alias, definition}  -- 术语检索（FR-08）
```

---

## 5. 容错设计

### 5.1 双写最终一致
指标注册/变更成功：MySQL 写 → 发 Redis 事件 → 异步写 Neo4j + ES；任一写失败入重试队列（指数退避，最多 N 次，超限告警人工介入）。读取侧对"图中缺失"该资产标 `stale`，不整页崩。

### 5.2 降级矩阵（呼应 FR-17 / 4.13）
| 能力故障 | 降级行为 | 用户态 |
|---------|---------|--------|
| LLM 解析不可用 | 取消 AI 预填，指标注册走纯人工 | 提示"AI 暂不可用，手动填写" |
| Neo4j 不可用 | 血缘图标 `stale`，列表/详情仍可用 | 血缘区灰显+提示 |
| ES 不可用 | 搜索退化为 MySQL `LIKE`（限域） | 搜索变慢但可用 |
| OLAP 不可达 | 查询返 `503` + `retry_after`，不返回缓存错数（除非 `accept_stale`） | 明确错误，不静默降可信度 |
| 推荐服务不可用 | 首页退化为"热门口径"榜单 | 不空窗 |

**降级中间件**：统一 `DegradationMiddleware` 拦截各能力异常，转 `degraded=true` + 友好文案，相邻能力不受影响（舱壁隔离）。

### 5.2a 依赖健康状态机与 Circuit Breaker（PRD 4.13.1/4.13.2）

**依赖健康状态机（每个依赖独立实例）**

```
HEALTHY ──超时/错误率超阈──▶ DEGRADED ──连续失败达阈值──▶ UNAVAILABLE
   ▲                            │                              │
   │        自动恢复探测通过      │         冷却期后探测恢复       │
   └────────────────────────────┘──────────────────────────────┘
```

| 状态 | 进入条件 | 行为 | 退出条件 |
|------|----------|------|----------|
| `HEALTHY` | 初始 / 探测恢复 | 正常服务 | — |
| `DEGRADED` | 单次超时 > 2×阈值 **或** 5min 错误率 > 5% | 轻降级：能力减弱但可用（如 LLM 不可用→AI 预填充禁用，人工通道正常） | 连续 3 次探测成功 → `HEALTHY` |
| `UNAVAILABLE` | 连续 N 次失败（N 由依赖类型决定，见下表）**或** Circuit Breaker `OPEN` | 重降级：返回明确错误（503 + `error_code=DEPENDENCY_DEGRADED_*`）而非超时 | Circuit Breaker `HALF_OPEN` 探测成功 → `DEGRADED` → 再 3 次成功 → `HEALTHY` |

**各依赖类型阈值参数（对齐附录 B）**：

| 依赖 | 超时阈值 | 错误率阈值 | 连续失败阈值(N) | 熔断冷却期 | 半开探测数 |
|------|----------|-----------|----------------|-----------|-----------|
| LLM | 30s | 5% | 5 | 60s | 1 |
| OLAP | 10s | 5% | 3 | 30s | 1 |
| Neo4j | 3s | 5% | 5 | 30s | 1 |
| ES | 2s | 10% | 5 | 30s | 1 |
| DataSource | 连接超时5s | 10% | 3 | 60s | 1 |
| Notification | 投递超时5s | 10% | 5 | 120s | 1 |

**Circuit Breaker 三态（PRD 4.13.2，防雪崩）**

```
CLOSED ──错误率超阈──▶ OPEN ──冷却期满──▶ HALF_OPEN ──探测成功──▶ CLOSED
                                          │  探测失败
                                          └──▶ OPEN（重置冷却计时）
```

| 熔断状态 | 语义 | 行为 |
|----------|------|------|
| `CLOSED` | 正常 | 请求正常通过，统计滑动窗口内错误率 |
| `OPEN` | 熔断 | 请求直接快速失败（返回 503 + `retry_after`），**不等待超时**，避免积压击穿 |
| `HALF_OPEN` | 半开探测 | 放行 1 个探测请求；成功 → 关闭熔断(`CLOSED`)；失败 → 重新开启(`OPEN`，重置冷却计时） |

**实现要点**：
- **滑动窗口统计**：每依赖独立 Redis 滑动窗口 key `cb:{dep_type}:{dep_id}:errors` / `cb:{dep_type}:{dep_id}:total`，窗口 60s
- **状态存储**：Redis key `cb:{dep_type}:{dep_id}:state` = `CLOSED|OPEN|HALF_OPEN`，`cb:{dep_type}:{dep_id}:opened_at` = timestamp
- **退避策略**：调用方收到 503 后指数退避 + 抖动（避免惊群），公式 `delay = base_delay * 2^attempt + random(0, base_delay)`，`base_delay=1s`，最大重试次数 3 次，超限入死信队列
- **429 vs 503 分场景**：429=调用方速率超限（可重试） / 503=依赖降级/熔断（须等待恢复）
- **降级度量**：`degradation_event` 记录每次状态变更 + 持续时长 + 影响用户数 → 入 4.10 运营看板（度量降级频次/时长/影响用户）

**舱壁与 6.4 服务划分映射（PRD 4.13.7/R8-09）**：
- LLM 池 = 独立 Worker 实例组（消费 Redis 队列 LLM 通道），与物化 Worker 隔离
- 计算池 = API 内 DataSource 连接池（受 `data_source.quota` 约束，每 DataSource 独立配额）
- 图库池 = 图库同步器实例 + Neo4j 只读连接池（查询走只读副本，同步器走 Leader 写入）
- 三池资源互不争抢，单池打满不影响其他池可用性

### 5.3 资源隔离（防 AI 打挂 OLAP，呼应 4.13.6）
- 查询路由：AI/DataAgent 流量 → 独立 OLAP 实例/队列；人工流量 → 主实例。
- 限流：消费方 `QPS=20`、`日调用=10万`、`单查询扫描行数上限`；超限 `429` + `retry_after`。
- 超大扫描：`scan_rows > quota` 直接 `422`，不裸跑。

### 5.4 业务错误码（统一语义表，C2；对齐 PRD 附录 A.2）
所有服务返回体 `{error_code, message, retry_after, trace_id}` 中的 `error_code` 取自下表，跨服务一致，禁止自定义数字码。**消费方按 `error_code`（非 HTTP 状态码）路由处理逻辑**——同一 HTTP 状态码可对应多个 `error_code`（如 410 同时用于 API 版本废弃、口径版本过期、指标退役、数据源断连，四者语义不同处理方式不同，R13-11/A.1）。

```
────────── 鉴权与权限 ──────────
401  AUTH_TOKEN_MISSING        请求未携带 Bearer Token
401  AUTH_TOKEN_EXPIRED        Token 已过期（用 refresh_token/client_secret 重新签发）
401  AUTH_TOKEN_INVALID        Token 签名/格式错误
403  FORBIDDEN                 越权（域/白名单不匹配，必入审计）
403  FORBIDDEN_DOMAIN          无权访问目标域（申请 domain_grant）
403  FORBIDDEN_METRIC          无权访问目标指标（申请指标权限）
403  FORBIDDEN_DIMENSION       无权访问敏感维度（申请维度权限或接受脱敏）
403  FORBIDDEN_PII             未获 PIPL 单独授权访问 PII 指标（走合规门禁）
403  FORBIDDEN_DEPRECATED      访问已废弃指标且未带 accept_deprecated=true
     └─ R13-11: 与 METRIC_DEPRECATED(410) 区分——403=权限策略拒绝/Sunset期内加头仍可用；410=彻底退役GONE
403  INJECTION_DETECTED        检测到提示词注入攻击（NL2SQL）

────────── 请求校验 ──────────
404  METRIC_NOT_FOUND          指标码不存在（检查 metric_code 拼写）
404  NOT_FOUND                 其他资源不存在（source/term 等）
409  CONFLICT                  同名待仲裁 / 术语冲突 / 口径版本冲突
409  VERSION_CONFLICT          消费方绑定的 metric_version 已不是当前生效版本（乐观锁CAS，R9-01）
409  COMPLIANCE_BLOCKED        PII 指标 approve 被合规门禁拒绝，回 REVIEW 记拒因
409  DRIFT_DETECTED            口径漂移高等级检出，已进 Owner 待办
409  RECONCILIATION_ALERT      外部基准对账差异超阈值
422  EXPRESSION_INVALID        指标表达式语法/类型错误（MEL 语法）
422  GRANULARITY_VIOLATION     下钻粒度细于指标 granularity 硬约束（4.5，PRD名 GRANULARITY_VIOLATION）
422  ADDITIVITY_VIOLATION      上卷违反指标可加性（须走重算）
422  DIMENSION_INVALID         请求维度不在指标合法维度集
422  DATE_RANGE_INVALID        时间范围不合法
422  FILTER_INVALID            过滤条件引用不存在的维度值
422  QUOTA_EXCEEDED_SCAN       单查询扫描行数超 data_source.quota（不裸跑）
422  QUOTA_EXCEEDED_EXPORT     导出行数超限
422  BATCH_QUOTA_EXCEEDED      批量操作超上限（20个/批次）
207  BATCH_PARTIAL_FAILURE     批量操作部分成功部分失败（检查响应体每条结果）

────────── 限流与降级 ──────────
429  RATE_LIMITED              调用速率超 QPS/RPM/TPM 配额（带 retry_after）
429  FREE_QUOTA_EXCEEDED       月度免费额度已用尽（响应含 budget_reset_at）
429  BUDGET_EXCEEDED           月度预算已超 100% 自动限流（响应含 budget_reset_at）
503  DEPENDENCY_DEGRADED_LLM   LLM 服务降级/熔断（AI辅助不可用，退回手动）
503  DEPENDENCY_DEGRADED_ENGINE 计算引擎降级/熔断（按 retry_after 重试或 accept_stale）
503  DEPENDENCY_DEGRADED_GRAPH 图库降级/熔断（血缘查询降级为表级概览）
503  DATA_SOURCE_UNAVAILABLE   绑定 DataSource 不可达（可选 accept_stale）
503  SERVICE_OVERLOADED        平台整体过载（按 retry_after 退避，避免重试风暴）

────────── 质量与新鲜度（非错误，HTTP 200 但 meta 标注） ──────────
200  QUALITY_DOWNGRADED        质量分低于阈值，结果标注 quality_downgraded（审慎使用）
200  STALE_DATA                返回陈旧缓存，标注 stale=true（仅显式 accept_stale=true 时返回）
200  FRESHNESS_DELAYED         SLA 级联延迟，数据非最新（检查 meta.freshness）

────────── 版本与迁移 ──────────
410  METRIC_DEPRECATED         指标已彻底退役（GONE，须使用 successor）
410  METRIC_SOURCE_DROPPED     指标数据源已断连（源表 DROP/不可达，R5-01）
410  METRIC_VERSION_SUNSET     指标口径钉住版本已过 Sunset 期（默认30天）
410  API_VERSION_SUNSET        API 版本已废弃（迁移至新版本文档指引）

────────── LLM 与 AI（四期） ──────────
422  NL_INTENT_UNRECOGNIZED    NL2SQL 无法识别意图（重新表述或使用结构化 API）
422  NL_AMBIGUOUS              自然语言歧义（回应澄清反问）
422  NL_MAPPING_FAILED         无法将自然语言映射到已注册指标

────────── 系统级 ──────────
500  INTERNAL_ERROR            未预期内部错误（附 trace_id 联系运维）
504  QUERY_TIMEOUT             下推查询超时（用 query_id 取消在途查询后重试）
```
>  degraded=true 仅出现在降级态（ES→LIKE、Neo4j→stale、推荐→榜单），不出现在 `403/409/422/429` 等错误态。消费方据 code 决定重试/retry_after/人工介入。

### 5.5 新增降级：分级分类引擎
| 能力故障 | 降级行为 | 用户态 |
|---------|---------|--------|
| 分级分类模型/引擎不可用 | `sensitivity_level` 暂标 `UNKNOWN`，资产地图热力退化为"未分级"灰区，注册/发布不阻断（仅提示），事后补扫（COMPL-2 重扫触发补全） | 提示"敏感度待定，已限制 PII 导出" |

---

## 6. 性能设计

| 场景 | 目标 | 手段 |
|------|------|------|
| 指标检索（ES） | P95 < 200ms | ES 索引 + 中文分词；列表分页游标 |
| 指标详情加载 | P95 < 500ms（R12-14） | MySQL 主键查 + ES 补充 + 缓存 |
| 血缘影响面（Neo4j） | 千节点 P95 < 500ms | 图索引 + 深度限制（默认 ≤5 跳） |
| 血缘图 L1→L2 展开渲染 | P95 < 3s（R12-14） | 按需加载字段级，超时降级表级概览 |
| 语义查询（下推） | P95 < 3s（小表）/ < 10s（大跨期） | 分区裁剪 + 预聚合命中 + 查询计划路由 |
| 语义查询（缓存命中） | P95 < 1s | Redis `metric_version+params` 哈希，TTL 随新鲜度 |
| LLM 解析单任务 | P95 < 30s（R12-14） | 批量合并 + SQL 指纹缓存复用 |
| 物化任务产出延迟 | P95 ≤ 2× 源 ETL 延迟（上限 1h，R12-14） | 分区就绪触发 + 优先级调度 |
| 查询缓存 | 命中 P95 < 50ms | Redis `metric_version+params` 哈希，TTL 随新鲜度 |
| 采集增量 | 单源 < 5min | 监听 DDL/新增表，增量而非全量 |
| 限流计数 | < 1ms | Redis `INCR` + 滑动窗口 |
| 批量注册 | 单批 20 指标 < 10s | 异步 LLM 解析队列 + 进度轮询 |
| 审计写入 | P99 < 100ms | 异步入库 + 批量 INSERT |
| PDP 决策（缓存命中） | P99 < 5ms | Redis 缓存 60s TTL |
| PDP 决策（缓存未命中） | P99 < 50ms | MySQL policy 表查询 + ABAC 条件求值 |
| 通知投递（站内） | P99 < 500ms | Redis Stream → 写 MySQL → 前端轮询/WS |
| 双写异步同步延迟 | P99 < 5s | Redis Stream → Worker 消费 → Neo4j/ES |

**预聚合**：高频查询经语义层物化为聚合表（4.5），API 优先命中，降 MPP 压力（命中率入运营看板）。二期引入 Cube 后，预聚合交由 Cube 的 pre-aggregations 托管（物化到 OLAP/Redis），平台不再自研物化调度，仅负责口径同步与命中率观测。
**容量基线**：一期按"核心指标数千、日查询数十万"设计；二/三/四期随埋点复核（见 PRD 7.1/OP-09）。

---

## 7. 前端设计（美观标准 + 布局样式）

### 7.1 设计语言（与 PRD 5.x 对齐）
- **风格**：企业级数据治理大屏，专业、克制、信息密度高但不拥挤；参考 Ant Design Pro + DataHub 资产地图观感。
- **色板（token）**：主色 `#2F54EB`（信任蓝），语义色 成功 `#52C41A` / 警告 `#FAAD14` / 错误 `#FF4D4F` / PII 红 `#CF1322`；中性灰阶 9 级。
- **圆角**：卡片 8px、按钮 6px；**间距**：8pt 栅格（8/16/24/32）。
- **字体**：系统无衬线；数字用等宽 `font-variant-numeric: tabular-nums`（口径/指标值对齐）。
- **暗色模式**：一期不做（大屏办公场景亮色优先）。

### 7.2 布局（三栏，呼应 PRD 5.2.6）
```
┌─────────┬──────────────────────────────┬──────────┐
│ 左导航   │  主内容（列表/详情/画布）        │ 右上下文   │
│ (≥1280固定│  顶栏：搜索框+全局操作          │ (溯源/血缘 │
│  中屏抽屉)│  面包屑 + 状态标签             │  /属性)   │
└─────────┴──────────────────────────────┴──────────┘
```
- 宽屏 ≥1280 三栏完整；中屏 768–1279 左导航收为抽屉。
- **指标详情页布局**：左=口径版本/定义；中=结果/血缘画布；右=溯源 meta + 消费方 + 活动（OP-07）。

### 7.3 三态反馈规范（PRD 5.4 核心）
- **加载态**：骨架屏 + 操作按钮 loading 禁用，不白屏。
- **成功态**：轻提示（toast 3s）+ 关键操作结果摘要（如"指标 gmv v3 已发布"）。
- **错误/降级态**：错误码→人话文案（如 `503 DEPENDENCY_DEGRADED_ENGINE` → "计算引擎暂不可用，预计 X 恢复，可稍后重试或联系管理员"）；降级能力灰显 + 角标"降级中"。

### 7.4 溯源优先（PRD 5.4 强调）
- 任何指标值/口径展示必带 `metric_version` 角标，点击展开版本历史与变更人。
- 血缘图节点可点下钻到字段级 + Owner + 敏感标签。
- 查询结果 `meta` 条常驻（新鲜度/粒度/来源/质量）。

### 7.5 美观硬性标准（验收用）
1. 所有表单有校验态（错误红框 + 文案），无裸报错。
2. 空状态有插画/引导（如"待认领资产"引导认领）。
3. 列表支持排序/筛选/分页，千行不卡（虚拟滚动）。
4. 响应式断点无横向滚动、无元素重叠。
5. 颜色对比度达 WCAG AA；PII 标记醒目且不误导。
6. 操作有确认（删/废弃/权限变更，呼应 OP-01/05）。

### 7.6 核心页面清单与交互映射（对齐 PRD 5.3/5.4）

**页面清单（一期 MVP/完整须交付，各页路由与数据源如下）**：

| 页面 | 路由 | 数据来源 | 交付期 |
|------|------|----------|--------|
| 指标目录（列表/搜索） | `/metrics` | consume `GET /metrics`（ES）+ 筛选 | MVP |
| 指标详情 | `/metrics/:code` | consume `GET /metrics/{code}` + lineage | MVP |
| 指标注册向导 | `/metrics/new` | semantic（LLM 预填 + 试算 + 冲突预检） | MVP |
| 审核工作台 | `/review` | semantic `GET /metrics?status=REVIEW` + conflict | MVP |
| 待办中心 | `/todos` | notify `GET /notifications` | MVP |
| 血缘视图 | `/lineage/:id` | lineage `GET /lineage/*`（G6 + 降级表级） | MVP |
| 资产地图/热力 | `/assets` | assetmap `GET /asset-map/*` | 一期完整 |
| 治理驾驶舱 | `/governance` | semantic `GET /metrics/dashboard` + observability | 一期完整 |
| 术语库 | `/glossary` | glossary `GET /terms` | 一期完整 |
| 我的收藏/最近浏览 | `/favorites` | consume user_preference | MVP |
| 指标对比 | `/compare` | semantic `POST /metrics/compare` | 一期完整 |
| 帮助中心 | `/help` | 静态 + glossary 概念卡 | 一期完整 |

**PRD 5.4 交互规格 → 组件映射（逐条落地）**：

| PRD 5.4 交互规格 | 前端实现（组件/状态） |
|------------------|---------------------|
| 5.4.2 搜索与找数 | 顶栏搜索框（防抖 300ms）→ `/metrics?q=`；结果卡含状态徽标/健康度/口径摘要；空结果空态引导 |
| 5.4.3 指标详情与可读口径 | 详情页三栏；"口径速查"折叠卡（先给结论，展开见 formula）；`metric_version` 角标点击弹版本时间线（US-DA-2） |
| 5.4.4 注册引导（ETL/模板） | 注册向导分 5 步（§12.3）；LLM 预填字段带"AI 推断"徽标可覆盖；试算按钮 → dry-run |
| 5.4.5 试算与口径校验 | 详情/注册页"试算"→ `POST /query/dry-run`，并排"试算结果 vs 预期值"，偏差>5% 高亮 |
| 5.4.6 审核与灰度发布 | 审核卡含"通过→PUBLISHED / 通过→EXPERIMENTAL(灰度)"双按钮 + 驳回理由必填；灰度指标蓝徽标"实验中" |
| 5.4.7 权限申请与续期 | 无权限页"申请权限"→ 表单（域/指标/用途）→ `POST /grants`；到期前待办"一键续期"（OP-02） |
| 5.4.8 冲突协商与仲裁 | 协商面板（双方面板并排 diff + 时间线）；升级/裁决按钮（GOV-1/2） |
| 5.4.9 待办与通知联动 | 待办卡含 SLA 倒计时（剩 Xh 升级）；铃铛角标与待办中心同源 |
| 5.4.10 降级与错误恢复 | 统一 ErrorBoundary + 错误码→人话文案映射表（§5.4）；降级能力灰显 + "降级中"角标 |

---

## 8. 边界能力（明确"不做什么"，呼应 PRD 1.5/4.13/4.15）

| 边界 | 说明 |
|------|------|
| 不重造 LLM | 仅调用现有模型服务网关，不训不托管；支持国内主流厂商（智谱/通义千问/文心/讯飞/DeepSeek/Kimi）官方 API 与任意 OpenAI 兼容协议端点（vLLM/Ollama/OneAPI/私有化网关），不绑定单一供应商 |
| LLM 多供应商一致性 | 平台仅依赖统一 `LLMClient` 语义接口与模型别名，业务代码不感知厂商；切换供应商改 `settings.yaml`，不触发业务改造 |
| 不替代 BI | 探索拖拽/看板排版/订阅由 QuickBI 承担，平台只供可信口径+计算 |
| 不持有源数据 | 只下推查询，不落地源表；源库不可达降级不返缓存错数（除非 `accept_stale`） |
| 不越源端权限 | API 权限是消费层管控，源库自身权限仍须配 |
| 方言有限 | 新增 DataSource 类型需补适配器；存储过程暂不支持 |
| 联邦查询（一期） | 跨源 JOIN 一期不支持 |
| 推荐不基于 PII | 个性化推荐不用人字段（呼应 4.15） |
| Cube 仅作执行/缓存层 | Cube 是语义层+预聚合+缓存的加速组件，不替代平台口径状态机/版本溯源/冲突仲裁/PII 门禁/血缘；业务方不直接改 Cube YAML 发布口径（须经平台审核流） |
| 移动端 | 不提供手机/小程序形态 |

---

## 9. 验收标准（对接 PRD 第 9 章 DoD）

### 9.1 一期 MVP（W1–W7）
- [ ] 数据源只读连接保存即触发全量采集，资产清点看板显示覆盖率。
- [ ] L1/L2 血缘可查、影响面计算正确（深度≤5 跳 P95<500ms）。
- [ ] 指标注册→审核→发布状态机闭环，PII 指标触发合规门禁前无法 PUBLISHED；门禁拒绝时状态正确回退 REVIEW 且记拒因。
- [ ] **灰度与版本确认**：审核通过可选 EXPERIMENTAL 灰度（白名单可见 + meta 标 experimental）；破坏性变更生成 PENDING_VERSION 且默认查询仍命中旧版本；消费方 confirm/reject/超时(14d 默认接受) 三条路径均正确升/驳版本；灰度 promote/rollback 一键可达（PRD 5.5.1）。
- [ ] 语义查询下推 OLAP 返回带 `meta` 的可信结果，P95<3s（小表）；口径 `metric_version` 变更后旧缓存键主动失效（不返旧数）。
- [ ] 四类冲突（同名/同义不同名/粒度单位/跨域异源 + 口径版本 + PII 路由）检测触发正确：硬冲突阻断发布、软冲突仅提示、PII 冲突转交 governance（403 FORBIDDEN_PII）。
- [ ] Semantic API token 模型（api_client 换短效 JWT + scope/白名单）+ 限流（QPS/日配额/扫描行）+ 降级可用，越权返 403 并审计。
- [ ] 双写最终一致：MySQL/Neo4j/ES 三方数据最终对齐（重试队列验证）；服务间事件总线至少一次投递 + 消费幂等（同事件不重复建冲突/待办）。

### 9.2 一期完整（W8–W10）
- [ ] LLM 辅助解析出指标候选草稿（不自动发布）；collector 以 `glossary_term` 标准词作 few-shot 约束。
- [ ] **口径漂移巡检**：Tier-1 指标源头 SQL 变更后 24h 内检出高漂移并告警；Owner 处置（有意→变更评审 / 无意→通知改SQL）闭环率 ≥ 90%（PRD 4.7.8）。
- [ ] **外部基准对账**：基准导入幂等可重跑；对账差异检出→确认闭环率 ≥ 90%；对账报告可导出（受权限，PRD 4.8.8）。
- [ ] **SLA 例外日历**：配置节假日/大促例外日后，对应指标不触发"数据延迟"误报；补跑期豁免生效（PRD 4.5）。
- [ ] **结果快照存证**：Tier-1 指标查询/物化结果落 `metric_value_snapshot`（WORM），`GET /snapshots` 可回溯历史值且受权限/脱敏（PRD 4.5）。
- [ ] 术语库检索+引用可用（指标引用 `term_id` 不拷贝）；维度映射（共享维度跨域对齐）+ 同源口径对账定时跑出 WARN/ALERT；质量异常分级告警；QuickBI 嵌入出图。
- [ ] 审计全量留痕；运营看板（DAU/审核时效）可看；降级 UX 全能力覆盖（含分级引擎降级 §5.5）。
- [ ] 模板包/帮助中心/治理驾驶舱可用；合规门禁全量（行级骨架）。

### 9.3 北极星验收（呼应 PRD 1.4 + 冷启动基线）
- [ ] 元数据覆盖率从 <40%（基线）提升至 ≥95%。
- [ ] 核心指标 PUBLISHED 率从 ≈20%（基线）提升至 ≥80%。
- [ ] 分析师确认口径平均耗时从"数天"降至"分钟级"（平台内可测）。
- [ ] 月度跨域同名不同义冲突从 ≈30 起降至趋零（冲突闭环收敛）。

### 9.4 非功能验收
- [ ] 单能力故障（LLM/Neo4j/ES/OLAP）不阻断相邻能力（降级矩阵验证）。
- [ ] 消费方 `429` 带 `retry_after`，AI 流量隔离不拖垮 OLAP。
- [ ] 所有 PII 访问/推导入审计；合规官复核留痕可追溯。
- [ ] 前端三态反馈、溯源优先、响应式断点无重叠（§7.5 六条硬性标准逐条过）。
- [ ] LLM 适配层：至少 2 家国内厂商官方 API（如 DeepSeek + 通义千问）+ 1 个 OpenAI 兼容端点（vLLM/Ollama/OneAPI 任一）接入成功；`model_alias` 切换供应商后业务零改。
- [ ] 单供应商故障触发 failover 至备用供应商；全不可用时返 `503 AI_UNAVAILABLE` 并走三态降级，非 AI 主链路（注册/查询/血缘）不受影响。

---

## 10. 技术风险与对策（呼应 PRD 10.x）

| 风险 | 对策 |
|------|------|
| 双写不一致 | 重试队列 + 定期对账任务（MySQL 为源，补偿 Neo4j/ES） |
| Cube 口径同步失败（二期） | PUBLISHED→Cube 定义生成走重试队列；同步未完成时查询路由回退自研执行引擎，不影响消费 |
| LLM 幻觉致错误口径 | 结果仅草稿、人确认、不自动发布；语义锚定降幻觉（4.12.1） |
| 多供应商响应差异 | 同 prompt 跨厂商输出不一：业务侧用 model_alias 而非硬编码型号；关键链路（NL2SQL）固定首选模型并做输出 schema 校验；切换供应商前跑回归样例集比对 |
| 单一供应商限流/故障 | 按供应商独立限流+熔断+failover 备用供应商；全不可用时降级 `503 AI_UNAVAILABLE`，不阻断非 AI 主链路 |
| OLAP 被打挂 | 舱壁隔离 + 限流 + 查询计划路由 + 预聚合 |
| 采集对生产库压力 | 只读 + 增量 + 低峰调度 + 速率限制 |
| PII 泄露 | 分级门禁 + 脱敏导出 + 审计全留痕 + 不基于 PII 推荐 |
| 血缘准确性 | 三期补准确性门禁（深化 FR-04） |

---

## 11. 下一步（编码启动 WBS 建议）
1. 脚手架：FastAPI 项目 + MySQL/Neo4j/ES/Redis 容器编排 + CI（lint+test 阻断）。
2. 核心域优先：collector（采集+敏感识别）→ semantic（指标状态机）→ consume（查询+meta）→ lineage（构图）。
3. 横切先行：鉴权/审计/降级中间件（避免后期返工）。
4. 前端并行：设计 token + 三栏骨架 + 指标详情页（溯源优先）先落地。
5. 一期 MVP 退出标准对齐 §9.1，按 PRD 7.1 周次滚动。
6. 二期评估接入 Cube：先验证"口径同步生成 cube 定义 + 预聚合命中率 + 回退路由"，确认收益后再切重负载下推/缓存（不替代口径状态机/血缘）。

---

## 12. 模块实现逻辑与数据流（逐模块细化）

> 本章对每个领域服务给出：**职责边界 → 核心流程步骤 → 接口调用链 → 数据流转 → 关键算法/容错**。
> 约定：服务间调用走内部 gRPC/HTTP（同步）或 Redis 事件（异步）；所有跨服务写操作经网关审计埋点。

### 12.0 跨模块数据流总览

```
[数据源] --pull/import--> collector --(元数据)--> MySQL.db_catalog
                                              \--(LLM候选)--> semantic(DRAFT)
                              collector --(表/字段节点)--> lineage --构图--> Neo4j
semantic(注册/审核) --(口径)--> MySQL.metric + 双写事件 --> Neo4j(血缘) + ES(检索) [+Cube(二期)]
semantic --(同名/相似)--> conflict --(裁决)--> MySQL.conflict + 通知
consume --(下推查询)--> OLAP ; (命中) --> Redis缓存 ; (消费方) --> QuickBI/DataAgent/前端
consume --(查询日志)--> lineage(CONSUMED_BY反填) + observability(度量) + recommend(埋点)
governance --(PII门禁)--> semantic(approve拦截) ; --(授权)--> consume(鉴权)
quality --(异常)--> notify(告警) ; observability --(审计)--> MySQL.audit_log
conflict --(漂移巡检 re-parse 源头SQL)--> 与 metric_version 比对 --> drift_scan_result --> notify(待办)   [PRD 4.7.8]
quality --(外部基准比对)--> external_benchmark --> reconciliation_record(ALERT) --> notify + conflict   [PRD 4.8.8]
consume --(Tier-1 查询/物化)--> metric_value_snapshot(WORM 存证)   [PRD 4.5]
semantic --(PENDING_VERSION)--> consume(确认回调) --> 升 CURRENT + 物化重建   [PRD 5.5.1]
```

#### 12.0.4 产品级端到端数据流（12 旅程 × 指标全生命周期）

> 本节把 PRD 3.3 的 12 个端到端旅程与指标全生命周期，落为**贯穿服务的数据流**：每流给出「触发角色 → 数据流步骤（经手服务 + 存储）→ 产出 → 回流/事件」。技术实现细节见各 §12.x；本节是「产品数据流全景」，与 §12.0 拓扑图互补（拓扑图看组件边，本节看业务流）。

**A. 指标全生命周期主数据流（核心骨架）**
```
[数据源] --采集--> collector --元数据/敏感-->MySQL.db_catalog + classification
                         \--LLM候选--> semantic(DRAFT)         [旅程二/五]
semantic(DRAFT) --提交审核--> REVIEW --PII门禁(governance)--> PUBLISHED
       │                         │
       │                         └--PII门禁拒绝--> REVIEW(回退,记拒因) [旅程十二]
       └--同名/相似--> conflict(OPEN) --仲裁--> canonical标记 / DEPRECATED [旅程六]
PUBLISHED --消费查询--> consume --下推--> OLAP --> 结果(meta:版本/新鲜度/粒度) [旅程一/四/十]
PUBLISHED --废弃--> DEPRECATED --缓存失效--> consume(下线) [旅程三待办闭环的一部分]
```
状态机权威在 `semantic`；所有状态迁移事件进总线 → notify（待办）/ observability（审计）/ assetmap（刷新）。

**B. 12 个旅程的数据流明细**

| 旅程 | 触发角色 | 数据流步骤（经手服务 → 存储） | 产出 / 回流 |
|------|----------|------------------------------|-------------|
| 一·溯源确认 | 分析师 | 搜索→`semantic`/`recommend`(ES term_idx)→指标详情→`lineage`(变更溯源链 L1/L2) | 标准口径 + 溯源链展示；置信来自 `metric_version` |
| 二·开发注册 | 开发 | ETL任务→`collector`(采集)→LLM候选→`semantic`(DRAFT)→提交→`governance`(审核)→PUBLISHED | `metric` 落库 + `metric.registered` 事件 → conflict/notify/lineage |
| 三·Owner待办 | Owner | `notify` 拉 `notification`(审核/裁决/反馈/确认)→`semantic`/`conflict`/`governance` 处理→状态迁移 | 待办清空；SLA 倒计时驱动（`observability` 度量） |
| 四·消费取数 | 分析师 | `consume.POST /query`→`governance`(鉴权)→`semantic`(口径AST+方言)→Redis(缓存)→OLAP→结果(meta) | 带口径版本/新鲜度结果；`consume.queried`→lineage/observability/recommend |
| 五·新员工术语 | 新人 | 术语库卡(`glossary`)→引用指标列表→模板克隆(`semantic`)→帮助引导提交 | 首注指标 DRAFT；`term.updated` 反哺 LLM few-shot |
| 六·跨域仲裁 | 双 Owner | `conflict` 检测(同名/粒度/跨域/版本)→协商→升级治理委→`conflict.arbitrate`→标记 canonical/DEPRECATED | `conflict` 记录 + 审计；落败方 `metric` 转别名/废弃 |
| 七·质量异常 | 质量引擎 | `quality` 检测(延迟/波动越界)→分级(P0/P1/P2)→`notify`(告警+关注者)→`consume`(结果标 quality_flag) | 分析师暂停决策；`quality.anomaly` 事件 |
| 八·能力降级 | 系统/用户 | LLM✗→`collector`退纯人工(503提示)；OLAP✗→`consume`返 503+retry_after；ES✗→MySQL LIKE | 三态降级 UX；主链路不崩（§5.2/§5.5） |
| 九·DataAgent消费 | 外部 Agent | `api_client` 申请→`governance` 换 JWT(token scope/白名单)→`consume`(限流/降级)→`ai`(MCP tool) | 受控程序化消费；审计留痕；429 按 retry_after |
| 十·QuickBI嵌入 | 分析师 | QuickBI 选指标→`consume.POST /embed/quickbi`→回传 metric_code+口径版本+维度约束→出图(标来源) | 报表可溯源；超界拖拽前端拦截粒度 |
| 十一·反馈迭代 | 用户 | `notify.POST /feedback`→`observability`(状态:采纳/拒绝)→路线图滚动输入 | 反哺采纳率；高频主题进 PRD 开放问题 |
| 十二·PII合规 | 数据开发 | 注册含 PII→`semantic` 触发 `governance.pii_review`(403 FORBIDDEN_PII 路由,非普通仲裁)→合规官复核→`classification` 落库→通过方进发布 | `metric.compliance_reviewed=true`；定期 `rescan`(COMPL-2) |

**C. 全局角色 × 数据流矩阵（谁经手什么数据）**
```
          采集  血缘  口径  冲突  质量  治理  消费  AI  通知  审计  资产图  推荐  术语  维度
分析师      ·   读   读/查  ·    ·    读    读   用   收    读   看    用   查    ·
开发       用    ·   写     ·    ·    申     ·    ·    ·     ·    ·     ·    ·    ·
Owner       ·    ·   审/写  裁   读    管     ·    ·   处    读   管     ·    ·    管
合规官      ·    ·    ·     ·    ·    审(PII) ·   ·    ·    读   管     ·    ·    管
新人        ·    ·   克隆   ·    ·     ·     ·    ·    ·     ·    ·    用   用    ·
外部Agent   ·    ·    ·     ·    ·    令牌  用   用    ·     ·    ·     ·    ·    ·
```
> 矩阵与 §2.1 服务清单、§3 接口、§12.x 实现一一对应；任一旅程的数据流均可在 B 表与 §12.x 双向追溯，构成「完整产品数据流」闭环。

**D. 单一指标对象全字段数据流（从生到死的字段级流转）**
```
① 采集期（collector → db_catalog）
   源表结构 ──> db_catalog(表/字段/类型/分区键/comment_inferred) + classification(sensitivity_level/pii_columns)
② 解析期（collector → semantic.DRAFT）
   ETL SQL + db_catalog + glossary_term + dimension ──LLM+Parser锚定──> metric(草稿):
     metric_code / type(原子/派生/复合) / agg_expr(AST) / grain / dimensions / unit /
     time_semantics / source_table_field / confidence / infer_basis
③ 注册审核期（semantic → 双写 + 门禁）
   metric.submit ──> REVIEW
     ├─ PII? ──是──> governance.pii_review(403 FORBIDDEN_PII) ──拒绝──> REVIEW(回退,reject_reason) ；通过──> 继续
     └─ 同名/相似? ──> conflict.OPEN(待仲裁)
   approve ──> PUBLISHED ──> 双写事件: Neo4j(metric节点+血缘) + ES(metric_idx) [+Cube 二期]
④ 消费期（consume → OLAP/Redis → 调用方）
   metric_code+version ──> semantic(口径AST) ──方言翻译──> SQL ──> OLAP ──> 结果
     附 meta: metric_version / freshness / granularity_bound / stale / source_trace / quality_flag
   查询结果 ──> Redis(键含version,缓存) + 事件 consume.queried
⑤ 反填期（consume.queried → lineage/observability/recommend）
   Report -[CONSUMED_BY]-> Metric (lineage 下游边) + audit_log(度量) + event_log(埋点)
⑥ 变更期（semantic 改口径）
   version+1 ──> 旧缓存键主动失效(12.0.2) ──> 新版本双写 ──> conflict(版本冲突提示)
⑦ 废弃期（semantic → DEPRECATED）
   metric.deprecated ──> consume(缓存失效) + conflict(关闭) + assetmap(标灰)
```
> 字段级流转与 §4 库表（`metric`/`db_catalog`/`classification`/`conflict`/`glossary_term`/`dimension`/`audit_log`/`event_log`）逐列对应。

**E. 存储间数据流向总表（数据从哪写到哪）**
| 数据 | 写入服务 | 写入存储 | 读取服务 | 备注 |
|------|----------|----------|----------|------|
| 源表元数据 / 敏感标记 | collector | MySQL.db_catalog / classification | semantic, lineage, assetmap | 采集即写 |
| 指标定义（真相源） | semantic | MySQL.metric | 全部消费方 | 状态机权威 |
| 指标血缘 | lineage | Neo4j | assetmap, conflict, consume | 双写最终一致 |
| 指标检索索引 | semantic/lineage | ES.metric_idx / term_idx | 搜索, recommend | 双写 |
| 查询缓存 | consume | Redis | consume | 键含 metric_version |
| 聚合/预聚合（二期） | semantic | Cube | consume | 同步失败回退自研 |
| 冲突记录 | conflict | MySQL.conflict | notify, governance | 仲裁留痕 |
| 分级分类结果 | governance | classification / db_catalog | assetmap, consume | 落库 |
| 权限/授权 | governance | MySQL.grants / audit_log | consume, observability | 鉴权依据 |
| 质量事件 | quality | MySQL.quality_event | notify, observability | 告警 |
| 通知/待办 | notify | MySQL.notification / event_log | 前端, recommend | 事件驱动 |
| 审计 | 网关中间件 | MySQL.audit_log | observability | 全写操作 |
| 术语/维度 | glossary/dimension | MySQL.glossary_* / dimension* | collector, semantic, consume | 治理底座 |
| 同源对账 | dimension | MySQL.reconciliation | conflict, notify | 定时 |
> 所有跨存储写入经「MySQL 为源 → 事件 → 异步 Neo4j/ES/Cube」双写（§5.1）；失败入重试队列 + 定期对账补偿。

**F. LLM 解析闭环数据流（呼应 PRD 4.3 端到端，仅出草稿不发布）**
```
触发: 全量(首接数据源扫描 etl_job+db_catalog) / 增量(新ETL/新表/结构变更,联动4.2)
  │
  ├─> collector 入 Redis队列(按数据源/优先级分桶,异步)
  │
  ├─> 上下文拼装(RAG): ETL SQL + 字段名/类型/原注释 + 表结构(分区键)
  │     + glossary_term(术语few-shot) + dimension(共享维度) + 数据示例
  │
  ├─> 双策略锚定:
  │     ├─ Parser优先: 提取 SELECT聚合/FROM-JOIN/WHERE/GROUP BY ──> "硬事实"骨架
  │     └─ LLM补位: 业务含义/口径描述/命名建议/单位时间语义 ──> "推断"(标AI推断)
  │
  ├─> 结构化输出(Pydantic): metric_code/type/agg_expr/dimensions/unit/
  │     time_semantics/source/source_field/confidence/infer_basis
  │
  ├─> 去重+冲突预检(联动conflict): 已存在→提示合并/复用; 同名不同义→预警
  ├─> 置信度分级: ≥0.85直进草稿待审 / 0.6–0.85预填高亮待人确认 / <0.6仅存待补
  ├─> 小样本校准门禁: golden set 校准,整体<0.85不进全量(仅小批量)
  │
  └─> 产物: metric(DRAFT草稿候选) + 推断依据持久化
         ├─> 人注册向导编辑 ──> 修正差异回灌 golden set(更准闭环)
         └─> 失败任务: 重试队列(指数退避); LLM✗暂停排队不丢(降级§5.2)
```
> 边界：解析结果仅草稿，不自动发布（呼应旅程二/五）；PII 推断受 governance 权限约束，不泄露敏感样本。

#### 12.0.1 服务间事件总线规范（C1：补齐异步契约）
各服务大量"发 `notify.todo`/`notify.alert`/`conflict`/`pii_review`"依赖统一事件机制，定义为 **Redis Stream 异步事件总线**（至少一次投递 + 消费幂等），不走同步 HTTP 调用以避免级联故障。

- **传输**：`Redis Stream`（持久化、可重放）；生产者写 `XADD`，消费者组 `XREADGROUP` 取，处理完 `XACK`；失败入 ` Dead Letter Stream`，人工/定时重试。
- **事件契约（统一信封）**：
  ```
  { event: "<DOMAIN>.<TYPE>",  producer: "<service>",  ts: ISO8601,
    payload: { ref_id, ref_type, actor_id, ctx_json },  trace_id: "<uuid>" }
  ```
- **主题/事件清单**：
  | 事件 | 生产者 | 消费者 | 触发动作 |
  |------|--------|--------|----------|
  | `metric.registered` | semantic | conflict, governance, notify, lineage | 冲突预检 / PII 门禁 / 待办 / 构图 |
  | `metric.published` | semantic | consume(Cube定义), assetmap, recommend | 缓存预热 / 地图刷新 / 推荐召回 |
  | `metric.deprecated` | semantic | consume(缓存失效), conflict(关闭) | 下线清理 |
  | `metric.pii_review` | semantic | governance | 合规门禁裁决 |
  | `conflict.opened` | conflict | notify | 待办给 GOV-1 |
  | `conflict.resolved` | conflict | semantic(标记 canonical), assetmap | 口径权威更新 |
  | `quality.anomaly` | quality | notify | 告警（P0/P1/P2） |
  | `classification.done` | governance | assetmap, notify | 热力刷新 / 敏感提示 |
  | `todo.created` | 任意 | notify | 站内/邮件/Webhook |
  | `consume.queried` | consume | lineage(反填), observability, recommend | CONSUMED_BY / 度量 / 埋点 |
  | `term.updated` | glossary(新增) | notify, collector(LLM上下文) | 引用方通知 / few-shot 刷新 |
  | `metric.pending_version` | semantic | notify, consume | 破坏性变更通知下游消费方确认（PRD 5.5.1） |
  | `metric.version_confirmed` | consume | semantic(升CURRENT), notify | 消费方确认/超时后版本生效 + 物化重建 |
  | `drift.detected` | conflict | notify | 口径漂移高等级告警进 Owner 待办（PRD 4.7.8） |
  | `reconciliation.alert` | quality | notify, conflict | 外部基准对账差异超阈值（PRD 4.8.8） |
   | `benchmark.imported` | quality | notify | 外部基准导入完成 + 绑定确认 |
   | `classification.changed` | governance | notify, assetmap, semantic | 数据分级变更（PII 升级/降级，R7-01），触发下游指标继承刷新 + 热力更新 + 合规告警 |
   | `config.changed` | governance / semantic | notify, observability | 平台配置变更（限流/配额/阈值/SLA，R9-07），通知管理员 + 审计 |
   | `grant.changed` | governance | notify, observability | 权限变更（授权/回收/过期，4.9.6），通知受影响用户 + 审计 |
   | `degradation.state_changed` | DegradationMiddleware | notify, observability | 依赖降级/恢复事件（4.13.5），通知管理员 + 入运维看板 |
   | `user.offboarded` | HR 回调 | governance | HR 离职事件 → 批量回收权限 + 吊销 token（4.9.6） |
   | `pii.anonymized` | governance | observability, notify | 被遗忘权执行完成通知（4.15.7） |
   | `snapshot.generated` | consume | observability | 指标结果快照落库事件（4.5，Tier-1 自动触发） |
   | `delivery.triggered` | notify | — | 指标投递事件（定时/阈值触发推送，4.5），走通知渠道投递 |
   | `export.requested` | consume | governance, observability | 数据导出请求（受权限+行数上限+脱敏管控，4.11.12） |
   | `auth.violation` | API 网关 | governance, observability | 越权尝试 / token 异常 / 批量授权异常（4.9.10 UEBA） |
- **幂等**：消费者以 `event + ref_id` 去重，重复事件安全忽略；`conflict.opened` 同 `metric_a/metric_b` 不重复建。
- **降级**：Stream 不可用 → 生产者落本地落盘队列，恢复后补发；不影响主链路写 MySQL。

#### 12.0.2 口径变更缓存与 Cube 失效一致性（C3）
防止"口径改了、缓存/Cube 仍返旧数"：
- **缓存键含版本**：Redis 查询缓存键 = `metric:{code}:v{version}:{dim}:{dateRange}`；口径 `version` 变更 → 旧版键自然过期（TTL）+ 主动 `DEL` 前缀，新查询落新版本。
- **Cube 定义失效**：`metric.published` v2 时，发 `cube.invalidate(code, v1)` → 删旧预聚合 + 重建 v2（二期）；重建完成前 consume 路由回自研下推（§2.2）。
- **双写对账**：定时任务比对 MySQL.metric_version 与 ES/Neo4j/Cube 的 `version` 字段，不一致标 `stale` 并触发补写（呼应 §5.1）。

#### 12.0.3 统一错误码应用（C2）
所有服务返回体 `{error_code, message, retry_after, trace_id}`，`error_code` 取 §5.4 语义表；网关中间件在 `403/409/422/429/503/504` 统一包装，越权(`403`)/PII(`FORBIDDEN_PII`)必写审计。消费方据 `error_code` 决策：
- `429` → 读 `Retry-After` 退避（US-EXT-2）；
- `503/504` → 提示 + `retry_after`，不静默返回缓存错数（除非 `accept_stale=true`）；
- `422` → 参数错误，前端拦截不改重试。

---

### 12.1 collector（采集服务，FR-02/03）

**职责边界**：仅负责"把源端元数据搬进平台"，不做口径推断的业务决策（LLM 推断结果只给 semantic 当草稿）。

**核心流程（通道 A：自动 pull）**
1. 接收 `POST /sources`（含连接串 → Secret Manager 加密存储，返回 `source_id`）。
2. 触发全量采集任务（arq 入队 `collect:full`）：
   - 用只读连接读 `information_schema`/`SHOW CREATE TABLE` 抽取表/字段/类型/注释/索引。
   - 敏感识别：正则 + 字典（phone/id_card/email/name 等）+ 列名启发式，打 `sensitivity_level`。
   - 幂等：以 `source_id + entity_name` 为 `upstream_signature` upsert `db_catalog`。
3. 写 `db_catalog` → 发 Redis 事件 `catalog.updated` → lineage 构图、assetmap 清点。
4. 增量：监听源端 DDL 变更事件（binlog/event trigger，按源类型适配）或定时 diff，仅处理变更实体。
5. 覆盖度计算：`coverage = 已采集实体 / 源端实体总数`，写入 `data_source.coverage`。

**通道 B：人工 import**（`POST /sources/{id}/import`）：提交 DDL/JSON + 样本，校验后同通道 A 落库（用于无法直连的生产库，作为人工证据）。

**接口调用链**：
- 入：`POST /sources` → collector.create_source → Secret Manager.store → arq.enqueue(collect:full)
- 出：collector → lineage（事件 `catalog.updated`）→ semantic（仅 LLM 候选，异步，可选）

**数据流转**：
```
源端(只读) --元数据--> collector --upsert(upstream_signature)--> MySQL.db_catalog
                                  --敏感标签--> db_catalog.sensitivity_level
                                  --事件--> Redis(catalog.updated) --> lineage/assetmap
```

**关键算法/容错**：
- 敏感识别：规则引擎（可配置字典），误报/漏报由治理人工修正并回填（写 `classification` 表，对应 FR-11 落库）。
- 限流：采集速率限制（行/秒）+ 低峰调度，避免打爆生产库。
- 失败：单表失败不影响整体，标 `stale` 重试（指数退避）；连接失败告警。
- **术语库上下文供给（联动 L1）**：采集元数据时拉取 `glossary_term` 标准词 + `aliases`，作为后续 `semantic` 中 LLM 解析的 few-shot 约束语料（呼应 PRD 4.6.4），不在此处做口径推断。

---

### 12.2 lineage（血缘服务，FR-04）

**职责边界**：负责"图"的构建与查询，不包含口径业务语义（口径→指标映射归 semantic）。

**核心流程**
1. 监听 `catalog.updated`：对新增/变更表字段建/更新 Neo4j `(:Table)/(:Field)` 节点。
2. **L1 血缘（表级）**：基于外键、命名约定（`fact_*`/`dim_*`）、ETL 作业元数据抽取表间 `LINEAGE_UP/DOWN`。
3. **L2 血缘（字段级）**：解析 ETL SQL / 视图定义（`EXPLAIN` 或 AST 解析），建字段级 `LINEAGE_UP/DOWN`。
4. **影响面计算**：`MATCH (f)<-[:LINEAGE_UP*..5]-(up)` 求上游，`*..5` 限制深度（性能 + 防爆炸）。
5. **运行时反填**：消费方查询日志（`consume` 回传）建 `(:Report)-[:CONSUMED_BY]->(:Metric)`，比静态推测更准。

**接口调用链**：
- 入：`GET /lineage/table/{id}`、`GET /lineage/field/{id}`、`GET /lineage/impact/{id}`（同步查 Neo4j）；`GET /lineage/{metric_code}?depth=&direction=&confidence_min=`（R13-10：按指标码查血缘，depth 默认 3/最大 10，direction=upstream|downstream|both，confidence_min 过滤低置信度边）
- 出：消费查询日志 → lineage 反填 `CONSUMED_BY`

**血缘查询响应结构（R13-10）**：
```json
{
  "nodes": [{"id":"table:sales_gmv_day","label":"Table","tier":"DWS"}, ...],
  "edges": [{"source":"field:gmv","target":"metric:sales_gmv_day","type":"DEFINED_BY","confidence":0.95}, ...],
  "meta": {"depth":3, "direction":"upstream", "total_nodes":12, "total_edges":15, "truncated":false}
}
```

**数据流转**：
```
collector(事件) --> lineage --> Neo4j(建节点/关系)
consume(查询日志) --> lineage --> Neo4j(CONSUMED_BY)
前端/assetmap --> GET /lineage/* --> Neo4j --> 返回图谱JSON
```

**关键算法/容错**：
- 深度限制默认 5 跳，超界提示"影响面过大，建议分域查看"。
- Neo4j 不可用 → 降级：血缘区标 `stale`，列表/详情仍可用（舱壁）。
- 解析失败字段标 `unknown_lineage`，不阻断构图。

**消费方追踪（第二期，PRD 3.10）**：第一期消费方感知基于 `DERIVED_FROM` 指标级反向边（覆盖"指标→指标"下游）；第二期扩展为"指标→表→任务/报表"物理全链路：
- 采集 `CONSUMED_BY` 边来源：① QuickBI 报表（数据集→物理表映射反查）；② Ad-Hoc 查询（Doris Query Log 高频访问模式）；③ Semantic API 调用日志（consume 回传）。
- 应用：指标变更影响面叠加表级消费方；上游表结构变更反向通知使用该表的所有指标 Owner；零引用孤儿指标清单供清理（assetmap）。

---

### 12.3 semantic（语义/指标服务，FR-05/06/07 + 状态机）

**职责边界**：**口径真相源**。所有指标定义、版本、状态机的唯一权威；执行引擎仅编排（一期自研 / 二期 Cube）。

**核心流程（状态机，对齐 PRD 5.5.1）**
```
DRAFT --submit--> REVIEW --approve--> PUBLISHED 或 EXPERIMENTAL(灰度)
  ^                   |                  |                    |
  |               reject(带原因)     deprecate          promote(灰度全量)
  +-------------------+------------------+                 │
                          ▲                                ▼
                    灰度回滚(rollback)                 PUBLISHED
PUBLISHED --改口径(PUT)--> 新版本: 非破坏性→CURRENT 直接生效 /
                           破坏性→PENDING_VERSION(消费方确认14天, 超时默认接受) → CURRENT
PUBLISHED --源表DROP/不可达--> DATA_SOURCE_DROPPED(异常子态, R5-01)
  DATA_SOURCE_DROPPED --源恢复/重绑定--> PUBLISHED
  DATA_SOURCE_DROPPED --确认废弃--> DEPRECATED
```
1. `POST /metrics`（DRAFT）：校验 `definition_json`（原子=表达式；派生=依赖指标+公式；复合=多指标聚合）+ 一等治理字段（granularity/unit/aggregation/time_semantics/freshness/sla/dw_layer/tier/serving_mode/additivity 必填，缺则门禁拦截）。
2. **冲突预检**：发 `conflict.check(metric_code, definition)` → 若命中同名/相似，挂 `pending_conflict` 标记（不阻塞注册，但审核时提示）。
3. **PII 门禁**：若 `pii_flag=true`，`approve` 前须 `governance.pii_review` 通过（COMPL-1），否则 `approve` 返 `409 COMPLIANCE_BLOCKED` 且 `status` 回 `REVIEW`（非 DRAFT，保留录入），记 `reject_reason` 并通知 Owner（D2，门禁为硬约束无例外）。
4. **F2/F3 门禁**：无 Owner 不可 PUBLISHED；派生/复合指标发布前递归校验依赖指标均 `PUBLISHED` 且非 `DEPRECATED`，并做 `DERIVED_FROM` 有向图环检测（F3/F16）。
5. `approve` → `PUBLISHED`（或选灰度则 `EXPERIMENTAL`，仅白名单租户/报表可见）：
   - 写 `metric` + `metric_version`（含 `change_type`/`diff_json` 结构化 diff）。
   - **双写事件** `metric.published` → Neo4j（指标节点 + DEFINED_BY/DERIVED_FROM 边）、ES（索引）、二期 Cube（cube 定义生成）。
6. **破坏性变更（PENDING_VERSION，PRD 4.5/5.5.1）**：`PUT /metrics/{code}` 改口径 → 按 `diff_json` 判定 `change_type`：
   - `non_breaking`（描述/别名/标签等）→ 直接升为 `CURRENT`，通知消费方（不阻塞）。
   - `breaking`（measure/formula/粒度/单位/owning_domain/物理表变更）→ 生成 `PENDING_VERSION`，不立即生效，默认查询仍命中旧 `CURRENT`；发 `notify` 通知全部下游消费方（经 `metric_lineage` 反向边）；消费方 `POST /versions/{id}/confirm|reject` 确认（14 自然日超时默认接受，可一次延期+7 天，明确拒绝则驳回带理由）；确认后 `PENDING_VERSION` 升为 `CURRENT` 并触发物化表重建（6.1）。
   - 灰度期（EXPERIMENTAL）：`POST /metrics/{code}/promote` 观察期满一键全量；`rollback` 异常一键回退上一 PUBLISHED（触发 4.14 通知 + 审计）。
7. 注册审核待办：发 `notify.todo`（给 domain_admin/reviewer）。
8. 模板/驾驶舱：从 `metric` 聚合生成 `GET /metrics/dashboard`。
9. **备份 Owner 兜底（PRD 4.9.6）**：主 Owner 缺席（HR 离职事件/超时未响应）时副 Owner 自动承接审批与治理职责；无副 Owner 则转 `domain_admin` 代管（14 天内指定新 Owner），交接留审计。

**接口调用链**：
- 入：§3.2 全部端点
- 出：→ conflict.check → governance.pii_review → notify.todo →（双写）Neo4j/ES/Cube

**数据流转**：
```
用户(前端) --POST/PUT/submit/approve--> semantic
  semantic --冲突预检--> conflict
  semantic --PII门禁--> governance
  semantic --待办--> notify
  semantic[PUBLISHED] --双写事件--> MySQL.metric_version + Neo4j + ES [+Cube]
```

**关键算法/容错**：
- 状态机非法跃迁（如 DRAFT 直接 deprecate）→ `409`。
- **PII 门禁拒绝回退（D2）**：`approve` 时若 `governance.pii_review` 拒绝，指标 `status` 回退 `REVIEW`（非 DRAFT，保留录入），记 `reject_reason` 并通知 Owner；仅当门禁通过才允许 `PUBLISHED`。门禁为硬约束（COMPL-1），无例外。
- 双写：任一路失败入重试队列，读侧对缺失标 `stale`（见 §5.1）。
- 口径表达式沙箱校验（防注入/无限递归依赖）。
- **口径 AST → 方言 SQL 翻译层（D1）**：统一 AST 表达 `SUM/AVG/COUNT/COUNT(DISTINCT)/CASE WHEN/RATIO`；经"方言适配器"（MySQL / PostgreSQL / Doris / Hive 各实现 `to_sql()`）生成下推 SQL。统一处理：维表 JOIN、`COUNT(DISTINCT)` 跨维上卷走**重算而非汇总**（避近似误差）、时区（统一 UTC 存储→展示时区转换）、币种换算（按 `currency_rate` 维度表）、分区裁剪随 `dateRange` 自动生效。新增数据源类型仅补适配器，业务零改。派生/复合指标递归展开依赖指标 AST 后合并翻译。
- **命名规范校验（PRD 4.5 `naming`）**：注册/更新时校验 `metric_code` 符合 `域_业务对象_度量_统计周期`（如 `sales_gmv_day`），命中保留词或不合规 → 提示修正（软提醒）；同域重名 → 阻断（硬约束，联动 §12.4）。
- **数仓分层驱动差异化（PRD 4.5 `dw_layer`）**：采集（collector）按库名前缀（`dws_`/`ads_`/`dwd_`）自动推断 `dw_layer`，人工可覆盖。分层驱动：① 质量 SLA——DWS/ADS 严于 DWD（如 DWS 06:30 前就绪、DWD 宽限 08:00）；② 审核流——DWS/ADS 注册/变更须 `reviewer` 审核，DWD 走"自动门禁 + Owner 确认"简化流；③ 血缘精度——DWS/ADS 要求字段级（L2），DWD 允许表级（L1）；④ 治理驾驶舱按层聚合，DWS 权重最高。

**紧急发布快通道（PRD 5.5.1/R6-04）**
- **逻辑**：`domain_admin` 可跳过常规审核环节直接发布，但须满足：(1) 填写紧急原因（监管要求/生产事故/数据泄露）；(2) 24 小时内补审并留 `EMERGENCY_PUBLISH` 审计标记；(3) 补审未通过须立即回滚。紧急发布的指标在详情页标"⚠ 紧急发布待补审"，补审通过后标记清除。
- **合规门禁不可跳过**：含 PII 的指标紧急发布仍须 `compliance_officer` 复核通过——合规门禁是安全硬线，不可因紧急而豁免；若合规官不可达，该指标仅可按 `INTERNAL` 分级发布（PII 维度值全脱敏），合规复核补审后方可放开 PII 维度可见范围。
- **状态机扩展**：`DRAFT ──紧急发布──▶ PUBLISHED`（带 `emergency_publish=true` + `reason`），跳过 REVIEW 但不跳过 PII 门禁。

**PENDING_VERSION 期间 Schema Drift 处理（PRD 5.5.1/R9-02）**
- 版本待生效期间若 4.2 Schema Drift 检测到源表结构变更（如字段删除/类型变更），新版本口径可能已失效：
  1. 自动暂停版本切换并通知 Owner："源表结构已变更，请复核新版本口径是否仍有效"
  2. Owner 须在 7 天内确认或重走口径修改→重新提交审核
  3. 逾期未处理则自动回退到旧版本（PENDING_VERSION→取消），避免失效口径静默生效
  4. Schema Drift 检测事件写入 `schema_drift_event`，关联 `metric_version.id`

**指标消费指南（PRD R3-20，Owner 维护的消费方指导）**
- 指标详情页"消费指南"Tab：Owner 可编写适用/不适用场景、常见误用、推荐用法示例
- 存储于 `metric` 表 `consumption_guide` JSON 字段：`{applicable: [...], not_applicable: [...], common_misuse: [...], recommended_usage: [...]}`
- 消费指南修改为 `non_breaking` 变更，直接升 CURRENT 不走 PENDING_VERSION

**存量迁移质量校验（PRD 6.7/R8-05）**
- **迁移策略**：
  1. 迁移后指标默认标 `DRAFT`（非直接 PUBLISHED），须 Owner 确认口径 + 绑定数据源后走审核发布
  2. 存量指标无 Owner → 由 `domain_admin` 指定临时 Owner（30 天内须认领，逾期标"待认领"告警）
  3. 迁移批次策略：按域分批（每批 ≤ 200 指标），每批完成后跑 4.8 质量规则抽样校验（校验通过率 ≥ 90% 方可继续下批）
  4. 迁移进度与质量在运营看板可见（`migration_batch` 记录批号/域/指标数/成功数/失败数/质量通过率）

**指标健康度评分（PRD 5.5.3，治理驾驶舱数据源）**
- **模型**：单指标 0–100 分，五维加权（权重可配，默认：口径完整度 25% / 活跃度 20% / 质量 25% / Owner 响应 15% / 血缘覆盖 15%）。
- **口径完整度**：一等字段（granularity/unit/aggregation/time_semantics/freshness/sla/source/dimensions）齐全率。
- **活跃度**：近 30 天 consume 查询/消费次数归一化（源：`consume.queried` 事件 → observability 聚合）。
- **质量**：近 30 天 `quality_event` 异常数反比 + 质量门禁通过率。
- **Owner 响应**：反馈/审核平均时效（SLA 内比例，源：audit_log）。
- **血缘覆盖**：上游解析率（物理表→字段覆盖，源：lineage）。
- **刷新与分级**：每日凌晨批量重算 + 关键事件（质量异常/状态变更）实时增量；≥85 优（绿）/ 70–84 良（蓝）/ 55–69 警（橙）/ <55 危（红）；红橙指标自动进整改待办（notify.todo）。某维度数据缺失（如埋点未覆盖）→ 该维记 0 并标"数据不足"，不臆造分数。
- **用途**：`GET /metrics/dashboard` 驾驶舱、治理红黑榜、指标退役建议（长期低活跃 + 低消费自动建议 DEPRECATED）。

**指标对比工具（PRD 4.5）**
- `POST /metrics/compare`：入参 `{metric_codes: [code_a, code_b]}`（上限 2，一期；二期可扩多指标对比），返回两指标关键字段并排差异：
  - `definition`（measure/formula 表达式 diff）、`granularity`、`dimensions`（维度集差集/交集）、`unit`/`currency`、`source_tables`（来源表差异）、`time_semantics`、`additivity`、`usage_notes`
  - 差异字段高亮标记（`difference_level: identical|similar|different`）
- **触发入口**：指标目录勾选两个指标 → "对比"（§7.6 `/compare` 页）；冲突仲裁面板也可复用此接口（§12.4 协商面板展示 diff）
- **实现**：读 `metric` 表两行定义 + `metric_lineage_source` 来源表对比 + `metric_dimension` 维度集对比，纯读聚合，不走下推
- **权限**：须同时有 A/B 两指标的查看权限，否则 403

---

### 12.4 conflict（冲突服务，FR-09）

**职责边界**：检测同名/相似口径，提供仲裁工作流，不修改指标本身（仅写裁决结论 + 通知）。

**核心流程**
1. 接收 `conflict.check`（来自 semantic 注册）：计算 `similarity(definition_a, definition_b)`。
   - **① 同名不同义（硬冲突）**：`metric_code` 相同但 `domain` 不同或 `definition` 差异 > 阈值 → 阻断发布（`409 CONFLICT`），最高优先级。
   - **② 同义不同名（重复建设）**：口径实质相同（embedding 余弦 > 0.85，术语+表达式向量）但命名各异（如 `sales_amt` vs `gmv_total`）→ 不阻断，建冲突候选提示合并（对应"识别并合并重复指标 ≥30%"）。
   - **③ 粒度/单位冲突（软冲突）**：同名但统计周期（`_day`/`_month`）或单位（`yuan`/`cent`）不同 → 不阻断，仅提示消费方绑定正确粒度/单位（PRD 4.7.1）。
   - **④ 跨域同口径异源**：同一业务含义指标指向不同物理表 → 提示合并或明确"权威源"。
   - **口径版本冲突**：同一指标新旧版本并存，消费方绑错版本 → 提示升级。
   - **PII 冲突（特殊路由，C4）**：含 PII 口径在无权域被引用 → **不进普通仲裁**，转交 `governance.pii_review`（403 FORBIDDEN_PII），由合规裁决而非业务仲裁。
2. 命中 → 建 `conflict`(OPEN) + 发 `notify.todo`（给治理角色 GOV-1）。
3. 仲裁 `POST /conflicts/{id}/arbitrate`：
   - 选唯一口径 / 合并 / 保留差异（标 `canonical`）。
   - 写 `conflict.arbitrator_id + decision_json`（GOV-2 裁决记录）。
4. 超时未决 → `escalate`（升级通知）。

**接口调用链**：
- 入：`conflict.check`（内部）、`GET /conflicts`、`POST /conflicts/{id}/arbitrate`、`POST /conflicts/{id}/escalate`
- 出：→ notify.todo →（裁决结果）semantic（标记 canonical）

**数据流转**：
```
semantic(注册) --conflict.check--> conflict --相似度--> MySQL.conflict(OPEN)
conflict --裁决--> MySQL.conflict(ARBITRATED) + notify + semantic(canonical标记)
```

**关键算法/容错**：
- 相似度模型：术语共现 + 表达式 AST 相似 + embedding；阈值可配。
- 裁决记录不可删（审计留痕），仅可追加变更。

**口径漂移巡检（drift detection，PRD 4.7.8，P1）**
- **职责**：检测"ETL 悄悄改了源头 SQL 但平台注册口径未更新"，防"平台说一套、ETL 算一套"。
- **流程**：
  1. **触发**：① 定期（默认每周，Tier-1/2 优先）；② `catalog.updated`/`etl_job` SQL 文本变更事件即时触发（4.2 增量采集联动）。
  2. **re-parse**：对指标绑定的源头 ETL SQL 重新解析（复用 §12.1 collector 的 Parser 优先 + LLM 补位），提取当前实际口径（聚合方式/过滤条件/来源字段/单位）。
  3. **比对**：实际口径 vs `metric_version` 当前生效版 → 复用 §12.4 相似度计算（AST 归一 + embedding）。
  4. **分级处置**：`drift_level=HIGH`（过滤增删/聚合改/来源字段变）→ 写 `drift_scan_result(OPEN)` + `notify` 进 Owner 待办；`LOW`（注释/命名）→ 仅提示。
  5. **Owner 闭环**：`POST /drift-scans/{id}/confirm`——`CONFIRMED_INTENT`（有意变更→引导走 semantic 变更评审/PENDING_VERSION）；`CONFIRMED_UNINTENT`（无意→通知 ETL 开发者回改源头 SQL）；`IGNORED`（加巡检白名单）。处置结果入审计。
- **能力边界**：只检测不自动改口径（改口径走 semantic 变更评审）；动态 SQL/跨系统断层处依赖 LLM、标 `drift_uncertain` 人工复核；巡检复用 LLM 配额按 Tier 优先 + 白名单控制范围。
- **验收**：Tier-1 指标源头 SQL 变更后 24h 内检出并告警；漂移闭环率 ≥ 90%。

---

### 12.5 governance（权限合规服务，FR-11）

**职责边界**：RBAC/域授权 + PII 合规门禁 + 分级分类；是 consume 鉴权与 semantic approve 的前置闸门。

**核心流程**
1. 角色模型（**对齐 PRD 4.9.2**）：`platform_admin`（跨域运维）/ `domain_admin`（本域管理+审批本域提交）/ `metric_owner`（本指标编辑/下线）/ `reviewer`（审批 PENDING_REVIEW）/ `compliance_officer`（PII/合规复核门禁）/ `viewer`（只读浏览，含被授予的跨域只读引用）。权限 = 角色 × 主题域；跨域只读经 `grants.grant_type=READ`（授权模型非复制，源域保留 WRITE）。
2. 授权：`POST /grants`（域 + 指标白名单 + 行级开关），写入 `grants`。
3. **PII 门禁**：`POST /pii/review`（COMPL-1）复核分级与脱敏策略，写 `metric.compliance_reviewed=true`；semantic.approve 前查此标志。门禁拒绝时 conflict 服务收到 `403 FORBIDDEN_PII` 路由（C4），不进普通仲裁。
4. **分级重扫**：`POST /classification/rescan`（COMPL-2）→ 调分级引擎重算 `db_catalog.sensitivity_level` + 写 `classification` 表（sensitivity_level/pii_columns/model_version）+ 触发 assetmap 热力刷新事件 `classification.done`。引擎不可用时降级（§5.5），标 `UNKNOWN` 不阻断。
5. 鉴权查询：`GET /me/permissions` → 返回当前用户域/指标权限快照，供 consume 与前端按钮级控制。
6. **维度映射维护**：维护 `dimension` / `dimension_mapping` / `metric_dimension`（L2 落库）；消费侧 `POST /query` 校验维度合法性时反查此表。
7. **同源口径对账调度**：定时任务跑 `reconciliation`（同源多指标一致性），差异超 `threshold` → 写 `reconciliation.status=ALERT` + 触发 conflict 候选（防"同表出俩数"）。

**接口调用链**：
- 入：§3.5 全部端点
- 出：→ semantic（approve 拦截）、→ consume（鉴权依据）、→ assetmap（热力刷新）、→ conflict（PII/同源对账候选）

**数据流转**：
```
用户/合规官 --授权/复核--> governance --写--> MySQL.grants / metric.compliance_reviewed / classification
governance --门禁--> semantic(approve拦截) / conflict(403 FORBIDDEN_PII 路由)
governance --权限快照--> consume(查询鉴权)
governance --分级结果--> assetmap(热力) + classification(落库)
```

**关键算法/容错**：
- 越权访问 → `403 FORBIDDEN` + 审计（observability）。
- 行级权限一期为骨架（域级），二期深化（行级表达式）。
- 分级引擎降级见 §5.5，不影响注册/发布主链路。

**PII 识别三路融合（PRD 4.15.3，字段级敏感判定核心）**

PII 字段判定由三路信号取并集，任一路命中即标 `PII`（宁误标可人工纠正，不漏标致泄露）：

1. **字段名/注释正则匹配**：按 `pii.identification.regex_list` 配置（默认含 id_card/phone/email/name），匹配字段名或注释关键词
2. **类型启发式**：字符串类型 + 长度/格式特征（如 `VARCHAR(18)` 且含数字→疑似身份证号），启发规则硬编码可配
3. **LLM 样本推断**：collector 采集时送字段名+注释+脱敏样本（受 4.9 约束不泄露敏感值）给 LLM，LLM 返回 PII 概率

**融合规则**：① 三路结果取并集（任一路标 PII → 字段标 PII）；② 正则匹配为硬规则不可覆盖；③ LLM 推断降级时不自动标 PII（防误标误拦），留"待人工复核"标记；④ 误标可由 `compliance_officer` 在复核时纠正（写 `data_classification.verified_by/verified_at`）；⑤ schema 变更后触发 §4.2 重扫防漏标

**PEP→PDP 决策引擎（PRD 4.9.1/4.15.5，RBAC×ABAC 融合核心）**

所有鉴权请求（API/页面/AI）经统一决策管线，PEP 提取属性、PDP 匹配策略、输出决策，全链路入审计：

```
请求 → PEP(提取属性) → PDP(策略匹配) → 决策 → 执行 → 审计
```

1. **PEP（Policy Enforcement Point，策略执行点）**：
   - 提取主体属性：`user_id` / `role` / `domain_grants` / `metric_whitelist`
   - 提取资源属性：`resource_type`（domain/metric/dimension/export） / `sensitivity_level` / `metric_status`
   - 提取环境属性：`time_of_day`（是否工作时间） / `network_zone`（内网/VPN/外网） / `client_type`（browser/api_client/mcp_agent）
   - 提取动作属性：`action`（read/write/approve/export/query）
   - 属性缺失时按最严策略处理（Deny 或 Mask），不默认放行。

2. **PDP（Policy Decision Point，策略决策点）**：
   - **策略匹配算法**：
     - 按 `role_id` + `resource_type` + `action` 三元组从 `policy` 表加载候选策略集
     - 按 `priority` 降序排列，逐条评估 `abac_condition`
     - 首条匹配的策略即为决策结果；无匹配策略 → 默认 Deny（fail-closed）
   - **ABAC 条件表达式求值**（`abac_condition` JSON 结构）：
     ```json
     {
       "operator": "AND",
       "conditions": [
         {"attr": "env.time_of_day", "op": "in_work_hours", "value": true},
         {"attr": "env.network_zone", "op": "in", "value": ["intranet","vpn"]},
         {"attr": "resource.sensitivity_level", "op": "not_equals", "value": "PII"}
       ]
     }
     ```
     支持算子：`equals` / `not_equals` / `in` / `not_in` / `greater_than` / `less_than` / `in_work_hours` / `contains_any`
   - **决策输出**（对齐 PRD 4.15.5）：
     | 决策 | 语义 | 后续动作 |
     |------|------|----------|
     | `ALLOW` | 完全允许 | 正常返回数据 |
     | `DENY` | 拒绝 | 返回对应 403 错误码 + 审计 |
     | `ALLOW_WITH_MASK` | 允许但须脱敏 | 调脱敏引擎处理 PII/敏感维度值 |
     | `ALLOW_SCOPED` | 允许但限定维度值范围 | 按维度值 RLS 过滤结果集 |
   - **PDP 决策缓存**（PRD 4.9.9 ABAC 性能边界）：
     - 缓存 key = `hash(user_id + role + resource_type + resource_id + action + abac_condition_hash)`
     - 缓存 value = 决策结果 + 过期时间
     - TTL = 60s（短 TTL，策略变更 1 分钟内生效）
     - 存储：Redis，`pdp:decision:{hash}` 前缀
     - 失效：策略变更（`POST /policy` / `PUT /policy`）时主动清除该 role 相关所有缓存键（按 `role_id` 前缀 SCAN + DEL）
     - 极端策略数（>1000 条/角色）评估：超过阈值告警 `platform_admin`，建议拆分策略或合并条件

3. **PII 沿血缘自动传播（PRD 4.15.2）**：
   - 上游表/字段标 PII → 沿 Neo4j `INHERITS_CLASSIFICATION` 边传播到下游指标
   - 传播规则：`sensitivity_level` 取上游最大值（PII > CONFIDENTIAL > INTERNAL > PUBLIC）
   - 写 `data_classification` 记录，`inherited_from` 指向上游实体 ID
   - 传播触发时机：① 采集分级结果更新时；② 合规官复核后分级变更时；③ 指标新绑 PII 源字段时
   - 推断降级时**不自动标 PII**（防误标误拦），留"待人工复核"标记

**脱敏策略引擎（PRD 4.15.4，PDP ALLOW_WITH_MASK 的执行层）**

PDP 输出 `ALLOW_WITH_MASK` 时，由脱敏引擎按策略对查询结果执行脱敏处理：

1. **四种脱敏策略**（按角色/环境/字段敏感度选择）：
   | 策略 | 实现 | 适用场景 | 示例 |
   |------|------|----------|------|
   | `MASK` | 保留首尾，中间用 `*` 填充 | 手机号/身份证号展示 | `138****8000` |
   | `HASH` | SHA-256 取前 8 位十六进制 | 须唯一标识但不可逆 | `a3f2c1b8` |
   | `GENERALIZE` | 映射到更粗粒度区间 | 年龄/金额/位置 | `年龄 30-40` / `金额 1万-5万` |
   | `NULLIFY` | 直接置空不返回 | 极高敏字段对无权角色 | `null` |

2. **脱敏策略选择算法**：
   ```
   输入: role, sensitivity_level, field_name, env
   规则优先级: policy.mask_strategy > 字段级默认 > 分级默认
   分级默认:
     PII + viewer/external_agent → MASK
     PII + analyst(in_work_hours+intranet) → 明文（ALLOW，不进脱敏引擎）
     PII + analyst(off_hours/VPN) → MASK
     CONFIDENTIAL + viewer → GENERALIZE
     CONFIDENTIAL + external_agent → NULLIFY
   兜底: 策略缺失 → 默认 NULLIFY（最严，Deny 语义的数据层等价）
   ```

3. **维度值 RLS 过滤（ALLOW_SCOPED 的执行层）**：
   - PDP 输出 `ALLOW_SCOPED` + `scope_dimensions` → 查询结果按维度值过滤
   - 例如：`scope_dimensions = {"region": ["华东","华北"]}` → 查询 WHERE 子句追加 `region IN ('华东','华北')`
   - 与 SQL 方言翻译层联动：追加的过滤条件经适配器生成方言 SQL

4. **脱敏不阻断查询链路**：脱敏在结果集后处理（非 SQL 层），不影响查询执行计划与缓存键；脱敏后结果不写入缓存（避免脱敏结果被其他请求复用导致泄露）。

**被遗忘权实现（PRD 4.15.7/R7-06，WORM 与 PIPL 协调）**

PIPL 撤回同意/销户时，审计中个人标识**覆写脱敏**而非物理删除（WORM 约束行不可删改）：

1. **覆写脱敏管线**：
   - 触发：用户销户/撤回同意事件（HR 系统回调或用户自助操作）
   - 定位：扫描 `audit_log` 中 `actor.user_id = :target_uid` 的所有行
   - 覆写：将 PII 字段替换为不可逆标识
     - `actor.name` → `ANONYMIZED_<SHA256(uid)[:8]>`
     - `context.IP` → `ANONYMIZED_<hash>`
     - `context.UA` → `ANONYMIZED`
     - 非个人标识字段（`action`/`resource_type`/`resource_id`/`before`/`after`/`result`）保留不变
   - 覆写操作本身入审计：`action=PII_ANONYMIZED`, `resource_type=user`, `resource_id=:target_uid`, `after={"fields_anonymized": ["actor.name","context.IP"]}`
   - 覆写完成后标记用户记录 `anonymized_at = NOW()`

2. **禁止物理删除审计行**：WORM 约束优先于 PIPL 删除权——保留事件行但使个人不可识别，两者不矛盾。

3. **批量覆写性能**：按用户 ID 批量 UPDATE，每批 1000 行；大用户（>10 万审计行）异步执行+进度看板。

4. **能力边界**：仅作用于平台审计侧标识；源库数据清除归源端负责。

**临时授权 TTL 与权限回收（PRD 4.9.6）**

1. **临时授权**：`POST /grants` 时可传 `expires_at` 字段（DATETIME），写入 `grants.expires_at`
2. **自动回收**：Worker 定时扫描 `grants WHERE expires_at < NOW() AND status = 'ACTIVE'`（每 5 分钟），批量置 `status=EXPIRED` + 发通知 + 审计
3. **HR 离职事件联动**：HR 系统发 `user.offboarded` 事件 → governance 接收 → 批量回收该用户所有 `grants` + 角色绑定 + API token 吊销 + 审计
4. **权限影响预览（what-if）**：`POST /grants/batch/dry-run` 返回受影响用户数/指标数/权限变更类型，管理员确认后执行

**审批回避与交叉审批（PRD 4.9）**
- **规则**：审批人（`reviewer`/`domain_admin`）若是指标 Owner 或创建者，系统自动检测并重定向——同域另一 `domain_admin` 或 `platform_admin` 执行审批；本域无其他 `domain_admin` 则升级至 `platform_admin`。**不允许自审通过**。
- **实现**：`semantic` 的 approve 端点校验 `approver_id != metric.owner_id` 且 `approver_id != metric.creator_id`；命中则返 `409 CONFLICT（自审不允许，已重定向）`，前端提示改用可审批人。
- **审计**：重定向事件写 `operation_audit`（action=APPROVE_REDIRECTED, reason=self_review_blocked），供治理复盘。

---

### 12.6 consume（消费/查询服务，FR-12/13）

**职责边界**：平台对外的"可信口径查询出口"。负责下推、缓存、meta 标注、鉴权限流降级；不持有源数据。

**核心流程（查询主链路）**
1. `POST /query`（前端 / api_client）：
   - **鉴权（token 模型，D3）**：用户态 `Bearer JWT` → governance.permissions 校验域/白名单；消费方 `X-Api-Key` → 换短效 `Bearer JWT`（含 `client_id/scope/tenant_id/metric_whitelist/TTL`），密钥经 Secret Manager，可吊销可轮换；越权 `403`+审计。
   - **限流（D3）**：按 client 维度 Redis 滑动窗口（`QPS=20`、日调用 `10万`、单查询扫描行上限 `scan_row_limit`）；超限 `429`+`Retry-After`；**免费额度超限 → `429 FREE_QUOTA_EXCEEDED`（响应含 `budget_reset_at`，R10-01/R11-07）；月度预算超 100% → `429 BUDGET_EXCEEDED`（响应含 `budget_reset_at`，R10-01/R11-09）**。热点驱动 semantic 预聚合。
   - **口径解析（语义锚定）**：用 `metric_code`+`version` 从 MySQL 取 `definition_json`（AST），**绝不接受裸 SQL**，降 NL 幻觉。
   - **维度/粒度校验**：反查 `metric_dimension`+`dimension` 验证 `dimensions` 合法性；细于 `granularity_bound` → `422 GRANULARITY_VIOLATION`（硬约束，呼应 4.5）。
   - **批量指标维度交集自动求取（R13-09）**：`POST /query` 请求含多个 `metrics` 时，平台自动求取各指标合法维度集的交集作为查询维度——交集为空则返回 `422 DIMENSION_INVALID`（提示哪些指标无可共享维度）；消费方也可显式指定维度子集（须为交集子集）。
   - **可加性校验（additivity，PRD 4.5）**：请求按维度上卷时，若指标 `additivity=NON_ADDITIVE` 或 `SEMI_ADDITIVE` 且目标维度落在 `non_additive_dimensions` → `422 ADDITIVITY_VIOLATION`，引导走重算路径（COUNT(DISTINCT)/AVG 上卷重算而非汇总，避近似误差）。`orderBy` 引用字段受 ADDITIVE 约束——NON_ADDITIVE 字段仅允许按其非可加维度分组后排序（R13-08）。
   - **缓存查询（C3）**：`key = metric:{code}:v{version}:{dim}:{dateRange}:{comparison_hash}`；命中且未过期 → 返（带 `freshness`）；口径版本变更时旧键主动失效。
   - **方言翻译（D1）**：`definition_json` AST → 调 semantic 的"口径→方言 SQL 翻译层"生成目标引擎 SQL（MySQL/PG/Doris/Hive 适配器），统一处理维表 JOIN / 时区 / 币种 / 分区裁剪。
   - **comparison 结构化参数（R13-07）**：`{"type":"yoy|mom|wow|custom","offset":1,"base_date":null}`——`type=yoy` 默认 offset=1（上年同月），`custom` 须显式传 offset+base_date；翻译层将 comparison 展开为 UNION ALL 子查询（当期 vs 对照期）。
   - **orderBy 结构化参数（R13-08）**：`{"field":"gmv","direction":"asc|desc"}`——field 须为请求 metrics 中合法指标码或维度，direction 默认 desc。
   - **幂等性（R13-06）**：请求可含 `idempotency_key`（UUID），24h 内相同 key → 直接返缓存结果（不重复下推），key 与 `client_id` 绑定隔离；无 key 请求不保证幂等（正常多提交由消费方去重）。
   - **批流双路路由（serving_mode，PRD 4.5）**：指标为 `BATCH_REALTIME_DUAL` 时，默认取实时路径（经 `data_set.serving_path=REALTIME` 解析），消费方可显式 `serving_mode=batch` 走批路径；响应 `meta.freshness` 返回双路径就绪态（`{batch:"T+1 08:30", realtime:"5min"}`）。质量规则按路径分别配置。
   - **路由决策**：命中物化/聚合表 → 直查（最快）；否则按 serving_mode 路径实时下推 OLAP（一期自研 / 二期 Cube `/cubejs-api/v1/load`）。
   - **执行保护**：扫描行 > `quota` → `422 QUOTA_EXCEEDED_SCAN`；超时 → `504 QUERY_TIMEOUT`+预计恢复；可 `query_id` 取消在途（§3.6 `GET /query/{id}/cancel`）。
   - **OLAP 不可达**：`503 DEPENDENCY_DEGRADED_ENGINE`+`retry_after`；仅当 `accept_stale=true` 返陈旧缓存（§5.2 舱壁）。
   - **DEPRECATED 指标访问控制（R13-11）**：默认拒绝访问 DEPRECATED 指标（`403 FORBIDDEN_DEPRECATED`）；消费方显式传 `accept_deprecated=true` 可在 Sunset 期内继续查询（口径钉住旧版本）；Sunset 期满后返回 `410 METRIC_VERSION_SUNSET`。彻底退役指标（GONE）返回 `410 METRIC_DEPRECATED` 须转 successor。
   - **meta 标注（对齐 PRD 4.11.7 + R13-14 serving_mode）**：`metric_version / freshness / granularity_bound / sample / stale / source_trace / quality_flag / serving_mode(batch|realtime)`（低于阈值标 `quality_downgraded`）。
   - **结果回写**：写缓存 + 发 `consume.queried` 事件 → lineage(CONSUMED_BY 反填)/observability(度量)/recommend(埋点)。
2. `POST /query/dry-run`（L3，4.11.6）：对样本分区跑口径，返样例结果+耗时+扫描行数；**不计费/不写生产/不进缓存**，用于注册/改口径 Owner 比对、质量规则预校验。
3. `POST /embed/quickbi`（FR-12）：取嵌入令牌（含 scope 约束），回传 `metric_code`+口径版本+合法维度/粒度边界；超界拖拽前端拦截"已聚合至 X 粒度"。
4. `GET /embed/quickbi/card`（口径卡片，PRD 4.11.1）：QuickBI 报表侧边栏嵌入——按 `metric_code` 返回口径摘要卡（业务描述 / formula 摘要 / 口径版本角标 / 新鲜度 / 质量分 / 关联术语链接），不离开报表即可查看口径；卡片数据与 `GET /metrics/{code}` 同源（同一语义层），受嵌入令牌 scope 约束。
4. `/metrics/{code}/semantic`：只读语义拉取（api_client），受 scope + `metric_whitelist`。
5. **消费侧运行时冲突检测（L3，4.11.8）**：从 `consume.queried` 日志提取"指标名/口径版本/消费方"三元组；发现两报表以不同口径版本调用同一指标名 → 触发运行时告警 + 生成 `Conflict` 候选（走 12.4 仲裁闭环），防"线上已出俩数"。
6. **查询血缘反填（L3，4.11.10）**：实际查询日志回灌 `lineage`，补全 `Report -[CONSUMED_BY]-> Metric` 下游边（比静态推测更准），支撑 conflict 运行时冲突 / observability 度量 / assetmap 热度。
 7. **结果快照存证（L3，PRD 4.5，P1）**：对 Tier-1 及涉财务/合规指标，查询结果（即时查询或物化）落 `metric_value_snapshot`（WORM，只写不删）——含 `metric_code`/`version`/`dims`/`date_range`/`value`/`quality_flag`/`generated_at`；供 4.7 争议仲裁证据回溯、外部审计佐证、跨期对账（"上月报财务的数 vs 本月口径"）。`GET /snapshots` 查询受 governance 权限 + 行数上限 + PII 脱敏。快照生成对 Tier-1 全量、Tier-2/3 可配（默认不落，控制存储）。
    - **触发时机（R3-09）**：Tier-1 指标分区产出就绪 + 4.8 质量校验通过后自动触发快照落库（校验未通过仍落库但标 `quality_downgraded`，保留"当时实际值"）；非 Tier-1 但涉财务/合规指标由 Owner 手动开启快照。
    - **快照粒度**：默认全维度组合落库；维度基数过大（如 UV 按 UID 维度）时仅存核心维度组合（Owner 配置，默认为指标默认下钻维度集 + 时间维度）。
    - **存储策略**：热存 180 天（MySQL 分区表按月分区），之后自动冷归档至对象存储，归档后查询走异步导出（T+1）；冷归档策略与 4.10 审计归档一致。
    - **与 /query 的关系（R4-03）**：快照接口为独立端点 `GET /metrics/{code}/snapshots`，不复用 `/query + as_of_date`——快照为 WORM 存证，/query 为即时计算，两者存储路径与权限模型不同；消费方需"某版本口径下的历史值"而非"存证值"应走 /query + `metric_version` 参数。
8. **口径版本消费方确认回调（L3，PRD 5.5.1）**：收到 `metric.pending_version` 通知的消费方经 `POST /versions/{id}/confirm|reject` 回执（14 天超时默认接受，一次延期 +7 天）；confirm 后 semantic 将 `PENDING_VERSION` 升为 `CURRENT` 并触发物化重建；reject 带理由驳回变更。
9. **收藏/最近浏览（PRD 4.5，用户侧本地数据）**：读写入 `user_preference`（`pinned_metrics` 收藏 / `recent_metrics` 最近浏览）——收藏与最近浏览仅用户侧视图，不影响指标治理；收藏指标发生破坏性变更/废弃时随 `metric.pending_version` 事件进入"关注"通知（与 §12.3 watch 语义一致）。最近浏览由前端在指标详情点击时记录（限 20 条，LRU 淘汰）。

**接口调用链**：
- 入：§3.6 全部端点
- 出：→ governance(鉴权/token) → semantic(口径 AST+翻译层) → Redis(缓存) → OLAP/Cube(下推) → lineage/observability/recommend(日志) → conflict(运行时冲突)

**数据流转**：
```
调用方 --POST /query--> consume
  --> governance(权限/token校验) --> semantic(口径AST + 方言翻译)
  --> Redis(缓存命中?) --miss--> OLAP/Cube(下推) --> Redis(写缓存,键含版本)
  --> 返回(带meta) + 事件consume.queried --> lineage(CONSUMED_BY) / observability / recommend / conflict(运行时冲突)
```

**关键算法/容错**：
- 下推优先，平台不落地源表（§4.11.11 边界）。
- 降级矩阵（§5.2）：OLAP✗→503 / ES✗→MySQL LIKE / Cube✗→回退自研。
- 舱壁：AI 流量独立 OLAP 实例/队列（§5.3）。
- 缓存一致性：口径版本变更主动失效旧键（§12.0.2）。
- **消费口径统一入审计（R11-01/R11-02）**：三条消费管线——Semantic API（`POST /query`）、QuickBI 嵌入（`POST /embed/quickbi`）、MCP 工具调用（`/mcp/tools/call`）——统一写入 `operation_audit`（`channel=api|quickbi|mcp`），确保全渠道消费可溯、可计量、可限流。

---

### 12.7 ai（AI 适配服务，FR-14/15，四期）

**职责边界**：NL2SQL 管线 + DataAgent MCP 工具集；**仅调用 LLM 网关，不重造 LLM**；结果须经语义锚定。

**核心流程（NL2SQL）**
1. `POST /nl2sql`：自然语言 → LLM 网关：
   - **语义锚定（D4 前置）**：先经 `semantic` 将自然语言映射为 `metric_code` + 合法 `dimensions` + `dateRange`（而非直接生成裸 SQL），降幻觉。
   - **生成查询意图**：输出结构化查询意图（metric_code/维度/筛选/粒度），**不直接产出可执行 SQL**。
   - **复用 consume 执行（D4 关键边界）**：ai 将结构化意图转调 `consume.POST /query`（走完整鉴权/口径 AST/方言翻译/缓存/下推链路），**不另起执行引擎**；保证 AI 与人工消费同源同口径。
2. 不自动发布任何口径；仅辅助查数。
3. **DataAgent MCP**：`/mcp/tools/list` + `/mcp/tools/call`，暴露 `list_metrics / query_metric / get_lineage / get_conflict` 等工具；其中 `query_metric` 内部同样转调 `consume.POST /query`，受 `X-Api-Key` 限流/降级，AI 流量走独立 OLAP 队列（§5.3）。
4. 流量隔离：AI 查询走独立 OLAP 队列，不打挂主查询（§5.3）。

**接口调用链**：
- 入：§3.7 端点
- 出：→ LLM 网关 → semantic(锚定) → consume(执行)

**数据流转**：
```
用户/Agent --NL2SQL/MCP--> ai --LLM网关--> semantic(锚定metric_code) --|> consume(下推)
```

**关键算法/容错**：
- LLM 不可用 → `503 AI_UNAVAILABLE`，前端走三态降级（取消 AI 预填）。
- 结果仅草稿/辅助，不落 PUBLISHED（防幻觉污染真相源）。

---

### 12.8 quality（质量服务，FR-10）

**职责边界**：指标/数据异常检测与告警，不处理业务语义；质量规则按 tier/dw_layer 差异化配置（PRD 4.8）。

**核心流程**
1. **规则配置**：`quality_rule` 表（COMPLETENESS/ACCURACY/TIMELINESS/CONSISTENCY/UNIQUENESS/VALIDITY/WAVE_DIFF/CROSS_SOURCE），随指标 PUBLISHED 注册；规则模式 `static`/`dynamic_baseline`/`yoy_woy`/`cross_source`（动态基线基于历史同期 + 季节因子，显著降固定阈值误报）。
2. **检测调度**：随 4.2 采集/产出分区就绪自动触发（按分区最小单位），写 `quality_event`（OPEN→ACK→RESOLVED→CLOSED）；异常分级（P0/P1/P2）→ `notify.alert`。
3. **SLA 例外日历（PRD 4.5）**：`quality_rule` 中 TIMELINESS 类检测时先查 `sla_calendar_exception`——当日为节假日/大促/维护窗口且命中 → SLA 放宽（`sla_offset_minutes` 或跳过当天判定），**不触发"数据延迟"误报**；补跑期同样豁免。
4. **外部基准对账（PRD 4.8.8，P1）**：
   - `POST /benchmarks/import`：导入外部权威值（银行对账单/审计数，Excel/CSV/API），幂等（`source_id+metric_code+bench_date+dims` 唯一键），复用批量原子性边界（按批独立事务 + 部分失败报告）。
   - `POST /benchmarks/{id}/bind`：绑定目标指标，声明比对口径（同维度/时间粒度/币种对齐，呼应 metric.currency）。
   - 调度比对：拉指标值（consume 即时查询/物化表）vs `external_benchmark`，差异超阈值 → 写 `reconciliation_record(ALERT)` + `409 RECONCILIATION_ALERT` + notify 双方（指标 Owner + 基准提供方）。
   - **闭环**：Owner `POST /reconciliation-records/{id}/confirm` 确认"差异合理"（口径边界）或"口径有误"（引导走 semantic 变更评审）；结论留审计，`GET /reconciliation-records` 支持导出对账报告（受 governance 权限 + 行数上限 + 脱敏）。
5. **血缘传导 + 根因下钻**：上游表/分区异常经 lineage 标红下游指标；支持沿血缘反查首个异常上游节点（缩短 MTTR）。

**接口调用链**：
- 入：定时任务（内部） + `POST /benchmarks/*` + `POST /reconciliation-records/*`
- 出：→ notify.alert → observability(度量) → semantic(口径有误引导变更) → conflict(同源/基准差异候选)

**数据流转**：`quality(规则跑数) --> 异常 --> notify + observability`
`external_benchmark --导入绑定--> quality(比对) --> reconciliation_record(ALERT) --> notify + observability`

**关键算法/容错**：告警去重（同指标同类型 5min 内合并），避免告警风暴；PII 字段质量豁免（不跑抽样检测）；动态基线冷启动退化为静态阈值；分级引擎降级标 UNKNOWN 不阻断（§5.5）。

---

### 12.9 notify（通知服务，FR-16/17）

**职责边界**：统一通知中心 + 用户行为埋点（反哺 recommend）。

**核心流程**
1. 消费事件总线（§12.0.1）事件：`todo.created`/`quality.anomaly`/`conflict.opened`/`metric.pii_review`/`term.updated`/`classification.done`：
   - 站内：`notification`(inapp) 写入，前端轮询/WS 拉取（标记 `read_at`）。
   - 外部：邮件（SMTP）/Webhook（按用户订阅 channel）；Webhook 带 HMAC 签名 + 重试（失败入 DLQ）。
   - **幂等**：以 `event + ref_id` 去重，重复事件安全忽略。
2. **埋点（PRD 4.14.5/4.14.6，产品行为采集与价值证伪）**：
   - **埋点事件枚举（围绕 1.4 北极星）**：
     | 事件 | 字段 | 用途 |
     |------|------|------|
     | `metric.search` | `query`/`result_count`/`clicked_code`/`is_no_result` | 搜索效率分析 |
     | `metric.detail_view` | `metric_code`/`duration_ms`/`active_tab` | 口径确认耗时 |
     | `metric.register_start` | `source`(manual/etl/template) | 注册转化漏斗 |
     | `metric.register_submit` | `metric_code`/`llm_adopted_fields`/`duration_ms` | LLM 采纳率 |
     | `quality.alert_received` | `metric_code`/`severity`/`action_taken` | 告警触达→处理闭环 |
     | `ai.assist_used` | `metric_code`/`field`/`adopted`(bool) | AI 辅助采纳率 |
     | `api.call` | `client_id`/`endpoint`/`status_code`/`duration_ms` | API 鉴权成功率/限流率 |
     | `conflict.participated` | `conflict_id`/`role`/`decision` | 冲突仲裁参与 |
     | `term.search` | `query`/`result_count` | 术语检索活跃度 |
   - **埋点字段规范**：`uid`（脱敏ID）/ `role` / `metric_code` / `duration_ms` / `trace_id` / `channel` / `ts`，与 4.10 审计字段对齐便于关联。
   - **采集管线**：前端 SDK 批量上报 + 后端日志双路，异步批量入 OLAP 分区表；失败不阻断主流程（埋点丢失降级告警，不阻塞业务）。
   - **隐私边界**：埋点不含 PII 明文（用户标识走脱敏 id，呼应 4.9/4.10 被遗忘权），PII 维度值不进埋点。
   - **埋点反哺**：① `metric.search`+`metric.detail_view` → 计算确认口径耗时（1.4 北极星）；② `metric.register_start`+`metric.register_submit` → 首注时长 + LLM 采纳率；③ `api.call` → API 鉴权成功率；④ 高频搜索/停留行为 → recommend 协同信号。
3. **订阅中心**：用户可配置 channel 偏好（inapp/email/webhook）+ 免打扰（避免告警风暴，呼应 quality 去重）。
4. **指标结果主动投递（PRD 4.5 `metric_delivery`）**：① 定时投递——按 `schedule_cron`（每日/每周）拉取指标值（转调 consume `/query`）推送到钉钉/邮件/Webhook；② 阈值触发——指标值突破 `trigger_rule` 即时推送。投递配置与 governance 对齐（`receiver_scope` 接收方须有该指标可见权限），投递结果入 observability 度量。
5. **投递逻辑与防骚扰（PRD 4.14.2）**：
   - **同根因聚合**：同源多事件（如一张上游表缺失引发下游 N 个指标质量告警）归并单条摘要，附受影响清单，防告警风暴。
   - **沉默期与分级**：P0/P1 实时触达；P2/P3 进沉默期（工作时间外或非紧急）汇总为每日摘要，降打扰。
   - **渠道降级**：IM/邮件/短信网关不可达 → 自动降级为站内信（保证"至少站内可达"），渠道健康度纳入 §5.2a 降级监测。
6. **已读回执与升级（PRD 4.14.4）**：通知带 `read` 状态；关键待办类（审核/冲突裁决/权限申请）未读超 `SLA_UNREAD`（默认 24h）自动升级——通知替补 Owner 或上级；升级动作本身入 4.10 审计（防"升而不理"）。

**接口调用链**：
- 入：Redis Stream（事件总线，消费者组）
- 出：→ MySQL.notification / event_log → 前端 / recommend

**数据流转**：`各服务 --XADD事件--> Redis Stream --> notify(消费组) --写--> notification/event_log --> 前端/推荐`

**关键算法/容错**：
- Webhook 投递失败指数退避，超限入 Dead Letter 人工查。
- 事件总线不可用 → 生产者落本地队列补发（§12.0.1），notify 不阻塞主链路。

---

### 12.10 observability（审计/运营，FR-16）

**职责边界**：审计留痕 + 运营度量 + NPS/反馈；只读聚合，不修改业务数据。

**核心流程**
1. **审计**：网关中间件对 all 写操作入 `audit_log`（actor/action/entity/detail/ip），不可删。
2. **运营度量**：从 `event_log`/查询日志聚合 DAU/搜索量/审核时效/降级率/预聚合命中率 → 运营看板。
3. **反馈**：`POST /feedback`（NPS/建议）→ 状态可见（已受理/采纳/拒，OP-04）。

**接口调用链**：
- 入：网关自动 + §3.8 端点
- 出：只读查询（看板/审计检索）

**数据流转**：`网关/各服务 --> audit_log/event_log --> observability(聚合) --> 看板`

---

### 12.11 assetmap（资产地图，FR-18）

**职责边界**：元数据"视图"非数据源；聚合渲染，不缓存生产数据。

**核心流程**
1. 监听事件总线（§12.0.1）：`catalog.updated`+`metric.published`+`classification.done` → 增量聚合资产总览（域/分级/覆盖率），不整表重建。
2. `GET /asset-map/overview`：按域/分级聚合 + 覆盖率看板（呼应北极星 cold-start 基线）。
3. `GET /asset-map/heatmap`：敏感分布热力（按 `classification.sensitivity_level` 着色）→ 驱动合规整改（触发 `governance.POST /classification/rescan`）。
4. `GET /asset-map/owner/{uid}`：责任人视图（名下资产+健康度，复用 metric 驾驶舱）。
5. `GET /asset-map/{entity}`：表/字段详情（含血缘入口、PII 标记、责任人）。

**接口调用链**：
- 入：§3.3 端点 + 事件总线（catalog/metric/classification）
- 出：读 MySQL/Neo4j/ES 聚合

**数据流转**：`db_catalog/metric/classification --事件--> assetmap(增量聚合) --> 前端(总览/热力/责任人/详情)`

**容错**：上游 stale 资产标灰，不整页崩；分级引擎降级时热力退化为"未分级"灰区（§5.5）。

---

### 12.12 recommend（智能找数推荐，FR-19，二期）

**职责边界**：辅助入口非结论；不基于 PII 字段个性化（仅用口径/术语/血缘信号）。

**核心流程**
1. 协同信号加权召回：`event_log`（共查频次 w1）+ `term_idx`（术语共现 w2）+ `lineage`（血缘邻近 w3）→ 候选打分排序。
2. `GET /metrics/{code}` 详情页："相关指标"（同术语/同上游/同域高频），点击回流 `event_log` 强化。
3. 首页"为你推荐"：基于角色+近期行为个性化（新人→术语库关联优先；分析师→近期高频优先）。
4. 自我强化：`term.updated` 事件刷新术语共现矩阵；冷启动无行为 → 退化为"热门口径"榜单（不空窗，§5.2）。

**接口调用链**：
- 入：§3 相关端点 + notify.event_log + glossary.term_idx
- 出：读 ES/event_log/lineage 聚合 → 前端

**数据流转**：`event_log + term_idx + lineage --> recommend(加权召回) --> 前端(相关/推荐)`

**容错**：推荐不参与质量评分；结果以指标详情口径为准（不替代真相源）；服务不可用退化为热门口径（§5.2）。

---

### 12.13 关键跨服务一致性机制

| 机制 | 实现 |
|------|------|
| 双写最终一致 | 业务态 MySQL 为源 → Redis 事件 → 异步 Neo4j/ES/Cube；重试队列 + 定期对账补偿（§5.1） |
| 状态机权威 | semantic 为唯一口径真相源，其他服务只读/引用，不反向写口径 |
| 鉴权统一 | 所有消费走 governance.permissions；越权 403+审计 |
| 降级舱壁 | 单能力故障仅削弱该能力，相邻不受影响（§5.2） |
| 审计全覆盖 | 网关中间件对所有写操作留痕，不可删 |
| 事件总线契约 | 服务间异步事件统一信封 + 至少一次投递 + 消费幂等，杜绝级联故障（§12.0.1） |
| 缓存/Cube 一致性 | 缓存键含 `metric_version`，口径变更主动失效；Cube 重建期路由回自研（§12.0.2） |
| PII 冲突路由 | PII 口径在无权域被引用 → 转交 governance.pii_review（403 FORBIDDEN_PII），不进普通仲裁闭环（C4） |
| Cube 同步 | PUBLISHED→cube 定义生成（二期），失败回退自研引擎 |

---

### 12.14 glossary（术语库服务，FR-08，L1 补齐）

**职责边界**：业务概念"标准名与定义"治理层（管名不管口径）；与指标 `synonyms` 分层——术语库管标准词，指标 `synonyms` 仅承载口语别名。是中文术语治理差异化核心，也是 LLM 解析 few-shot 约束语料源。

**核心流程**
1. **创建/编辑**：`POST /terms`（DRAFT）→ Steward `submit`→REVIEW→`approve`→APPROVED（状态机见 PRD 4.6.2）。写 `glossary_term` + `term_version`（每次变更留版本快照）。
2. **同义词归一**：`aliases` 经小写/繁简/同义词扩展归一，归入 `synonym_group`；新建术语命中既有别名或指标 `synonyms` → 生成 `glossary_conflict`（OPEN），由管理员裁决（不与指标发布强耦合）。
3. **引用不拷贝**：指标注册时"引用 `term_id`"而非内嵌定义；术语修订不要求改写指标，仅发 `term.updated` 事件通知引用方 Owner（防口径理解偏差），不阻断发布。
4. **LLM 联动（4.6.4）**：`collector` 采集时拉取 `glossary_term` 标准词+别名作 few-shot 约束；LLM 解析出口语词经术语库反查标准词再生成 `metric_code`，降"同义不同名"重复建设。
5. **检索**：先术语概念对齐（标准概念→关联指标列表），再用 `synonym_group` 扩展命中 `term_idx`（ES 中文分词）；结果卡展示"标准概念→关联指标"层级。
6. **能力边界**：不强制指标计算逻辑一致（口径一致由 semantic/conflict 保障）；别名归并依赖人工裁决；不提供翻译服务；术语缺失/冲突不阻断指标发布（仅提示）。

**接口调用链**：
- 入：§3 相关端点（术语 CRUD/检索） + collector（上下文供给） + semantic（引用/反查）
- 出：→ notify(`term.updated` 通知引用方) → collector(LLM few-shot) → recommend(术语共现矩阵)

**数据流转**：
```
用户/Steward --CRUD--> glossary --状态机--> MySQL.glossary_term/term_version
glossary --同义词归一--> glossary_conflict(裁决)
glossary --term.updated事件--> notify(引用方) + collector(LLM上下文) + recommend(共现)
指标 --引用term_id--> glossary(不拷贝)
```

**关键算法/容错**：
- 废弃须填替代术语，避免引用悬空（DEPRECATED 后新指标不可引用，旧引用保留可追溯）。
- 孤儿术语/低检索术语定期清理建议入 observability 看板。

**帮助中心内容供给（PRD 4.5）**：`GET /help` 返回操作指引（静态 Markdown，版本化）+ 术语概念卡关联——每篇指引可内嵌 `glossary_term` 概念卡（"什么是派生指标""口径变更如何影响下游"），新用户边用边学；内容变更走术语审核流（DRAFT→REVIEW→APPROVED），与术语库同源治理。

---

### 12.15 dimension & reconciliation（维度映射与同源对账服务，L2 补齐）

**职责边界**：维护"共享维度（conformed dimension）"映射与跨域指标对齐底座；定时跑同源口径对账（`reconciliation`）。归 governance 域编排，但逻辑独立成节。**外部基准对账（`external_benchmark`/`reconciliation_record`，PRD 4.8.8）归属 quality 服务（§12.8）**，此处仅保留同源口径对账（平台内部多指标一致性校验）。

**核心流程**
1. **共享维度定义**：`POST /dimensions` 建 `dimension`（标准名/枚举/域/owner）；指标注册时关联 `metric_dimension`（关联标准维度而非源字段）。
2. **维度映射**：`POST /dimension-mappings` 录 `dimension_mapping`（源表/源列 → 标准维度值规则，如省→大区上卷、缩写→标准）；`dws_sales_di.region`/`dwd_order_df.province`/`ads_traffic_daily.area` 虽名不同但均映射到同一 `地区` 维度，使多表指标同维度可对齐、可跨域汇总。
3. **维度值 RLS**：标准维度值（如"渠道"枚举）也引用术语（4.6.8），降维度值歧义；行级权限按维度值约束（二期深化）。
4. **同源口径对账（4.5）**：定时任务对 `reconciliation`（同源事实被多指标引用）跑一致性校验——同事实不同口径指标结果差异超 `threshold`（默认 1%）→ 标 `WARN/ALERT` + 触发 `conflict` 候选（防"同表出俩数"）。
5. **消费校验支撑**：`consume.POST /query` 校验 `dimensions` 合法性时反查 `metric_dimension`+`dimension`，非法维度拒单（422）。

**接口调用链**：
- 入：§3 相关端点 + semantic(指标注册关联) + consume(维度校验)
- 出：→ conflict(同源对账候选) → consume(维度合法性依据) → semantic(跨域对齐)

**数据流转**：
```
Steward --维度/映射CRUD--> dimension/dimension_mapping/metric_dimension(MySQL)
定时任务 --同源校验--> reconciliation(差异超阈) --> conflict(候选) + notify(告警)
consume --维度校验--> metric_dimension(反查)
semantic --跨域指标--> 共享dimension(对齐汇总)
```

**关键算法/容错**：
- 映射规则失败（源值无对应标准值）→ 标 `UNMAPPED` 提示 Steward 补规则，不静默归并。
- 跨域对齐以"标准维度"为准，源字段名差异不阻断（呼应 PRD 4.5 字段名不同≠维度不同）。

---

## 13. 平台配置参数清单（对齐 PRD 附录 B）

所有参数变更入 4.10 审计（R9-07）。平台级由 `platform_admin` 变更；域级由 `domain_admin` 变更；合规约束类参数变更须经 `compliance_officer` 复核。变更前可 what-if 预览影响范围。

| 参数 | 默认值 | 可调范围 | 影响模块 | 作用域 |
|------|--------|----------|----------|--------|
| **指标生命周期** | | | | |
| `metric_version.sunset_days` | 30 | 7–90 | semantic 口径版本钉住 Sunset（4.5） | 域级配置 |
| `metric.recycle_bin_days` | 30 | 7–90 | semantic 回收站保留期（4.5） | 域级配置 |
| `metric.deprecate_notice_days` | 30 | 7–180 | semantic 退役通知期（4.5） | 域级配置 |
| `metric.emergency_review_deadline_h` | 24 | 4–72 | semantic 紧急发布补审时限（4.5） | 平台级 |
| `metric.batch_register_limit` | 20 | 5–50 | semantic 批量注册上限（4.5） | 平台级 |
| **质量监控** | | | | |
| `quality.dynamic_baseline.window_days` | 28 | 14–90 | quality 动态基线历史窗口（4.8） | 指标级覆盖 |
| `quality.dynamic_baseline.sigma` | 3 | 1–5 | quality 动态基线 σ 倍数（4.8） | 指标级覆盖 |
| `drift_detection.schedule_days` | 7 | 1–30 | conflict 口径漂移巡检周期（4.7.8） | 域级配置 |
| **权限与合规** | | | | |
| `grant.temp_ttl_days` | 7 | 1–30 | governance 临时授权有效期（4.9） | 域级配置 |
| `grant.review_cycle_days` | 90 | 30–365 | governance 长期权限复审周期（4.9） | 平台级 |
| `pii.identification.regex_list` | id_card/phone/email/name | — | governance PII 正则匹配模式（4.15.3） | 平台级 |
| `pii.rescan_interval_days` | 7 | 1–30 | governance PII 字段定期重扫（R12-09/R12-17） | 平台级 |
| **审计与留存** | | | | |
| `audit.hot_retention_days` | 180 | 90–365 | observability 审计热存天数（4.10，合规约束） | 平台级 |
| `audit.cold_archive_enabled` | true | — | observability 冷归档开关（4.10） | 平台级 |
| `snapshot.hot_retention_days` | 180 | 90–365 | consume 快照热存天数（4.5，合规约束） | 平台级 |
| `lineage.edge_history_retention_days` | 180 | 90–365 | lineage 边变更快照保留（R10-04/R11-16） | 平台级 |
| **消费层** | | | | |
| `api.client_qps_limit` | 20 | 5–200 | consume 限流（4.11.9） | 客户端级 |
| `api.client_daily_limit` | 100000 | 10000–∞ | consume 日调用上限（4.11.9） | 客户端级 |
| `api.query_timeout_seconds` | 30 | 5–120 | consume 下推超时（4.11.4） | DataSource 级 |
| `cache.ttl_default_hours` | 24 | 1–168 | consume 缓存默认 TTL（4.11.5） | 指标级覆盖 |
| `search.page_size_default` | 20 | 10–100 | consume/ES 分页默认（4.11.2） | 平台级 |
| `search.page_size_max` | 100 | 50–500 | consume/ES 分页上限（4.11.2） | 平台级 |
| **成本与额度** | | | | |
| `api.free_quota_monthly` | 10000 | 0–∞ | consume API 免费额度/月/client（R10-01/R11-15） | 客户端级 |
| `llm.free_quota_monthly` | 1000 | 0–∞ | LLM 解析免费额度/月/域（R10-01/R11-15） | 域级配置 |
| `cost.budget_alert_threshold_pct` | 80 | 50–95 | ops_cost 预算预警阈值%（R10-01/R11-17） | 域级/客户端级 |
| `cost.budget_hard_limit_pct` | 100 | 80–200 | ops_cost 预算自动限流阈值%（R10-01/R11-17） | 域级/客户端级 |
| `export.row_limit` | 100000 | 10000–1000000 | consume 导出行数上限（4.11.12） | 平台级 |
| **备份与运维** | | | | |
| `backup.mysql_full_daily` | true | — | MySQL 每日全量备份（6.7） | 平台级 |
| `backup.mysql_binlog_enabled` | true | — | MySQL binlog 归档（6.7） | 平台级 |
| `backup.neo4j_dump_daily` | true | — | Neo4j 每日 dump（6.7） | 平台级 |
| `backup.retention_days` | 7(MySQL)/3(Neo4j/ES) | 1–30 | 备份保留天数（6.7） | 平台级 |
| `consistency.check_daily` | true | — | 每日对账开关（R8-01） | 平台级 |
| `consistency.alert_threshold_pct` | 0.1 | 0.01–1.0 | 对账差异告警阈值%（R8-01） | 平台级 |
| **降级与韧性** | | | | |
| `circuit_breaker.error_rate_pct` | 50 | 10–80 | 熔断错误率阈值（4.13.2） | 依赖级 |
| `circuit_breaker.half_open_timeout_s` | 30 | 10–120 | 半开探测间隔（4.13.2） | 依赖级 |
| `sync.queue_backlog_alert` | 10000 | 1000–100000 | 同步器积压告警阈值（R8-03） | 平台级 |
| `sync.queue_backlog_timeout_min` | 30 | 5–120 | 积压超时告警（R8-03） | 平台级 |

---

## 14. 部署架构（PRD 6.7/R8-01）

### 14.1 双写一致性模型

MySQL 为权威源（Authority），Neo4j/ES 为异步衍生副本。写操作路径：

```
API → MySQL(同步写) → Redis Stream(XADD) → [Neo4j Sync Worker / ES Sync Worker](异步消费)
```

- **一致性级别**：最终一致（Eventual Consistency），延迟 ≤ 5s（P99）；写 MySQL 成功即返回，衍生副本异步更新
- **写失败补偿**：Redis Stream 写入失败 → 生产者落本地磁盘队列，后台 Worker 补发；衍生副本消费失败 → 重试 3 次后入 Dead Letter Stream，告警 + 人工/定时重试
- **读侧补偿**：读 Neo4j/ES 时若数据缺失（`stale` 标记或版本号不一致），API 层主动读 MySQL 回填（§5.1 stale 读策略）

### 14.2 每日对账修复（R8-01）

- **触发**：`consistency.check_daily=true` 时每日 03:00 对账任务
- **对账逻辑**：
  1. 扫描 MySQL 全量 `metric`/`metric_version`，提取 `{id, version, updated_at}`
  2. 批量比对 Neo4j/ES 对应节点，不一致则标 `stale` + 触发补写
  3. 血缘边：MySQL `lineage_edge` vs Neo4j `DERIVED_FROM` 边，不一致补写
- **差异告警**：差异率 > `consistency.alert_threshold_pct`(默认 0.1%) → 发 `consistency.alert` 事件入 notify
- **修复策略**：以 MySQL 为准单向修复（MySQL→Neo4j/ES），不反向；修复操作入审计

### 14.3 高可用规格

| 组件 | 部署模式 | 最小实例数 | 故障切换 | RPO | RTO |
|------|----------|-----------|----------|-----|-----|
| MySQL | 主从半同步复制 | 1 主 + 2 从 | VIP 漂移 + 半同步从自动提升 | 0（binlog 无损） | ≤ 30s |
| Redis | Sentinel 哨兵 | 3 节点（1 主 2 从 + 3 Sentinel） | Sentinel 自动故障转移 | ≤ 1s | ≤ 15s |
| Neo4j | 因果集群 | 1 Leader + 2 Follower | 自动 Leader 选举 | ≤ 1s | ≤ 30s |
| ES | 3 节点集群 | 3（各含 Master+Data） | 分片自动重分配 | ≤ 5s | ≤ 30s |
| API 服务 | K8s Deployment | 2 Pods（跨节点反亲和） | K8s 自动重建 + 就绪探针 | 0（无状态） | ≤ 10s |
| Sync Worker | K8s Deployment | 2 Pods | 消费者组自动 Rebalance | 0（Stream 可重放） | ≤ 15s |
| OLAP 引擎 | 独立集群 | 3 FE + 3 BE | FE 选举 + BE 副本 | ≤ 5min | ≤ 5min |

### 14.4 资源表（最小生产规格）

| 组件 | 规格 | 数量 | 存储 | 扩容触发 |
|------|------|------|------|----------|
| MySQL | 8C/32G/SSD | 3 | 500GB | 连接数>80% / 慢查询>P5 / 磁盘>70% |
| Redis | 8C/16G | 6 (3 Sentinel) | 32GB 内存 | 内存>80% / 延迟>P99>50ms |
| Neo4j | 8C/32G/SSD | 3 | 200GB | 查询P99>3s / 磁盘>70% |
| ES | 8C/16G/SSD | 3 | 300GB | 搜索P95>500ms / 磁盘>70% |
| API Pod | 4C/8G | 2+ | — | CPU>70% / QPS>限流80% |
| Sync Worker | 4C/8G | 2+ | — | 积压>backlog_alert阈值 |
| OLAP | 8C/32G | 3 FE+3 BE | 1TB | 查询P95>10s / 并发>80% |

**扩容增长预估公式**：
- 指标数增长 10× → MySQL 存储 ×5（版本历史非等比）+ Neo4j 节点 ×10 + ES 索引 ×3（搜索优化压缩）
- DAU 增长 5× → API Pod ×3（缓存命中率提升抵消部分）+ Redis 内存 ×4
- 消费量增长 10× → OLAP BE ×3 + Sync Worker ×2

---

## 15. 安全合规（PRD 4.9/4.15/6.6）

### 15.1 传输安全
- 所有外部流量 TLS 1.3 强制（API 网关层终止 TLS，内部服务间 mTLS）
- WebSocket 升级走 WSS（禁止 WS 明文）
- 内部服务间调用：Service Mesh mTLS 或 Redis Stream 加密传输

### 15.2 存储安全
- PII 明文仅存 MySQL（加密表空间 `encryption='Y'`），Neo4j/ES 仅存 PII 标记（`pii_flag`/`sensitivity_level`），不存 PII 值
- 密钥管理：JWT 签名密钥 / HMAC 密钥 / 加密密钥 → KMS 托管，轮转周期 90 天
- 备份加密：MySQL 全量备份 + binlog 归档 → AES-256 加密存储

### 15.3 访问控制
- 外部 API：JWT Bearer Token（短期 access_token 15min + refresh_token 7天）或 api_client secret 签发 token
- 内部服务：Service Account + RBAC，服务间调用鉴权 token 透传 `trace_id`/`actor_id`
- 数据库：应用账号按最小权限（MySQL 只读/读写分离；Neo4j 读副本/写 Leader 分离）
- 运维：跳板机 + 审计，禁止直连数据库

### 15.4 审计与合规
- 全写操作审计（§12.10 observability）：actor/action/entity/detail/ip/trace_id，不可删除
- 合规约束参数变更须 `compliance_officer` 复核（§13 配置变更原则）
- PII 指标访问审计独立标记（`pii_access=true`），按 PIPL 要求保留 ≥ 3 年
- 被遗忘权执行审计（A6）：记录 anonymize 操作时间/范围/SHA256 前缀

### 15.5 注入防护
- NL2SQL（四期 AI 服务）：LLM 输出 SQL 沙箱校验——禁止 DDL/DML（仅 SELECT）、禁止 `INTO OUTFILE`/`LOAD DATA`、查询深度 ≤ 3 层子查询、扫描行数上限强制
- 口径表达式沙箱：AST 校验防注入/无限递归依赖
- API 参数：统一入参校验中间件（防 SQL 注入/XSS/路径穿越），`INJECTION_DETECTED` 返回 403

---

## 16. 可观测性（PRD 4.14/4.13/6.7）

### 16.1 指标（Metrics）
- **业务指标**（北极星）：口径确认耗时 / 首注时长 / LLM 采纳率 / API 鉴权成功率 / 告警触达→处理闭环率 / 降级频次·时长·影响用户数
- **技术指标**：各服务 P50/P95/P99 延迟 / QPS / 错误率 / Redis 命中率 / Neo4j 查询耗时 / ES 搜索延迟 / OLAP 查询耗时
- **资源指标**：CPU / 内存 / 磁盘 / 连接池 / 队列深度
- 采集：Prometheus + Grafana 仪表盘；告警规则按 §5.2a 降级阈值配置

### 16.2 日志（Logging）
- 统一结构化日志格式：`{ts, level, service, trace_id, span_id, msg, ctx_json}`
- 日志级别：ERROR（必告警）/ WARN（降级/重试）/ INFO（审计/关键操作）/ DEBUG（开发调试，生产默认关）
- 审计日志独立通道（§12.10），不走通用日志管道
- 日志保留：热存 30 天 / 冷归档 180 天（与 `audit.hot_retention_days` 对齐）

### 16.3 分布式追踪（Tracing）
- `trace_id` 全链路透传：API 网关生成 → 各服务 → Redis Stream → 消费者组 → 下游服务
- OpenTelemetry SDK 集成；Span 覆盖：API 入口 / MySQL 写 / Neo4j 写 / ES 写 / OLAP 查询 / LLM 调用
- 采样率：生产 1%（P99 慢查询 100% 采样）；调试模式 100%

### 16.4 降级告警
- 告警分级：P0（平台不可用/数据丢失）/ P1（单域降级/PII 泄露风险）/ P2（质量异常/延迟）/ P3（容量预警）
- 告警去重：5min 内同 `metric_code+severity` 仅首次告警，避免风暴
- 告警渠道：按 §12.9 订阅偏好分发（站内 + 邮件 + Webhook + 钉钉）
- 告警恢复：自动恢复时发 `degradation.state_changed(HA→HEALTHY)` 通知

### 16.5 灰度上线与回滚
- **灰度策略**：新版本按 `domain` → `tenant` → `全量` 逐步放量；灰度期间双版本并行（金丝雀发布）
- **回滚**：K8s Deployment `rollback` + MySQL migration `down` 脚本；口径版本回滚走 `metric_version` 降级（§12.3 状态机）
- **回滚判定**：错误率 > 5%（5min 窗口）自动回滚；P0 事故人工决策回滚

### 16.6 备份恢复与 RTO/RPO
| 场景 | RPO | RTO | 恢复方式 |
|------|-----|-----|----------|
| MySQL 主库故障 | 0（半同步） | ≤ 30s | 从库提升 |
| MySQL 误删数据 | ≤ 5min（binlog） | ≤ 1h | binlog 闪回 + 全量恢复 |
| Neo4j Leader 故障 | ≤ 1s | ≤ 30s | Follower 选举 |
| Redis 主故障 | ≤ 1s | ≤ 15s | Sentinel 故障转移 |
| ES 节点故障 | ≤ 5s | ≤ 30s | 分片重分配 |
| 全机房故障 | ≤ 24h（异地冷备） | ≤ 4h | 异地备份恢复 |

---

## 17. ER 整合图（Mermaid）

> 命名对齐规则：ER 实体名 = DDL 表名（唯一权威源），消除历史不一致（E9-E21 修复）。

```mermaid
erDiagram
    %% ===== 用户与域 =====
    user ||--o{ grants : receives
    user ||--o{ subscription_pref : prefers
    user ||--o{ user_preference : customizes
    user ||--o{ feedback : submits
    user ||--o{ operation_audit : triggers
    user ||--o{ event_log : records
    role ||--o{ grants : authorizes
    policy ||--o{ grants : governed_by

    %% ===== 指标核心 =====
    metric ||--o{ metric_version : versions
    metric ||--o{ metric_value_snapshot : snapshots
    metric ||--o{ metric_set_item : included_in
    metric ||--o{ metric_tree : navigates
    metric ||--o{ metric_dimension : cuts_by
    metric ||--o{ metric_business_relation : relates_to
    metric ||--o{ metric_code_alias : renamed_from
    metric ||--o{ metric_delivery : delivers_to
    metric ||--o{ quality_rule : governed_by
    metric ||--o{ quality_event : alerts
    metric ||--o{ data_classification : classified_as
    metric ||--o{ compliance_review : reviewed_by
    metric ||--o{ sla_calendar_exception : excepts
    metric }o--|| user : owned_by

    %% ===== 指标集 =====
    metric_set ||--o{ metric_set_item : contains

    %% ===== 指标版本与血缘 =====
    metric_version ||--o{ metric_lineage_source : sourced_from
    metric_version ||--o{ lineage_edge : derives
    metric_version ||--o{ schema_drift_event : affected_by
    metric_version ||--o{ drift_scan_result : scanned_by
    metric_version ||--o{ operation_audit : audited_in

    %% ===== 血缘边 =====
    lineage_edge ||--o{ lineage_edge_history : history

    %% ===== 数据源与采集 =====
    data_source ||--o{ db_catalog : catalogs
    data_source ||--o{ data_set : binds
    data_source ||--o{ collector_job : schedules
    data_source ||--o{ lineage_edge : traces
    data_source ||--o{ quality_rule : monitors
    data_source ||--o{ schema_drift_event : detects
    data_source ||--o{ dependency_health : health_tracked

    %% ===== 逻辑表/物理表 =====
    data_set ||--o{ partition_rewrite_event : rewrites

    %% ===== 物化 =====
    metric_version ||--o{ materialized : materialized_as

    %% ===== 术语 =====
    glossary_term ||--o{ glossary_conflict : conflicts_with
    glossary_term ||--o{ term_version : versions

    %% ===== 冲突 =====
    conflict ||--o{ ruling_record : ruled_by
    conflict }o--|| metric : metric_a
    conflict }o--|| metric : metric_b

    %% ===== 质量 =====
    quality_rule ||--o{ quality_event : triggers
    external_benchmark ||--o{ reconciliation_record : compares
    reconciliation }o--|| metric : checks

    %% ===== 分级分类 =====
    data_classification ||--o{ compliance_review : requires

    %% ===== 依赖健康 =====
    dependency_health ||--o{ degradation_event : records
    dependency_health }o--o{ data_source : tracks

    %% ===== 消费 =====
    api_client ||--o{ operation_audit : calls
    api_client }o--|| ops_cost : charged
    role ||--o{ ops_cost : domain_bears

    %% ===== LLM =====
    llm_model_config ||--o{ llm_test_report : tested_by
    llm_model_config ||--o{ prompt_template : uses
    prompt_template ||--o{ prompt_template_version : versions
    golden_set ||--o{ calibration_result : calibrates

    %% ===== 通知 =====
    notification }o--|| user : targets

    %% ===== 维度 =====
    dimension ||--o{ dimension_member : has_members
    dimension ||--o{ dimension_mapping : mapped_from
```

---

## 18. 测试计划（生产级，对齐 PRD 第 9 章 DoD）

### 18.1 测试分层与覆盖目标

| 层 | 工具 | 覆盖目标 | 重点 |
|----|------|----------|------|
| 单元测试 | pytest / Vitest | 核心算法与门禁逻辑 ≥ 80% | 口径 AST 翻译、冲突相似度、PEP→PDP 判定、脱敏策略、状态机非法跃迁 |
| 集成测试 | pytest + testcontainers（MySQL/Neo4j/ES/Redis 临时实例） | 服务间接口与存储读写 | 双写最终一致、事件总线幂等、缓存版本失效、质量规则触发 |
| E2E 测试 | Playwright（前端）+ httpx（API 全链路） | 核心旅程 100% 可达 | §18.3 四旅程 + 降级场景 |
| 性能测试 | k6 / JMeter | §6 性能目标 | 搜索 P95<200ms、血缘影响面<500ms、下推<3s、审计写入 P99<100ms |
| 安全测试 | OWASP ZAP + 手工 | 0 高危漏洞 | 注入防护（NL2SQL/口径表达式）、越权 403、PII 访问审计、渗透测试 |

### 18.2 测试数据与环境

- **测试环境**：dev（本地容器）/ staging（与生产同构，数据脱敏）/ prod（只读验证）。
- **测试数据**：构造 3 域 × 200 指标（含原子/派生/复合、Tier-1/2/3、批流双路、PII/非 PII）+ 1,000 条 ETL SQL（Parser 可解析 + 动态 SQL 混合）+ golden set 500 条校准样本。
- **质量门槛**：CI 中 lint + 单测 + 集成阻断合并；E2E 每日跑一次。

### 18.3 核心 E2E 旅程（对齐 §12.0.4 四旅程）

| 旅程 | 步骤（UI + API） | 验收点 |
|------|------------------|--------|
| 一·溯源确认 | 搜索指标→详情→版本时间线→反馈纠错 | 口径确认≤10 分钟；版本 diff 可展示 |
| 二·开发注册 | ETL 一键注册→LLM 预填→试算→提交审核→批准 | LLM 采纳字段高亮；试算偏差高亮；PII 门禁拦截 |
| 三·Owner 治理 | 待办中心→审核/裁决/确认→SLA 倒计时 | 待办聚合正确；破坏性变更消费方确认闭环 |
| 四·程序化消费 | api_client 换 token→/query→限流→meta 校验 | 越权 403；429 retry_after；meta 全字段 |
| 五·降级韧性 | 依次断 LLM/Neo4j/ES/OLAP→验证降级 | 单能力故障不阻断相邻；降级文案可读 |

### 18.4 验收标准（对接 §9 + PRD 9.x）

- 单元测试覆盖率 ≥ 80%（核心门禁/翻译/判定模块）；CI 阻断。
- 四类冲突（含 PII 路由 403 FORBIDDEN_PII）触发正确。
- 破坏性变更：消费方 confirm/reject/超时(14d 默认接受) 三路径均验证。
- 灰度 promote/rollback 一键可达，EXPERIMENTAL 仅白名单可见。
- 口径漂移巡检：Tier-1 源头 SQL 变更后 24h 内检出；闭环率 ≥ 90%。
- 外部基准对账：导入幂等；差异确认闭环率 ≥ 90%。
- 双写对账：注入 Neo4j 写入失败，重试队列补偿后三方一致；差异率告警阈值生效。
- 性能达标（§6 全项 P95）+ 安全渗透 0 高危漏洞。
- 所有验收结果留痕（测试报告入库 `audit_log`，可追溯）。
