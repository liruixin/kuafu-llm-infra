-- ============================================================================
-- LLM 请求指标表 — 配合 kuafu-llm-infra recording 模块使用
--
-- 固定列由 ClickHouseRecordSink 写入，label 列与 YAML 配置中
-- recording.clickhouse.label_columns 保持一致。
--
-- 示例 YAML:
--   recording:
--     enabled: true
--     clickhouse:
--       host: "your-clickhouse-host"
--       database: "llm_metrics"
--       label_columns: [app_id, conversation_id]
-- ============================================================================

CREATE DATABASE IF NOT EXISTS llm_metrics;

CREATE TABLE IF NOT EXISTS llm_metrics.llm_request_metrics
(
    -- 时间（必须放第一位，分区键）
    event_time          DateTime64(3),
    date                Date DEFAULT toDate(event_time),

    -- 业务维度（label_columns，按需增减，需与 YAML 配置一致）
    app_id              String                 DEFAULT '',
    conversation_id     String                 DEFAULT '',

    -- 模型维度（固定列）
    provider            LowCardinality(String),
    model               LowCardinality(String),

    -- Token 指标
    input_tokens        UInt32,
    output_tokens       UInt32,
    cache_read_tokens   UInt32,

    -- 延迟指标
    total_latency_ms    UInt32,
    ttft_ms             UInt32,

    -- 状态
    status              LowCardinality(String),   -- success / error
    error_code          String                 DEFAULT ''
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (app_id, provider, model, event_time)
TTL date + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;
