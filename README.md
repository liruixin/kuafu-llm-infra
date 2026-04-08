# kuafu-llm-infra

LLM 模型稳定性基础设施 —— 为多模型、多提供商场景提供 OpenAI 兼容的统一调用接口，内置智能降级、健康探测、指标采集与告警。

## 为什么需要

当业务同时使用多个 LLM 提供商时，不可避免会遇到：

- 某个提供商突然变慢，TTFT 飙升
- 某个提供商间歇性返回空内容
- 某个提供商直接宕机，需要毫秒级切换

`kuafu-llm-infra` 在 SDK 层与业务层之间提供一个**稳定性网关**，让业务代码只关心「调哪个模型」，不关心「哪个提供商在服务」。

## 核心特性

- **OpenAI 兼容接口** — `client.chat.completions.create()`，零学习成本
- **模型为第一公民** — 以官方模型 ID 为主键，各提供商映射各自的 model_id
- **智能降级** — 基于健康状态、速度、成功率、稳定性的复合评分，自动选择最优提供商
- **实时流式监控** — TTFT 超时、空帧检测、流中断、慢速检测，请求级实时降级
- **声明式指标** — `MetricDef` 注册表，新增指标只需一行声明，后端自动注册
- **Provider 注册表** — `@register_provider` 装饰器，新增提供商类型只需一个文件
- **策略注册表** — `@register_strategy` 装饰器，新增降级策略零侵入
- **配置热更新** — 单一 `update_config()` 入口，Redis Pub/Sub 多实例同步
- **配置校验** — 启动时自动校验 strategy→model→provider 引用完整性
- **灵活告警** — 飞书 / Webhook / 日志，去重静默
- **可插拔后端** — 状态（Memory / Redis）、指标（Simple / Prometheus）

## 架构

```
业务代码
    │
    │  client = create_client("llm_stability.yaml")
    │  await client.chat.completions.create(...)
    │
    ▼
┌───────────────────────────────────────────────────────────┐
│  Gateway  (OpenAI 兼容接口)                                │
│                                                           │
│  ┌───────────┐  ┌───────────┐  ┌─────────┐  ┌─────────┐  │
│  │ Fallback  │  │  Metrics  │  │  Alert  │  │  State  │  │
│  │  Engine   │  │ Registry  │  │Dispatch │  │ Backend │  │
│  └─────┬─────┘  └───────────┘  └─────────┘  └─────────┘  │
│        │                                                  │
│  ┌─────┴──────────────────────────────────┐               │
│  │  StreamMonitor + RequestRecorder       │               │
│  │  (策略监控 + 指标/告警集中记录)           │               │
│  └─────┬──────────────────────────────────┘               │
│        │                                                  │
│  ┌─────┴──────────────────────────────────┐               │
│  │  Provider Registry (SDK 适配器)         │               │
│  │  @register_provider("openai")          │               │
│  │  @register_provider("anthropic")       │               │
│  └────────────────────────────────────────┘               │
└───────────────────────────────────────────────────────────┘
```

## 项目结构

