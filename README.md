# kuafu-llm-infra

LLM 模型基础设施层 —— 为多模型、多提供商场景提供统一调用接口，内置智能降级、健康监控、测速探针与告警能力。

## Why

当你的业务同时使用多个 LLM 提供商（OpenAI、Anthropic、Google、国内各平台）时，不可避免会遇到：

- 某个提供商突然变慢，TTFT 飙升
- 某个提供商间歇性返回空内容
- 某个提供商直接宕机，需要快速切换

`kuafu-llm-infra` 在 SDK 层与业务层之间提供一个**稳定性网关**，让业务代码只关心「调模型」，不关心「哪个提供商在服务」。

## Features

- **统一调用接口** — 一个 Gateway，屏蔽 OpenAI / Anthropic / Google SDK 差异
- **智能降级** — 基于健康状态、速度评分、失败计数的多维度 Provider 选择
- **实时测速** — 定期探测 TTFT 和吞吐量，慢速 Provider 自动降权
- **流式异常检测** — 空帧检测、流中断超时，请求级实时降级
- **可插拔监控** — 内置 Prometheus / 内存两种 Metrics 后端
- **灵活告警** — 飞书 / Webhook / 日志，支持去重和频控
- **配置热更新** — 文件 / HTTP 多配置源，秒级生效，无需重启
- **模块化设计** — 各子模块独立，按需引入

## Architecture

```
Your Service
    │
    │  from kuafu_llm_infra import create_gateway
    │  gateway = create_gateway("llm_stability.yaml")
    │
    ▼
┌──────────────────────────────────────────────┐
│  Gateway (统一对话接口)                        │
│                                               │
│  ┌─────────┐ ┌─────────┐ ┌───────┐ ┌───────┐ │
│  │ Fallback│ │ Metrics │ │ Alert │ │Config │ │
│  │ Engine  │ │Collector│ │Dispatch│ │Watcher│ │
│  └────┬────┘ └─────────┘ └───────┘ └───────┘ │
│       │                                       │
│  ┌────┴──────────────────────────┐            │
│  │  Providers (SDK Adapters)     │            │
│  │  OpenAI │ Anthropic │ Google  │            │
│  └───────────────────────────────┘            │
└──────────────────────────────────────────────┘
```

## Project Structure

```
kuafu_llm_infra/
├── gateway.py                  # 统一对话接口，业务唯一入口
├── config/
│   ├── schema.py               # 配置数据结构定义与校验
│   ├── loader.py               # 配置加载（文件 / HTTP / 自定义源）
│   └── watcher.py              # 配置热更新（定时拉取 + 变更回调）
├── fallback/
│   ├── engine.py               # 降级决策引擎
│   ├── health_checker.py       # 提供商探活
│   ├── speed_prober.py         # 测速探针（TTFT / 吞吐量）
│   ├── scorer.py               # 提供商综合评分
│   └── strategies/             # 可扩展降级策略
│       ├── base.py             # 策略基类
│       ├── failure_count.py    # 连续失败降级
│       ├── slow_speed.py       # 慢速降级
│       └── empty_frame.py      # 空帧降级
├── metrics/
│   ├── collector.py            # 指标采集器（抽象层）
│   ├── prometheus.py           # Prometheus 后端
│   └── simple.py              # 内存后端（零依赖）
├── alert/
│   ├── dispatcher.py           # 告警分发
│   ├── rules.py                # 去重 / 静默 / 频控
│   └── channels/
│       ├── base.py             # 通道基类
│       ├── feishu.py           # 飞书 Webhook
│       ├── webhook.py          # 通用 Webhook
│       └── log.py              # 日志（默认兜底）
└── providers/
    ├── base.py                 # Provider 适配器基类
    ├── openai_provider.py      # OpenAI SDK 适配
    ├── anthropic_provider.py   # Anthropic SDK 适配
    └── google_provider.py      # Google GenAI SDK 适配
```

## Quick Start

### Installation

