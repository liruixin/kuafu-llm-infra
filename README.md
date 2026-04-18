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
  # 提供商（每个提供商可有多个协议端点）
  providers:
    openai-next:
      api_key: "sk-xxx"
      endpoints:
        openai:
          base_url: "https://api.openai-next.com/v1"

    ppio:
      api_key: "sk-yyy"
      endpoints:
        openai:
          base_url: "https://api.ppinfra.com/openai/v1"
        anthropic:
          base_url: "https://api.ppinfra.com/anthropic/v1"

  # 模型定义（模型为第一公民）
  models:
    claude-opus-4-5-20251101:
      providers:
        - provider: openai-next
          endpoint: openai
          priority: 1
          probe: true
        - provider: ppio
          endpoint: anthropic
          model_id: "claude-opus-4-5-20251101"
          priority: 2
          probe: true

    gpt-4.1-2025-04-14:
      providers:
        - provider: openai-next
          endpoint: openai
          priority: 1
          probe: true

  # 业务场景策略
  strategies:
    requirement_clarify:
      primary: claude-opus-4-5-20251101
      fallback:
        - gpt-4.1-2025-04-14
      timeout:
        ttft: 8
        chunk_gap: 15
        per_request: 120
        total: 180

    code_generation:
      primary: gpt-4.1-2025-04-14
      timeout:
        per_request: 60
        total: 120

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

然后在配置中使用该端点类型即可：

```yaml
providers:
  my-vendor:
    api_key: "sk-xxx"
    endpoints:
      my_type:
        base_url: "https://api.my-vendor.com/v1"
```

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

## 业务系统集成最佳实践

### 1. 生命周期管理

`LLMClient` 包含后台探测任务和告警消费任务，必须配合应用生命周期正确启停。

**FastAPI**

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from kuafu_llm_infra import create_client

llm_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global llm_client
    llm_client = create_client("llm_stability.yaml")  # start() 在内部自动调用
    yield
    await llm_client.shutdown()

app = FastAPI(lifespan=lifespan)
```

**纯 asyncio / 脚本（信号优雅退出）**

如果不使用 Web 框架，或者担心进程被强行终止（Ctrl+C / SIGTERM）时后台任务未清理，可以注册信号处理：

```python
import signal
import asyncio
from kuafu_llm_infra import create_client

async def main():
    client = create_client("llm_stability.yaml")

    # 注册信号，确保强退时优雅关闭
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(client.shutdown()))

    # ... 业务逻辑 ...

asyncio.run(main())
```

> **说明**：即使不注册信号处理，进程也能正常退出（后台任务均为 asyncio Task，不会阻塞进程），但可能会在日志中看到 `Task was destroyed but it is pending` 警告。注册信号后可以消除此警告并确保资源优雅释放。

**Django（ASGI）**

```python
# myapp/apps.py
import asyncio
from django.apps import AppConfig
from kuafu_llm_infra import create_client

class MyAppConfig(AppConfig):
    name = "myapp"

    def ready(self):
        from myapp.globals import llm_client_holder
        loop = asyncio.get_event_loop()
        llm_client_holder.client = create_client("llm_stability.yaml")

# myapp/globals.py
class _Holder:
    client = None

llm_client_holder = _Holder()
```

> **关键原则**：全进程共享一个 `LLMClient` 实例。多次 `create_client()` 会创建多套探测任务和状态，浪费资源且评分数据不共享。

---

### 2. 按业务场景设计 Strategy

Strategy 是连接「业务需求」和「模型选择」的桥梁。**每个 business_key 应对应一类真实的业务场景**，而非每个 API 接口一个。

```yaml
strategies:
  # ✅ 好的设计：按场景划分
  user_facing_chat:          # 用户可见的对话，要求低延迟
    mode: stream
    primary: claude-opus-4-5-20251101
    fallback: [gpt-4.1-2025-04-14]
    timeout:
      ttft: 5                # 用户等待容忍度低
      chunk_gap: 10
    slow_speed_threshold: 8.0

  background_analysis:       # 后台批量分析，容忍高延迟
    primary: deepseek-v3-2-251201
    fallback: [gpt-4.1-2025-04-14]
    timeout:
      per_request: 120         # 单次请求可以等久一些
      total: 240               # 整条链路总超时

  # ❌ 不好的设计：按接口划分
  # /api/v1/chat → chat_v1
  # /api/v2/chat → chat_v2     ← 实际需求相同，不应拆分