```
kuafu_llm_infra/
├── __init__.py                     # 导出 LLMClient, create_client, TokenUsage, RequestContext
├── types.py                        # TokenUsage, RequestContext 核心数据类型
├── gateway.py                      # OpenAI 兼容的统一调用入口
│
├── config/
│   ├── schema.py                   # Pydantic 配置模型 + validate_references()
│   └── loader.py                   # YAML / dict / 环境变量加载
│
├── providers/
│   ├── base.py                     # Provider 抽象基类 + 统一响应类型
│   ├── registry.py                 # @register_provider 注册表
│   ├── openai_provider.py          # OpenAI SDK 适配器（自注册）
│   └── anthropic_provider.py       # Anthropic SDK 适配器（自注册）
│
├── fallback/
│   ├── engine.py                   # 降级编排引擎（纯编排逻辑）
│   ├── stream_monitor.py           # 流式实时策略监控
│   ├── recorder.py                 # 指标记录 + 告警发送集中处理
│   ├── scorer.py                   # 复合评分 + Provider 排序
│   ├── health_checker.py           # 后台健康探测（按模型×提供商粒度）
│   └── strategies/
│       ├── base.py                 # 策略基类
│       ├── registry.py             # @register_strategy 注册表
│       ├── ttft_timeout.py         # 首 Token 超时（自注册）
│       ├── empty_frame.py          # 空帧检测（自注册）
│       ├── chunk_gap.py            # 流中断检测（自注册）
│       ├── slow_speed.py           # 慢速检测（自注册）
│       └── total_timeout.py        # 总超时检测（自注册）
│
├── metrics/
│   ├── registry.py                 # MetricDef 声明中心（12 个指标集中定义）
│   ├── collector.py                # 3 个泛型方法：inc() / observe() / set()
│   ├── simple.py                   # 内存后端（零依赖）
│   └── prometheus.py               # Prometheus 后端（自动注册）
│
├── alert/
│   ├── dispatcher.py               # 异步告警分发
│   ├── rules.py                    # 去重 / 静默
│   └── channels/
│       ├── base.py                 # AlertEvent + 通道基类
│       ├── log.py                  # 日志通道
│       ├── feishu.py               # 飞书 Webhook
│       └── webhook.py              # 通用 Webhook
│
└── state/
    ├── backend.py                  # 状态抽象层 + ScoreCard / ProbeResult
    ├── memory.py                   # 内存后端
    └── redis.py                    # Redis 后端（分布式锁 + Pub/Sub）
```

## 快速开始

### 安装

```bash
pip install kuafu-llm-infra

# 可选依赖
pip install kuafu-llm-infra[prometheus]   # Prometheus 监控
pip install kuafu-llm-infra[redis]        # Redis 状态后端 + 多实例同步
```

### 配置

创建 `llm_stability.yaml`：

```yaml
llm_stability:
  # 提供商（纯连接凭证）
  providers:
    openai-next:
      type: openai
      api_key: "sk-xxx"
      base_url: "https://api.openai-next.com/v1"

    ppio:
      type: openai
      api_key: "sk-yyy"
      base_url: "https://api.ppinfra.com/openai/v1"

  # 模型定义（模型为第一公民）
  models:
    claude-opus-4-5-20251101:
      providers:
        - provider: openai-next
          priority: 1
          probe: true
        - provider: ppio
          model_id: "pa/claude-opus-4-5-20251101"   # 该提供商使用不同的模型 ID
          priority: 2
          probe: true

    gpt-4.1-2025-04-14:
      providers:
        - provider: openai-next
          priority: 1
          probe: true

  # 业务场景策略
  strategies:
    requirement_clarify:
      mode: stream
      primary: claude-opus-4-5-20251101
      fallback:
        - gpt-4.1-2025-04-14
      timeout:
        ttft: 8
        chunk_gap: 15
        total: 120

    code_generation:
      mode: block
      primary: gpt-4.1-2025-04-14
      timeout:
        total: 60

  # 健康探测
  health_check:
    interval: 20
    failure_threshold: 3
    timeout: 10

  # 指标采集
  metrics:
    enabled: true
    backend: simple               # "simple" | "prometheus"
    label_keys: [user_id, module] # 自定义标签维度

  # 告警
  alert:
    channels:
      - type: log
    rules:
      silence_seconds: 60

  # 状态后端
  state_backend:
    type: memory                  # "memory" | "redis"
```

### 使用

```python
from kuafu_llm_infra import create_client

client = create_client("llm_stability.yaml")

# 流式调用（OpenAI 兼容）
stream = await client.chat.completions.create(
    model="claude-opus-4-5-20251101",
    messages=[{"role": "user", "content": "你好"}],
    stream=True,
    business_key="requirement_clarify",
    labels={"user_id": "u123", "module": "chat"},
)
async for chunk in stream:
    print(chunk.content, end="", flush=True)

# 非流式调用
response = await client.chat.completions.create(
    model="gpt-4.1-2025-04-14",
    messages=[{"role": "user", "content": "你好"}],
    business_key="code_generation",
)
print(response.content)
```

Gateway 内部自动完成 Provider 选择、异常降级、指标采集和告警通知。业务代码无需关心底层切换逻辑。

## 降级流程

