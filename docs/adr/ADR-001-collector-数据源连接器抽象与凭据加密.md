# ADR-001: 数据源采集采用连接器 SPI 抽象 + 凭据 Fernet 加密 + 外部依赖显式重试

## 状态：Accepted
## 日期：2026-08-08
## 背景
指标语义中台需从多种异构数据源（MySQL 等）采集库表/字段元数据，建立统一资产目录（TD §12.1）。核心挑战：
1. 多源异构、连接管理复杂，且要可测试、可替换；
2. 连接凭据（账号/密码/连接串）属高敏信息，落库需加密、API 需脱敏；
3. 源库故障不应被静默掩盖，否则下游目录出现静默数据缺失。

对应模块 `collector`（状态 released，13/13 门禁）。

## 决策
1. **连接器 SPI 抽象**：定义 `Connector` 协议（仅 `async query(sql, params)` 方法）与 `BaseCollector` 抽象基类；`InformationSchemaCollector` 为默认实现（基于 `information_schema` 采集），`SqlalchemyConnector` 为真实实现（`create_async_engine(..., pool_pre_ping=True)`）。测试可注入假连接器（Protocol 可替），`dispose()` 释放异步引擎避免连接池泄漏。
2. **凭据加密落库**：连接配置经 `SecretManager`（Fernet，密钥由 `settings.jwt_secret` 经 sha256 派生，确定性便于轮转后解密）加密为密文；`DataSourceResponse` 仅返回 `connection_config_present: bool`，绝不暴露明文。
3. **外部依赖显式重试**：源库失败统一抛 `ExternalDependencyError`（映射 503，可重试），**不**静默吞为 200。
4. **参数化查询 + 纵深防御**：所有 SQL 走 `Connector.query(sql, params)` 参数化；配合 `guard_against_injection` 中间件（FastAPI 依赖，扫描 query 参数与 JSON body 顶层字符串，命中注入即 400 + `INJECTION_DETECTED`）做额外一层拦截。

## 备选方案
- **直连各厂商 SDK / JDBC**：维护成本高、难统一抽象、难单测替身 → 否决。
- **凭据明文或仅存环境变量**：不符合合规与最小暴露原则 → 否决。
- **源库失败静默降级为 200**：掩盖数据缺失、破坏可观测性 → 否决。

## 后果
- **正面**：采集逻辑可穷举单测（连接器可 mock）；凭据不落明文、API 脱敏；源库故障可见、可重试、可告警。
- **负面**：新增数据源类型需实现 `Connector`；字段级采样依赖源库可读 `information_schema.columns`；加解密引入少量 CPU 开销（可接受）。