```

---

### 3. Labels 设计规范

`labels` 是指标的自定义维度。设计原则：**低基数、高价值、按需投入**。

```yaml
metrics:
  label_keys: [module, env]   # 在配置中声明所有可能的 label 名
```

```python
# ✅ 推荐：低基数、有分析价值
await client.chat.completions.create(
    ...,
    labels={"module": "customer_service", "env": "prod"},
)

# ❌ 避免：高基数字段（会导致 Prometheus 时间序列爆炸）
labels={"request_id": "uuid-xxx", "user_id": "每个用户不同"}
```

| 推荐维度 | 示例值 | 说明 |
|---------|--------|------|
| `module` | `chat`, `analysis`, `code_gen` | 业务模块 |
| `env` | `prod`, `staging` | 部署环境 |
| `priority` | `high`, `normal` | 业务优先级 |
| `team` | `algo`, `platform` | 调用团队 |

> 如果只需要在日志排查时关联请求 ID，放在日志 context 里而非 labels。

---

### 4. 错误处理

```python
from kuafu_llm_infra.fallback.engine import AllProvidersExhausted

async def call_llm(messages: list) -> str:
    try:
        resp = await client.chat.completions.create(
            business_key="user_facing_chat",
            messages=messages,
            labels={"module": "chat"},
        )
        return resp.content

    except AllProvidersExhausted:
        # 所有提供商（含 fallback）均不可用
        # 此时库已自动发送告警，业务层做兜底
        return "抱歉，服务暂时不可用，请稍后再试。"

    except ValueError as e:
        # business_key 未在配置中定义且未传 model
        logging.error(f"配置错误: {e}")
        raise
```

**流式场景的特殊性**：Phase 1（首 Token 前）降级对用户无感；Phase 2（已返回部分内容）无法切换，库会记录异常但不中断流。业务层应在流结束后检查是否完整：

```python
stream = await client.chat.completions.create(
    business_key="user_facing_chat",
    messages=messages,
    stream=True,
)

content = ""
async for chunk in stream:
    content += chunk.content
    yield chunk.content          # SSE 推送给前端

if not content.strip():
    # 极端情况：所有提供商都在 Phase 1 被切换，但最终仍无内容
    yield "[服务异常，请重试]"
```

---

### 5. 多实例部署

生产环境通常多实例部署，需要 Redis 后端保证：

- **探测去重** — 分布式锁，同一 (model, provider) 不被多实例重复探测
- **评分共享** — ScoreCard 存储在 Redis，所有实例基于相同数据排序
- **配置同步** — 一个实例 `update_config()` → Pub/Sub → 全集群生效

```yaml
state_backend:
  type: redis
  redis:
    url: "redis://redis:6379/0"
    key_prefix: "llm_infra:"       # 多项目共用 Redis 时通过前缀隔离
```

```python
# 管理后台触发配置变更
new_config = fetch_config_from_admin_panel()
await client.update_config(new_config, broadcast=True)
# → 本实例立即生效
# → 其他实例通过 Redis Pub/Sub 自动同步
```

> **单实例 / 开发环境**：使用默认的 `type: memory` 即可，零依赖。

---

### 6. 监控与告警

#### Prometheus + Grafana

```yaml
metrics:
  enabled: true
  backend: prometheus
  label_keys: [module, env]
```

业务侧通过 `client.get_metrics()` 获取 Prometheus 格式数据，挂到自己的 HTTP 路由：

```python
@app.get("/metrics_infra")
def metrics_infra():
    return Response(client.get_metrics(), media_type="text/plain")
