# Unisense 统一指标语义平台 · 技术设计文档（TD）

> 配套文档：`proposal.md`（PRD）。本 TD 为 PRD 第 2/6 章技术约束的细化落地，作为立项评审后技术设计与编码的直接输入。
> 约束基线：Web 浏览器形态、面向桌面大屏、私有化部署；不重造 LLM / BI / 数据源；平台不持有源数据副本。

---

## 0. 设计总览

| 项 | 决策（最小粒度） |
|----|------|
| 交付形态 | Web 浏览器（React 19 + TypeScript 5 + Ant Design 5），桌面大屏优先（≥1280px 断点），无移动端；构建产物经 Nginx 静态托管 + CDN（私有化内网可省） |
| 前端框架细节 | Vite 5 构建；React Router 6 路由；TanStack Query（缓存/重试/降级态）、Zustand（轻态）；血缘图 Cytoscape.js / AntV G6；ECharts 看板；Axios 拦截统一 `code`/`degraded` |
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
│ +PII→423路由 │          │               │ 同源对账  │                  │
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
      PII口径 approve 前须 governance.pii_review(423路由,非普通仲裁); 变更走 PENDING_VERSION+灰度+回滚
  降级边界(舱壁): LLM✗→取消AI预填 | Neo4j✗→血缘标stale | ES✗→退MySQL LIKE | OLAP✗→503 | Cube✗→回退自研引擎 | 分级引擎✗→标UNKNOWN(§5.5)
  鉴权: 用户态JWT / 消费方X-Api-Key换短效JWT(scope/白名单); 越权→403+审计; PII跨域→423+转交合规
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
| `governance` | FR-11 | RBAC/域授权（grants）、PII 合规门禁（423 路由）、分级分类（落 `classification`）、维度映射与同源对账编排、权限申请/回收/TTL |
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

> 统一约定：所有响应包 `{code, message, data, trace_id, degraded}`；`code` 为业务码（见 §5.4）；`degraded=true` 表示命中降级。
> 统一前缀：`/api/v1`。鉴权：`Authorization: Bearer <JWT>`（用户态）或 `X-Api-Key` + JWT（消费方 `api_client`）。

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
GET/POST /sla-calendar               # SLA 例外日历查询/配置（PRD 4.5，domain_admin 维护）
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
POST   /pii/review                   # 合规官复核（COMP-1，留痕）
POST   /classification/rescan        # 分级重扫（COMP-2）
GET    /me/permissions               # 当前用户权限快照
```

### 3.6 消费（consume · Semantic API）
```
POST   /query                       # 口径查询（metrics/dimensions/filters/dateRange/comparison）
POST   /query/dry-run               # 试算沙箱（不计费/不写生产/不进缓存）
GET    /query/{query_id}/cancel     # 取消在途查询
GET    /metrics/{code}/semantic     # 只读语义拉取（api_client 用，受 scope 约束）
POST   /embed/quickbi               # 获取嵌入令牌（FR-12）
POST   /versions/{id}/confirm       # 破坏性变更消费方确认（PENDING_VERSION → CURRENT，PRD 5.5.1）
POST   /versions/{id}/reject        # 消费方拒绝破坏性变更（带理由，PENDING_VERSION 驳回）
GET    /snapshots?metric_code=&date_range=  # 指标结果快照查询（WORM 存证，PRD 4.5，受 4.9 权限）
```
**查询请求体（核心 schema）**
```json
{
  "metrics": ["gmv","order_cnt"],
  "dimensions": ["region","date"],
  "filters": [{"field":"channel","op":"in","value":["app","web"]}],
  "dateRange": {"from":"2026-07-01","to":"2026-07-31","granularity":"day"},
  "comparison": {"type":"yoy"},
  "accept_stale": false,
  "client_version": "1.0"
}
```
**统一响应 `meta`（呼应 4.11.7）**：`metric_version` / `freshness` / `granularity_bound` / `sample` / `stale` / `source_trace` / `quality_flag`。

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
  status ENUM('DRAFT','REVIEW','PUBLISHED','EXPERIMENTAL','DEPRECATED'),  -- EXPERIMENTAL=灰度（PRD 5.5.1）
  owner_id BIGINT, backup_owner_id BIGINT NULL,   -- 主/副 Owner（离职交接兜底，PRD 4.9.6）
  approver_id BIGINT NULL,
  pii_flag BOOLEAN, compliance_reviewed BOOLEAN DEFAULT FALSE,
  effective_version INT NULL,      -- 当前生效版本（PENDING_VERSION 场景下默认查询命中的版本）
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
CREATE TABLE notification (id BIGINT PK, user_id BIGINT, channel ENUM('inapp','email','webhook'), payload JSON, read_at DATETIME NULL);

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

-- 成本核算（PRD 4.10：按域/消费方聚合 LLM 与查询成本）
CREATE TABLE ops_cost (
  id BIGINT PK, cost_date DATE, domain VARCHAR(64), consumer_id VARCHAR(64) NULL,
  category ENUM('LLM','QUERY','STORAGE'), amount_usd DECIMAL(12,2), detail JSON, created_at DATETIME
);

-- 消费方凭证（FR-13 token 模型）
CREATE TABLE api_client (
  id BIGINT PK, client_id VARCHAR(64) UNIQUE, client_secret_ref VARCHAR(255),
  scope_domain VARCHAR(64), metric_whitelist JSON, qps INT DEFAULT 20,
  daily_quota INT DEFAULT 100000, scan_row_limit BIGINT,
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
```