```
请求 ──► Scorer 按复合评分排序提供商
           │
           ├─► Provider A ──► 成功 ──► 记录指标，返回
           │       │
           │       └─► 失败/超时/空内容 ──► 记录失败，切换
           │
           ├─► Provider B ──► 成功 ──► 记录指标，返回
           │       │
           │       └─► 失败 ──► 记录失败
           │
           └─► 当前模型所有提供商耗尽
                   │
                   ▼
           Fallback 模型 ──► 同样流程
                   │
                   └─► 全部耗尽 ──► 抛出异常 + 发送告警
```

### 流式两阶段模型

| 阶段 | 状态 | 策略动作 |
|------|------|---------|
| Phase 1（首 Token 前） | 尚未返回内容给用户 | **SWITCH** — 中断当前提供商，切换下一个 |
| Phase 2（内容流中） | 已有内容返回给用户 | **RECORD** — 无法切换，仅记录异常 |

## 复合评分

```
Score = 健康门控 × (0.25 × 优先级 + 0.30 × 速度 + 0.35 × 成功率 + 0.10 × 稳定性)
```

| 维度 | 权重 | 计算方式 |
|------|------|---------|
| 优先级 | 25% | `1 / priority`（配置值越小分越高） |
| 速度 | 30% | `1 / (1 + ttft)`（近期请求 TTFT + 探测 TTFT 加权混合） |
| 成功率 | 35% | 近 300 秒请求成功率 |
| 稳定性 | 10% | `1 - 连续失败 / 阈值` |

## 配置热更新

```python
# 程序内热更新
await client.update_config(new_config_dict)

# 多实例场景（Redis 后端）
# 一个实例 update_config() → Redis Pub/Sub → 所有实例自动同步
await client.update_config(new_config_dict, broadcast=True)
```

## 指标

12 个内置指标，通过 `MetricDef` 注册表声明：

| 指标 | 类型 | 范围 | 说明 |
|------|------|------|------|
| `llm_request_total` | Counter | 请求级 | 请求总数（按 status 分） |
| `llm_request_duration_seconds` | Histogram | 请求级 | 请求耗时 |
| `llm_ttft_seconds` | Histogram | 请求级 | 首 Token 延迟 |
| `llm_tokens_per_second` | Histogram | 请求级 | Token 吞吐量 |
| `llm_input_tokens_total` | Counter | 请求级 | 输入 Token 总量 |
| `llm_output_tokens_total` | Counter | 请求级 | 输出 Token 总量 |
| `llm_fallback_total` | Counter | 请求级 | 降级次数 |
| `llm_strategy_triggered_total` | Counter | 请求级 | 策略触发次数 |
| `llm_provider_health` | Gauge | 系统级 | 提供商健康状态 (0/1) |
| `llm_provider_score` | Gauge | 系统级 | 提供商复合评分 |
| `llm_probe_ttft_seconds` | Histogram | 系统级 | 探测 TTFT |
| `llm_probe_total` | Counter | 系统级 | 探测请求总数 |

请求级指标标签：`model` + `provider` + 配置中 `label_keys` 声明的自定义标签。

### 新增指标

```python
# 1. 在 metrics/registry.py 声明
MY_METRIC = MetricDef("llm_my_metric", MetricKind.COUNTER, "描述")

# 2. 在调用处记录
metrics.inc(MY_METRIC, model="gpt-4", provider="openai", custom_label="value")

# 完成。无需修改 collector / simple / prometheus 任何代码。
```

## 扩展

### 新增提供商类型

```python
# providers/my_provider.py
from .base import BaseProvider
from .registry import register_provider

class MyProvider(BaseProvider):
    ...

@register_provider("my_type")
def _create(api_key, base_url, extra_headers=None):
    return MyProvider(api_key=api_key, base_url=base_url)
```

然后在配置中使用 `type: my_type` 即可。

### 新增降级策略

```python
# fallback/strategies/my_strategy.py
from .base import BaseStrategy
from .registry import register_strategy

class MyStrategy(BaseStrategy):
    ...

@register_strategy
def create_my_strategy(cfg, provider, model):
    return MyStrategy(cfg.my_threshold, provider, model)
```

策略自动生效，Engine 无需任何修改。

## 环境要求

- Python >= 3.10
- openai >= 1.0
- anthropic >= 0.30
- pydantic >= 2.0
- PyYAML

## License

[Apache License 2.0](LICENSE)