```

推荐 Grafana Dashboard 面板：

| 面板 | PromQL | 说明 |
|------|--------|------|
| 请求成功率 | `sum(rate(llm_request_total{status="success"}[5m])) / sum(rate(llm_request_total[5m]))` | 整体成功率 |
| P99 TTFT | `histogram_quantile(0.99, rate(llm_ttft_seconds_bucket[5m]))` | 首 Token 延迟分布 |
| Token 消耗 | `sum(rate(llm_input_tokens_total[1h])) + sum(rate(llm_output_tokens_total[1h]))` | 每小时 Token 消耗速率 |
| 降级次数 | `sum(rate(llm_strategy_triggered_total[5m])) by (strategy)` | 按策略类型分组 |
| 提供商健康 | `llm_provider_health` | 直接展示 0/1 |

#### 告警通道

```yaml
alert:
  channels:
    - type: log                           # 开发环境：输出到日志
    - type: feishu                        # 飞书群告警
      webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
    - type: webhook                       # 接入自有告警平台
      url: "https://your-alert-platform.com/api/alert"
  rules:
    silence_seconds: 60                   # 同一 (title, provider) 60 秒内不重复告警
```

---

### 7. 配置管理建议

#### 开发环境

```yaml
# config/llm_stability.dev.yaml
llm_stability:
  providers:
    dev-proxy:
      api_key: "sk-dev-xxx"
      endpoints:
        openai:
          base_url: "http://localhost:8080/v1"

  metrics:
    enabled: true
    backend: simple            # 开发用内存指标，无需 Prometheus

  state_backend:
    type: memory

  health_check:
    interval: 60               # 开发环境降低探测频率
```

#### 生产环境

```yaml
# config/llm_stability.prod.yaml
llm_stability:
  providers:
    primary-vendor:
      api_key: "${LLM_PRIMARY_API_KEY}"
      endpoints:
        openai:
          base_url: "https://api.primary.com/v1"
    backup-vendor:
      api_key: "${LLM_BACKUP_API_KEY}"
      endpoints:
        openai:
          base_url: "https://api.backup.com/v1"

  metrics:
    enabled: true
    backend: prometheus
    label_keys: [module, env]

  state_backend:
    type: redis
    redis:
      url: "${REDIS_URL}"

  health_check:
    interval: 20
    failure_threshold: 3
    cooldown: 60
```

> **提示**：YAML 中的 `${ENV_VAR}` 需要业务层在加载前做环境变量替换，或使用 dict 方式传入配置：

```python
import os, yaml

with open("config/llm_stability.prod.yaml") as f:
    raw = f.read()
    for key, value in os.environ.items():
        raw = raw.replace(f"${{{key}}}", value)
    config_dict = yaml.safe_load(raw)

client = create_client(config_dict)
```

---

### 8. 集成检查清单

上线前对照检查：

- [ ] `create_client()` 只在启动时调用一次，全进程共享
- [ ] 应用退出时调用 `await client.shutdown()`
- [ ] `business_key` 按业务场景而非接口划分
- [ ] `label_keys` 已声明，且所有调用点传入了对应的 `labels`
- [ ] `labels` 中无高基数字段（request_id、user_id 等）
- [ ] 每个 model 至少配了 2 个 provider（单 provider 无降级意义）
- [ ] 每个 strategy 至少配了 1 个 fallback 模型
- [ ] 多实例部署已切换 `state_backend: redis`
- [ ] Prometheus 端口已配置且被监控系统抓取
- [ ] 告警通道已配置且经过测试
- [ ] 配置文件中无硬编码 API Key（使用环境变量或密钥管理服务）

## 环境要求

- Python >= 3.10
- openai >= 1.0
- anthropic >= 0.30
- pydantic >= 2.0
- PyYAML

## License

[Apache License 2.0](LICENSE)