```bash
pip install kuafu-llm-infra

# 按需安装可选依赖
pip install kuafu-llm-infra[prometheus]  # Prometheus 监控
pip install kuafu-llm-infra[google]      # Google GenAI 支持
pip install kuafu-llm-infra[redis]       # Redis 状态后端
pip install kuafu-llm-infra[all]         # 全部可选依赖
```

### Configuration

创建 `llm_stability.yaml`：

```yaml
llm_stability:
  providers:
    provider-a:
      api_key: "sk-xxx"
      openai_base_url: "https://api.provider-a.com/v1"
      anthropic_base_url: "https://api.provider-a.com"
      priority: 1
      enabled: true
      model_mapping:
        claude-opus-4-5-20251101: claude-opus-4-6

    provider-b:
      api_key: "sk-yyy"
      openai_base_url: "https://api.provider-b.com/v1"
      priority: 2
      enabled: true

  models:
    - model: gpt-4.1-2025-04-14
      interface: openai
      providers: [provider-a, provider-b]
      fallback_models: [deepseek-v3]

    - model: claude-opus-4-5-20251101
      interface: anthropic
      providers: [provider-a]
      fallback_models: [gpt-4.1-2025-04-14]

  health_check:
    interval: 20
    failure_threshold: 3
    recovery_threshold: 3
    timeout: 25
    probe_max_tokens: 5

  speed_probe:
    enabled: true
    interval: 20
    ttft_threshold_ms: 5000

  metrics:
    backend: prometheus

  alert:
    channels:
      - type: feishu
        webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
    rules:
      silence_seconds: 60
```

### Usage

```python
from kuafu_llm_infra import create_gateway

gateway = create_gateway("llm_stability.yaml")

# Streaming
async for chunk in gateway.chat_stream(
    model="claude-opus-4-5-20251101",
    messages=[{"role": "user", "content": "Hello"}],
    max_tokens=4096,
):
    print(chunk.text, end="", flush=True)

# Non-streaming
response = await gateway.chat(
    model="gpt-4.1-2025-04-14",
    messages=[{"role": "user", "content": "Hello"}],
    max_tokens=4096,
)
print(response.content)
```

Gateway 内部自动完成 Provider 选择、异常降级、Metrics 采集和告警通知。业务代码无需关心底层切换逻辑。

## Fallback Flow

```
Request ──► Scorer ranks providers
              │
              ├─► Provider A ──► Success ──► Record metrics, return
              │       │
              │       └─► Fail/Slow/Empty ──► Record failure
              │
              ├─► Provider B ──► Success ──► Record metrics, return
              │       │
              │       └─► Fail ──► Record failure
              │
              └─► All providers exhausted
                      │
                      ▼
              Fallback models ──► Same flow per fallback model
                      │
                      └─► All exhausted ──► Raise error + Alert
```

## Configuration Sources

| Source | Description |
|--------|-------------|
| `file` | 本地 YAML 文件（默认） |
| `http` | 远程 HTTP 接口，定时拉取 |

```yaml
llm_stability:
  config_source:
    type: http
    url: "https://your-backend/api/llm-config"
    refresh_interval: 10    # 每 10 秒拉取一次
    fallback: file           # HTTP 失败时回退到本地文件
```

## Metrics

内置指标（以 Prometheus 为例）：

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `llm_request_total` | Counter | model, provider, status | 请求总数 |
| `llm_request_duration_seconds` | Histogram | model, provider | 请求耗时 |
| `llm_ttft_seconds` | Histogram | model, provider | 首 Token 延迟 |
| `llm_fallback_total` | Counter | model, from_provider, to_provider | 降级次数 |
| `llm_provider_health` | Gauge | provider | 健康状态 (0/1) |

## Requirements

- Python >= 3.10
- openai >= 1.0
- anthropic >= 0.30

## License

[Apache License 2.0](LICENSE)

## Contributing

Issues and pull requests are welcome at [GitHub](https://github.com/kuafuai/kuafu-llm-infra).