### 4.2 Neo4j 血缘图
```
(:Table {id, source_id, name, sensitivity, dw_layer})
(:Field {id, table_id, name, type, pii})
(:Metric {code, version, status, tier, domain})
(:Dimension {code, standard_name})       -- 维度节点（PRD 4.4 USES_DIMENSION）
(:Report {id, name})   -- 消费方（QuickBI/API）
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

### 5.3 资源隔离（防 AI 打挂 OLAP，呼应 4.13.6）
- 查询路由：AI/DataAgent 流量 → 独立 OLAP 实例/队列；人工流量 → 主实例。
- 限流：消费方 `QPS=20`、`日调用=10万`、`单查询扫描行数上限`；超限 `429` + `retry_after`。
- 超大扫描：`scan_rows > quota` 直接 `422`，不裸跑。

### 5.4 业务错误码（统一语义表，C2）
所有服务返回体 `{code, message, data, trace_id, degraded}` 中的 `code` 取自下表，跨服务一致，禁止自定义数字码：
```
200  OK                        正常
400  PARAM_INVALID             入参校验失败（维度非法/粒度越界/分页错）
401  UNAUTH                    未携带有效 JWT / api_client token
403  FORBIDDEN                 越权（域/白名单不匹配，必入审计）
404  NOT_FOUND                 资源不存在（metric/source/term 等）
409  CONFLICT                  同名待仲裁 / 术语冲突 / 口径版本冲突
409  COMPLIANCE_BLOCKED         PII 指标 approve 被合规门禁拒绝，回 REVIEW 记拒因（PRD 4.9.5）
409  DRIFT_DETECTED             口径漂移高等级检出，已进 Owner 待办（PRD 4.7.8）
409  RECONCILIATION_ALERT       外部基准对账差异超阈值（PRD 4.8.8）
422  SCAN_OVER_QUOTA           单查询扫描行数超配额（不裸跑）
422  GRANULARITY_FORBIDDEN     下钻细于粒度的硬约束拒绝（4.5）
422  ADDITIVITY_VIOLATION      上卷违反指标可加性（NON_ADDITIVE/SEMI_ADDITIVE 跨不可加维度，须走重算，PRD 4.5）
429  RATE_LIMITED              限流（带 Retry-After，消费方 QPS/日配额）
503  AI_UNAVAILABLE            LLM 全供应商不可用
503  OLAP_DOWN                 下推引擎不可达（不返缓存错数，除非 accept_stale）
504  QUERY_TIMEOUT             下推超时（带预计恢复时间）
423  PII_BLOCKED               PII 口径在无权域被引用，转交 governance（4.7/4.9）
```
> `degraded=true` 仅出现在降级态（ES→LIKE、Neo4j→stale、推荐→榜单），不出现在 `403/409/422/429` 等错误态。消费方据 `code` 决定重试/`retry_after`/人工介入。

### 5.5 新增降级：分级分类引擎
| 能力故障 | 降级行为 | 用户态 |
|---------|---------|--------|
| 分级分类模型/引擎不可用 | `sensitivity_level` 暂标 `UNKNOWN`，资产地图热力退化为"未分级"灰区，注册/发布不阻断（仅提示），事后补扫（COMPL-2 重扫触发补全） | 提示"敏感度待定，已限制 PII 导出" |

---

## 6. 性能设计

| 场景 | 目标 | 手段 |
|------|------|------|
| 指标检索（ES） | P95 < 200ms | ES 索引 + 中文分词；列表分页游标 |
| 血缘影响面（Neo4j） | 千节点 P95 < 500ms | 图索引 + 深度限制（默认 ≤5 跳） |
| 语义查询（下推） | P95 < 3s（小表）/ < 10s（大跨期） | 分区裁剪 + 预聚合命中 + 查询计划路由 |
| 查询缓存 | 命中 P95 < 50ms | Redis `metric_version+params` 哈希，TTL 随新鲜度 |
| 采集增量 | 单源 < 5min | 监听 DDL/新增表，增量而非全量 |
| 限流计数 | < 1ms | Redis `INCR` + 滑动窗口 |

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
- **错误/降级态**：错误码→人话文案（如 `503 OLAP_DOWN` → "计算引擎暂不可用，预计 X 恢复，可稍后重试或联系管理员"）；降级能力灰显 + 角标"降级中"。

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
- [ ] 四类冲突（同名/同义不同名/粒度单位/跨域异源 + 口径版本 + PII 路由）检测触发正确：硬冲突阻断发布、软冲突仅提示、PII 冲突转交 governance（423）。
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
| 十二·PII合规 | 数据开发 | 注册含 PII→`semantic` 触发 `governance.pii_review`(423 路由,非普通仲裁)→合规官复核→`classification` 落库→通过方进发布 | `metric.compliance_reviewed=true`；定期 `rescan`(COMPL-2) |

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
     ├─ PII? ──是──> governance.pii_review(423) ──拒绝──> REVIEW(回退,reject_reason) ；通过──> 继续
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
| 权限/授权 | governance | MySQL.grant / audit_log | consume, observability | 鉴权依据 |
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
- **幂等**：消费者以 `event + ref_id` 去重，重复事件安全忽略；`conflict.opened` 同 `metric_a/metric_b` 不重复建。
- **降级**：Stream 不可用 → 生产者落本地落盘队列，恢复后补发；不影响主链路写 MySQL。

#### 12.0.2 口径变更缓存与 Cube 失效一致性（C3）
防止"口径改了、缓存/Cube 仍返旧数"：
- **缓存键含版本**：Redis 查询缓存键 = `metric:{code}:v{version}:{dim}:{dateRange}`；口径 `version` 变更 → 旧版键自然过期（TTL）+ 主动 `DEL` 前缀，新查询落新版本。
- **Cube 定义失效**：`metric.published` v2 时，发 `cube.invalidate(code, v1)` → 删旧预聚合 + 重建 v2（二期）；重建完成前 consume 路由回自研下推（§2.2）。
- **双写对账**：定时任务比对 MySQL.metric_version 与 ES/Neo4j/Cube 的 `version` 字段，不一致标 `stale` 并触发补写（呼应 §5.1）。

#### 12.0.3 统一错误码应用（C2）
所有服务返回体 `{code, message, data, trace_id, degraded}`，`code` 取 §5.4 语义表；网关中间件在 `403/409/422/423/429/503/504` 统一包装，越权(`403`)/PII(`423`)必写审计。消费方据 `code` 决策：
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
- 入：`GET /lineage/table/{id}`、`GET /lineage/field/{id}`、`GET /lineage/impact/{id}`（同步查 Neo4j）
- 出：消费查询日志 → lineage 反填 `CONSUMED_BY`

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

**指标健康度评分（PRD 5.5.3，治理驾驶舱数据源）**
- **模型**：单指标 0–100 分，五维加权（权重可配，默认：口径完整度 25% / 活跃度 20% / 质量 25% / Owner 响应 15% / 血缘覆盖 15%）。
- **口径完整度**：一等字段（granularity/unit/aggregation/time_semantics/freshness/sla/source/dimensions）齐全率。
- **活跃度**：近 30 天 consume 查询/消费次数归一化（源：`consume.queried` 事件 → observability 聚合）。
- **质量**：近 30 天 `quality_event` 异常数反比 + 质量门禁通过率。
- **Owner 响应**：反馈/审核平均时效（SLA 内比例，源：audit_log）。
- **血缘覆盖**：上游解析率（物理表→字段覆盖，源：lineage）。
- **刷新与分级**：每日凌晨批量重算 + 关键事件（质量异常/状态变更）实时增量；≥85 优（绿）/ 70–84 良（蓝）/ 55–69 警（橙）/ <55 危（红）；红橙指标自动进整改待办（notify.todo）。某维度数据缺失（如埋点未覆盖）→ 该维记 0 并标"数据不足"，不臆造分数。
- **用途**：`GET /metrics/dashboard` 驾驶舱、治理红黑榜、指标退役建议（长期低活跃 + 低消费自动建议 DEPRECATED）。

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
   - **PII 冲突（特殊路由，C4）**：含 PII 口径在无权域被引用 → **不进普通仲裁**，转交 `governance.pii_review`（423 PII_BLOCKED），由合规裁决而非业务仲裁。
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
2. 授权：`POST /grants`（域 + 指标白名单 + 行级开关），写入 `grant`。
3. **PII 门禁**：`POST /pii/review`（COMPL-1）复核分级与脱敏策略，写 `metric.compliance_reviewed=true`；semantic.approve 前查此标志。门禁拒绝时 conflict 服务收到 `423 PII_BLOCKED` 路由（C4），不进普通仲裁。
4. **分级重扫**：`POST /classification/rescan`（COMPL-2）→ 调分级引擎重算 `db_catalog.sensitivity_level` + 写 `classification` 表（sensitivity_level/pii_columns/model_version）+ 触发 assetmap 热力刷新事件 `classification.done`。引擎不可用时降级（§5.5），标 `UNKNOWN` 不阻断。
5. 鉴权查询：`GET /me/permissions` → 返回当前用户域/指标权限快照，供 consume 与前端按钮级控制。
6. **维度映射维护**：维护 `dimension` / `dimension_mapping` / `metric_dimension`（L2 落库）；消费侧 `POST /query` 校验维度合法性时反查此表。
7. **同源口径对账调度**：定时任务跑 `reconciliation`（同源多指标一致性），差异超 `threshold` → 写 `reconciliation.status=ALERT` + 触发 conflict 候选（防"同表出俩数"）。

**接口调用链**：
- 入：§3.5 全部端点
- 出：→ semantic（approve 拦截）、→ consume（鉴权依据）、→ assetmap（热力刷新）、→ conflict（PII/同源对账候选）

**数据流转**：
```
用户/合规官 --授权/复核--> governance --写--> MySQL.grant / metric.compliance_reviewed / classification
governance --门禁--> semantic(approve拦截) / conflict(423 PII路由)
governance --权限快照--> consume(查询鉴权)
governance --分级结果--> assetmap(热力) + classification(落库)
```

**关键算法/容错**：
- 越权访问 → `403 FORBIDDEN` + 审计（observability）。
- 行级权限一期为骨架（域级），二期深化（行级表达式）。
- 分级引擎降级见 §5.5，不影响注册/发布主链路。

---

### 12.6 consume（消费/查询服务，FR-12/13）

**职责边界**：平台对外的"可信口径查询出口"。负责下推、缓存、meta 标注、鉴权限流降级；不持有源数据。

**核心流程（查询主链路）**
1. `POST /query`（前端 / api_client）：
   - **鉴权（token 模型，D3）**：用户态 `Bearer JWT` → governance.permissions 校验域/白名单；消费方 `X-Api-Key` → 换短效 `Bearer JWT`（含 `client_id/scope/tenant_id/metric_whitelist/TTL`），密钥经 Secret Manager，可吊销可轮换；越权 `403`+审计。
   - **限流（D3）**：按 client 维度 Redis 滑动窗口（`QPS=20`、日调用 `10万`、单查询扫描行上限 `scan_row_limit`）；超限 `429`+`Retry-After`；热点驱动 semantic 预聚合。
   - **口径解析（语义锚定）**：用 `metric_code`+`version` 从 MySQL 取 `definition_json`（AST），**绝不接受裸 SQL**，降 NL 幻觉。
   - **维度/粒度校验**：反查 `metric_dimension`+`dimension` 验证 `dimensions` 合法性；细于 `granularity_bound` → `422 GRANULARITY_FORBIDDEN`（硬约束，呼应 4.5）。
   - **可加性校验（additivity，PRD 4.5）**：请求按维度上卷时，若指标 `additivity=NON_ADDITIVE` 或 `SEMI_ADDITIVE` 且目标维度落在 `non_additive_dimensions` → `422 ADDITIVITY_VIOLATION`，引导走重算路径（COUNT(DISTINCT)/AVG 上卷重算而非汇总，避近似误差）。
   - **缓存查询（C3）**：`key = metric:{code}:v{version}:{dim}:{dateRange}`；命中且未过期 → 返（带 `freshness`）；口径版本变更时旧键主动失效。
   - **方言翻译（D1）**：`definition_json` AST → 调 semantic 的"口径→方言 SQL 翻译层"生成目标引擎 SQL（MySQL/PG/Doris/Hive 适配器），统一处理维表 JOIN / 时区 / 币种 / 分区裁剪。
   - **批流双路路由（serving_mode，PRD 4.5）**：指标为 `BATCH_REALTIME_DUAL` 时，默认取实时路径（经 `data_set.serving_path=REALTIME` 解析），消费方可显式 `serving_mode=batch` 走批路径；响应 `meta.freshness` 返回双路径就绪态（`{batch:"T+1 08:30", realtime:"5min"}`）。质量规则按路径分别配置。
   - **路由决策**：命中物化/聚合表 → 直查（最快）；否则按 serving_mode 路径实时下推 OLAP（一期自研 / 二期 Cube `/cubejs-api/v1/load`）。
   - **执行保护**：扫描行 > `quota` → `422 SCAN_OVER_QUOTA`；超时 → `504 QUERY_TIMEOUT`+预计恢复；可 `query_id` 取消在途（§3.6 `GET /query/{id}/cancel`）。
   - **OLAP 不可达**：`503 OLAP_DOWN`+`retry_after`；仅当 `accept_stale=true` 返陈旧缓存（§5.2 舱壁）。
   - **meta 标注**：`metric_version / freshness / granularity_bound / sample / stale / source_trace / quality_flag`（低于阈值标 `quality_downgraded`）。
   - **结果回写**：写缓存 + 发 `consume.queried` 事件 → lineage(CONSUMED_BY 反填)/observability(度量)/recommend(埋点)。
2. `POST /query/dry-run`（L3，4.11.6）：对样本分区跑口径，返样例结果+耗时+扫描行数；**不计费/不写生产/不进缓存**，用于注册/改口径 Owner 比对、质量规则预校验。
3. `POST /embed/quickbi`（FR-12）：取嵌入令牌（含 scope 约束），回传 `metric_code`+口径版本+合法维度/粒度边界；超界拖拽前端拦截"已聚合至 X 粒度"。
4. `/metrics/{code}/semantic`：只读语义拉取（api_client），受 scope + `metric_whitelist`。
5. **消费侧运行时冲突检测（L3，4.11.8）**：从 `consume.queried` 日志提取"指标名/口径版本/消费方"三元组；发现两报表以不同口径版本调用同一指标名 → 触发运行时告警 + 生成 `Conflict` 候选（走 12.4 仲裁闭环），防"线上已出俩数"。
6. **查询血缘反填（L3，4.11.10）**：实际查询日志回灌 `lineage`，补全 `Report -[CONSUMED_BY]-> Metric` 下游边（比静态推测更准），支撑 conflict 运行时冲突 / observability 度量 / assetmap 热度。
7. **结果快照存证（L3，PRD 4.5，P1）**：对 Tier-1 及涉财务/合规指标，查询结果（即时查询或物化）落 `metric_value_snapshot`（WORM，只写不删）——含 `metric_code`/`version`/`dims`/`date_range`/`value`/`quality_flag`/`generated_at`；供 4.7 争议仲裁证据回溯、外部审计佐证、跨期对账（"上月报财务的数 vs 本月口径"）。`GET /snapshots` 查询受 governance 权限 + 行数上限 + PII 脱敏。快照生成对 Tier-1 全量、Tier-2/3 可配（默认不落，控制存储）。
8. **口径版本消费方确认回调（L3，PRD 5.5.1）**：收到 `metric.pending_version` 通知的消费方经 `POST /versions/{id}/confirm|reject` 回执（14 天超时默认接受，一次延期 +7 天）；confirm 后 semantic 将 `PENDING_VERSION` 升为 `CURRENT` 并触发物化重建；reject 带理由驳回变更。

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
2. **埋点**：所有消费/查询事件写 `event_log`（user_id, event, target_metric, ctx_json）→ 供 recommend 协同计算（共查频次）。
3. **订阅中心**：用户可配置 channel 偏好（inapp/email/webhook）+ 免打扰（避免告警风暴，呼应 quality 去重）。
4. **指标结果主动投递（PRD 4.5 `metric_delivery`）**：① 定时投递——按 `schedule_cron`（每日/每周）拉取指标值（转调 consume `/query`）推送到钉钉/邮件/Webhook；② 阈值触发——指标值突破 `trigger_rule` 即时推送。投递配置与 governance 对齐（`receiver_scope` 接收方须有该指标可见权限），投递结果入 observability 度量。

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
| PII 冲突路由 | PII 口径在无权域被引用 → 转交 governance.pii_review（423），不进普通仲裁闭环（C4） |
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
