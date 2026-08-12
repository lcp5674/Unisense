-- Unisense Doris DDL（P2: consume 语义查询下推 OLAP）
-- 在 Doris FE 上执行：mysql -h <fe_host> -P 9030 -u root < doris_init.sql

-- 1. 创建数据库
CREATE DATABASE IF NOT EXISTS unisense_olap;

USE unisense_olap;

-- 2. 指标事实表（聚合结果表，consume 层查询下推目标）
CREATE TABLE IF NOT EXISTS metric_value (
    metric_code    VARCHAR(64)  NOT NULL COMMENT '指标编码',
    domain         VARCHAR(64)  NOT NULL COMMENT '所属域',
    dim_date       DATE         NOT NULL COMMENT '统计日期',
    dim_version    INT          NULL COMMENT '版本号',
    value          DECIMAL(20,4) NULL COMMENT '指标值',
    is_pii         BOOLEAN      NOT NULL DEFAULT FALSE COMMENT '是否含PII',
    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
    
    -- Doris 特有
    INDEX idx_domain (domain) USING BITMAP,
    INDEX idx_pii (is_pii) USING BITMAP
)
UNIQUE KEY(metric_code, dim_date, dim_version)
DISTRIBUTED BY HASH(metric_code) BUCKETS 8
PROPERTIES (
    "replication_allocation" = "tag.location.default: 1",
    "enable_unique_key_merge_write" = "true"
);

-- 3. 指标维度关联宽表（consume 层按维度查询）
CREATE TABLE IF NOT EXISTS metric_dimension_assoc (
    metric_code    VARCHAR(64)  NOT NULL COMMENT '指标编码',
    dim_type       VARCHAR(32)  NOT NULL COMMENT '维度类型(PARTITION/SPLICE/FILTER)',
    dim_code       VARCHAR(64)  NOT NULL COMMENT '维度编码',
    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
)
UNIQUE KEY(metric_code, dim_type, dim_code)
DISTRIBUTED BY HASH(metric_code) BUCKETS 4
PROPERTIES (
    "replication_allocation" = "tag.location.default: 1"
);

-- 4. 质量观测时序表（quality 高级检测模式的观测值写入）
CREATE TABLE IF NOT EXISTS quality_observation (
    rule_id        INT          NOT NULL COMMENT '质量规则ID',
    metric_id      INT          NOT NULL COMMENT '指标ID',
    source         VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT '数据源',
    obs_value      DECIMAL(20,4) NOT NULL COMMENT '观测值',
    observed_at    DATETIME     NOT NULL COMMENT '观测时间',
    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
)
DUPLICATE KEY(rule_id, metric_id, observed_at)
DISTRIBUTED BY HASH(rule_id) BUCKETS 4
PROPERTIES (
    "replication_allocation" = "tag.location.default: 1"
);
