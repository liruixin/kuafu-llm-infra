# kuafu-llm-infra 核心机制：降级 / 健康探测 / 评分

## 一、模型降级机制

### 1.1 降级链路

每个业务场景（`business_key`）在配置中对应一个 Strategy，定义了**主模型** + **fallback 模型列表**：

```yaml
strategies:
  requirement_clarify:
    primary: claude-opus-4-5-20251101
    fallback:
      - gpt-4.1-2025-04-14
      - deepseek-v3-2-251201
```

请求发起后，Engine 按如下链路逐级尝试：

```
模型链: claude → gpt-4.1 → deepseek
         │
         ├─ 每个模型下有多个提供商，按评分从高到低排序
         │    Provider A (score=0.82) → Provider B (score=0.65) → ...
         │
         ├─ 某个提供商失败 → 记录失败 → 尝试同模型下一个提供商
         ├─ 同模型所有提供商耗尽 → 切换到 fallback 链中下一个模型
         └─ 所有模型所有提供商耗尽 → 抛出 AllProvidersExhausted + 发送告警
```

### 1.2 流式两阶段模型

流式请求的降级分两个阶段：

| 阶段 | 条件 | 降级动作 | 用户感知 |
|------|------|---------|---------|
| **Phase 1**（首 Token 前） | 尚未向用户返回任何内容 | **SWITCH** — 中断当前提供商，无缝切换下一个 | 无感知，仅感受到延迟 |
| **Phase 2**（内容流中） | 已有部分内容返回给用户 | **RECORD** — 无法切换（内容已发出），仅记录异常到指标 | 可能感受到中断 |

### 1.3 五种实时检测策略

每个流式请求启动时，会创建以下 5 个策略实例，逐 chunk 实时检测：

| 策略 | 触发条件 | 默认阈值 | 动作 |
|------|---------|---------|------|
| **TTFT 超时** | 首个有内容的 Token 迟迟不到 | `ttft: 8s` | SWITCH |
| **空帧检测** | 连续 N 个 chunk 内容为空 | `empty_frame_threshold: 5` | Phase 1: SWITCH / Phase 2: RECORD |
| **流中断检测** | 已有内容后，两个 chunk 间隔过长 | `chunk_gap: 15s` | RECORD（已有内容，无法切换） |
| **慢速检测** | Token 吞吐量低于阈值（≥20 token 后才评估） | `slow_speed_threshold: 5 token/s` | Phase 1: SWITCH / Phase 2: RECORD |
| **总超时** | 非流式请求总耗时超限 | `total: 60s` | SWITCH |

### 1.4 非流式降级

非流式（block 模式）更简单：

- 超时 → 切换下一个提供商
- 返回空内容 → 切换
- 异常 → 切换

> **⚠️ 待优化：末位提供商超时策略**
>
> 当前所有提供商（含 fallback 链末尾）均使用相同的超时阈值。在所有提供商普遍变慢的场景下（如大模型高峰期），会逐个超时、逐个降级，最终触发 `AllProvidersExhausted`。
>
> **慢成功 > 快失败**。后续优化方向：当请求到达整条链的最后一个提供商时，取消超时限制（`timeout=None`），宁可慢也要拿到结果。流式场景同理——最后一个提供商应跳过 TTFT / 慢速等 SWITCH 策略。这样既保留前面快速切换寻找更优提供商的能力，又保证最坏情况下至少能返回结果。

---

## 二、健康探测机制

### 2.1 探测粒度

按 **（模型 × 提供商）** 粒度探测，而不是按提供商整体探测。

> 原因：同一个提供商对不同模型的服务质量可能不同。比如 Provider A 的 GPT-4 正常，但其 Claude 通道可能出问题。

### 2.2 探测流程

```
Health Checker（后台循环任务）
    │
    │  每 20s 一轮（interval: 20）
    │
    ├─ 收集所有 probe=true 的 (model, provider) 对
    │
    ├─ 对每个目标（间隔 2s 错开，避免并发风暴）：
    │   │
    │   ├─ 获取分布式锁（Redis 模式下防止多实例重复探测）
    │   │
    │   ├─ 发送最小请求：messages=[{role:user, content:"hi"}], max_tokens=5
    │   │
    │   ├─ 成功 → 记录 TTFT、标记 health=true、更新 ScoreCard
    │   │
    │   └─ 失败 → 标记 health=false、更新 ScoreCard、发送告警
    │
    └─ 等待下一轮
```

### 2.3 探测结果如何影响评分

探测结果写入 `ScoreCard`，影响两个方面：

1. **健康门控**：`health=false` → 该提供商直接被排除（score=0），不参与排序
2. **速度评分**：`probe_ttft_ms` 参与速度分的计算（见下文评分公式）

---

## 三、提供商评分机制

### 3.1 评分公式

```
Score = 健康门控 × (0.25 × 优先级分 + 0.30 × 速度分 + 0.35 × 成功率 + 0.10 × 稳定性分)
```

得分 ≤ 0 的提供商被直接排除。得分越高越优先使用。

### 3.2 健康门控（前置条件）

在计算四维评分之前，先检查两个前置条件，任一不满足则 score=0：

| 条件 | 判断逻辑 | 效果 |
|------|---------|------|
| **不健康** | `ScoreCard.health == false`（来自最近一次探测） | score=0, 原因: "unhealthy" |
| **冷却中** | 连续失败 ≥ `failure_threshold`(3) 且距上次失败 < `cooldown`(60s) | score=0, 原因: "cooldown (Xs remaining)" |

### 3.3 四维评分详解

#### ① 优先级分（权重 25%）

```
priority_score = 1 / priority     (最低 0.05)
```

- 来源：配置中每个模型下提供商的 `priority` 字段
- `priority=1` → 得分 1.0（最高）
- `priority=2` → 得分 0.5
- `priority=10` → 得分 0.1
- 含义：运维人员可以通过配置显式指定偏好，评分高的提供商优先被选中

#### ② 速度分（权重 30%）

```
speed_score = 1 / (1 + ttft)      (最低 0.05)
```

ttft 的取值逻辑（按优先级）：

| 情况 | 取值 | 说明 |
|------|------|------|
| 近 5 分钟真实请求 ≥ 10 条 | 真实请求平均 TTFT | 数据充足，完全信任实际表现 |
| 有真实请求但不足 10 条，且有探测数据 | 加权混合：`w × 实际TTFT + (1-w) × 探测TTFT`，w = 实际条数/10 | 逐步从探测数据过渡到实际数据 |
| 仅有探测数据 | 探测 TTFT | 冷启动场景，依赖探测 |
| 无任何数据 | 返回默认 0.5 | 新提供商兜底 |

#### ③ 成功率（权重 35%）

```
success_rate = 近 300 秒滑动窗口内成功请求数 / 总请求数
```

- 滑动窗口大小：最近 20 条请求，且在 300 秒内
- 无请求记录时：如果探测健康则 1.0，否则 0.0
- 这是权重最大的维度，意味着实际请求表现是最重要的评判标准

#### ④ 稳定性分（权重 10%）

```
stability_score = max(1.0 - 连续失败次数 / failure_threshold, 0)
```

- `failure_threshold` 默认 3
- 0 次连续失败 → 1.0
- 1 次 → 0.67
- 2 次 → 0.33
- ≥ 3 次 → 0.0（但此时已触发冷却，直接被门控排除）
- 含义：惩罚正在恶化中的提供商，在它被完全熔断之前就降低其优先级

### 3.4 评分数据来源汇总

```
                    ┌──────────────────────────┐
                    │       ScoreCard          │
                    │   (每个 model×provider)   │
                    ├──────────────────────────┤
  真实请求 ────────►│ 滑动窗口 (最近 20 条)     │──► 成功率、实际 TTFT
  record_success()  │ consecutive_failures     │──► 稳定性分、冷却判断
  record_failure()  │ last_failure_time        │
                    ├──────────────────────────┤
  后台探测 ────────►│ health (bool)            │──► 健康门控
  update_probe()    │ probe_ttft_ms            │──► 速度分（冷启动/混合）
                    ├──────────────────────────┤
  配置文件 ────────►│ priority                 │──► 优先级分
                    └──────────────────────────┘
                                │
                                ▼
                         Scorer.rank_providers()
                                │
                                ▼
                     按 score 降序排列提供商
                     score ≤ 0 的直接排除
```

### 3.5 评分示例

假设某模型有 3 个提供商，当前状态：

| 提供商 | 健康 | 优先级 | TTFT | 成功率 | 连续失败 | 最终得分 |
|-------|------|--------|------|--------|---------|---------|
| A | true | 1 | 0.8s | 95% | 0 | 0.25×1.0 + 0.30×0.56 + 0.35×0.95 + 0.10×1.0 = **0.85** |
| B | true | 2 | 1.5s | 80% | 1 | 0.25×0.5 + 0.30×0.40 + 0.35×0.80 + 0.10×0.67 = **0.59** |
| C | false | 1 | - | - | - | 健康门控=0 → **排除** |

最终排序：A(0.85) → B(0.59)，C 被排除。

---

## 四、数据如何流转：从请求/探测到下一次排序

### 4.1 全局数据流总览

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         ScoreCard (每个 model×provider 一份)              │
│                                                                          │
│   ┌─────────────────────────────┐    ┌──────────────────────────────┐    │
│   │  请求滑动窗口 (最近 20 条)    │    │  探测数据                      │    │
│   │  ─────────────────────       │    │  ──────                      │    │
│   │  [成功, ttft=0.5s]           │    │  health: true/false          │    │
│   │  [成功, ttft=0.8s]           │    │  probe_ttft_ms: 320          │    │
│   │  [失败, reason=timeout]      │    │  probe_time: 1712649600      │    │
│   │  ...                        │    │                              │    │
│   │                             │    │                              │    │
│   │  consecutive_failures: 1    │    │                              │    │
│   │  last_failure_time: ...     │    │                              │    │
│   └──────────┬──────────────────┘    └──────────────┬───────────────┘    │
│              │                                      │                    │
│              │         ┌────────────────────┐        │                    │
│              └────────►│  Scorer 评分计算    │◄───────┘                    │
│                        └────────┬───────────┘                            │
│                                 │                                        │
│                                 ▼                                        │
│                     score = 0.85 (用于下次排序)                            │
└──────────────────────────────────────────────────────────────────────────┘
```

### 4.2 实时请求记录 → 影响评分的完整路径

每次业务请求结束后，`RequestRecorder` 将结果写入 `ScoreCard`，影响该提供商在**后续请求中的排名**：

```
业务请求完成
    │
    ├─ 成功 ──► RequestRecorder.record_success()
    │              │
    │              ├─► Scorer.record_success(model, provider, ttft, tps, duration)
    │              │       │
    │              │       └─► ScoreCard.push_request(RequestOutcome(success=True, ttft=...))
    │              │               │
    │              │               ├─ 写入滑动窗口 → 影响「成功率」和「速度分(实际TTFT)」
    │              │               └─ consecutive_failures 重置为 0 → 影响「稳定性分」和「冷却判断」
    │              │
    │              └─► MetricsCollector 记录指标（REQUEST_TOTAL、TTFT、TPS、Token 等）
    │
    └─ 失败 ──► RequestRecorder.record_failure()
                   │
                   ├─► Scorer.record_failure(model, provider, reason)
                   │       │
                   │       └─► ScoreCard.push_request(RequestOutcome(success=False))
                   │               │
                   │               ├─ 写入滑动窗口 → 降低「成功率」
                   │               ├─ consecutive_failures += 1 → 降低「稳定性分」
                   │               └─ 若 consecutive_failures ≥ 3 → 触发「冷却」，提供商被完全排除
                   │
                   ├─► MetricsCollector 记录指标（REQUEST_TOTAL status=error、STRATEGY_TRIGGERED）
                   └─► AlertDispatcher 发送告警
```

**具体影响的评分维度**：

| 记录字段 | 写入位置 | 影响的评分维度 | 影响方式 |
|---------|---------|-------------|---------|
| `success=True/False` | 滑动窗口 | **成功率**（权重 35%） | `近 300 秒成功数 / 总数` |
| `ttft_seconds` | 滑动窗口 | **速度分**（权重 30%） | 近 5 分钟实际 TTFT 均值，条数 ≥10 则完全替代探测数据 |
| `consecutive_failures` | ScoreCard | **稳定性分**（权重 10%） | `1 - 连续失败次数/3`，越高越优先 |
| `consecutive_failures ≥ 3` | ScoreCard | **冷却门控** | score=0，提供商被完全排除 60 秒 |

### 4.3 后台探测记录 → 影响评分的完整路径

后台探测每 20 秒一轮，结果写入 `ScoreCard`，影响**健康门控**和**冷启动场景的速度分**：

```
后台探测完成
    │
    ├─ 成功 ──► HealthChecker._probe_one()
    │              │
    │              ├─► ScoreCard.update_probe(ProbeResult(health=True, ttft_ms=320))
    │              │       │
    │              │       ├─ health = true → 通过「健康门控」，允许参与排序
    │              │       └─ probe_ttft_ms = 320 → 参与「速度分」计算（冷启动或混合场景）
    │              │
    │              └─► MetricsCollector 记录（PROVIDER_HEALTH=1、PROBE_TTFT、PROBE_TOTAL）
    │
    └─ 失败 ──► HealthChecker._probe_one()
                   │
                   ├─► ScoreCard.update_probe(ProbeResult(health=False))
                   │       │
                   │       └─ health = false → 「健康门控」拦截，score=0，提供商被完全排除
                   │
                   ├─► MetricsCollector 记录（PROVIDER_HEALTH=0、PROBE_TOTAL status=error）
                   └─► AlertDispatcher 发送告警（probe_failed）
```

**具体影响的评分维度**：

| 记录字段 | 写入位置 | 影响的评分维度 | 影响方式 |
|---------|---------|-------------|---------|
| `health` | ScoreCard | **健康门控**（前置条件） | `false` → score=0，直接排除 |
| `probe_ttft_ms` | ScoreCard | **速度分**（权重 30%） | 仅在实际请求不足时使用（见 4.4） |

### 4.4 速度分中探测数据与实际数据的混合策略

速度分的 TTFT 取值随实际请求量动态调整，逐步从"依赖探测"过渡到"信任实际"：

```
近 5 分钟实际请求条数
    │
    ├─ ≥ 10 条 ────────────────► 完全使用实际 TTFT（探测数据被忽略）
    │                             speed_score = 1 / (1 + 实际TTFT)
    │
    ├─ 1~9 条，且有探测数据 ──► 加权混合
    │                             w = 实际条数 / 10
    │                             ttft = w × 实际TTFT + (1-w) × 探测TTFT
    │                             speed_score = 1 / (1 + ttft)
    │
    ├─ 0 条，有探测数据 ────────► 完全使用探测 TTFT
    │                             speed_score = 1 / (1 + 探测TTFT)
    │
    └─ 0 条，无探测数据 ────────► 返回默认 0.5（新提供商兜底）
```

### 4.5 完整的请求生命周期：从发起到影响下一次排序

以一次完整的业务请求为例，展示数据如何流转并影响后续请求：

```
时刻 T：业务发起请求
    │
    │  ① Engine 调用 Scorer.rank_providers()
    │     读取所有提供商的 ScoreCard，按复合评分排序
    │     → A(0.85) > B(0.59)，选择 A
    │
    │  ② 向 Provider A 发起请求
    │     → 超时（8 秒未返回首 Token）
    │
    │  ③ 记录失败
    │     ScoreCard[A].push_request(success=False)
    │     ScoreCard[A].consecutive_failures: 0 → 1
    │     → A 的成功率下降，稳定性分下降
    │
    │  ④ 切换到 Provider B
    │     → 成功，TTFT=1.2s
    │
    │  ⑤ 记录成功
    │     ScoreCard[B].push_request(success=True, ttft=1.2s)
    │     ScoreCard[B].consecutive_failures: 重置为 0
    │     → B 的成功率上升，实际 TTFT 被记录
    │
    ▼
时刻 T+1：下一次请求
    │
    │  ① Engine 再次调用 Scorer.rank_providers()
    │     A 的分数因为刚才的失败记录而下降
    │     B 的分数因为刚才的成功记录而上升
    │     → 可能变成 B(0.72) > A(0.68)，B 被优先选择
    │
    │  同时，后台探测可能在 T 到 T+1 之间完成了一轮：
    │     → 如果探测发现 A 恢复健康，A 的评分会逐步回升
    │     → 如果探测发现 A 持续失败，A 可能被健康门控排除
```

### 4.6 两条数据通路的对比

| 维度 | 实时请求记录 | 后台探测记录 |
|------|-----------|-----------|
| **写入组件** | RequestRecorder | HealthChecker |
| **触发时机** | 每次业务请求结束 | 每 20 秒一轮 |
| **写入 ScoreCard 字段** | 滑动窗口、consecutive_failures | health、probe_ttft_ms |
| **影响的评分维度** | 成功率（35%）、速度分（30%，实际 TTFT）、稳定性（10%） | 健康门控（前置条件）、速度分（30%，冷启动/混合） |
| **影响的时效性** | 即时生效（下一次 rank_providers 就能看到） | 即时生效（下一次 rank_providers 就能看到） |
| **核心作用** | 反映提供商对**真实业务请求**的表现 | 反映提供商的**基础可用性** |
| **数据关系** | 请求量充足时完全替代探测数据（速度分） | 请求量不足或冷启动时提供兜底数据 |

---

## 五、相关配置项速查

```yaml
health_check:
  interval: 20          # 探测间隔（秒）
  failure_threshold: 3  # 连续失败几次触发冷却
  recovery_threshold: 3 # 连续成功几次恢复（探测层面）
  cooldown: 60          # 冷却时长（秒）
  timeout: 10           # 单次探测超时
  probe_max_tokens: 5   # 探测请求的 max_tokens
  stagger_interval: 2   # 各目标间探测间隔（避免并发）

strategies:
  my_scenario:
    mode: stream        # stream | block
    primary: claude-opus-4-5-20251101
    fallback:
      - gpt-4.1-2025-04-14
    timeout:
      ttft: 8           # 首 Token 超时（秒）
      chunk_gap: 15     # 流中断超时（秒）
      total: 60         # 总超时（秒）
    empty_frame_threshold: 5    # 连续空帧阈值
    slow_speed_threshold: 5.0   # 慢速阈值（token/s）
```

---

## 六、监控数据采集

### 6.1 采集架构

```
                          采集点
                            │
          ┌─────────────────┼──────────────────┐
          │                 │                  │
    RequestRecorder    HealthChecker        Scorer
    (请求级指标)       (系统级指标)        (评分指标)
          │                 │                  │
          └────────┬────────┘                  │
                   │                           │
                   ▼                           │
          MetricsCollector ◄───────────────────┘
          (3 个泛型方法)
           inc() / observe() / set()
                   │
         ┌─────────┴─────────┐
         │                   │
   SimpleCollector    PrometheusCollector
   (内存字典)          (Prometheus SDK)
```

整个库只有 3 个组件往 MetricsCollector 写数据：

| 组件 | 职责 | 写入的指标 |
|------|------|-----------|
| **RequestRecorder** | 每次请求成功/失败时集中记录 | 请求总数、耗时、TTFT、TPS、Token 消耗、策略触发 |
| **HealthChecker** | 每次后台探测完成后记录 | 提供商健康状态、探测 TTFT、探测总数 |
| **Scorer** | 每次排序提供商时记录 | 提供商评分 |

### 6.2 指标全景

共 12 个内置指标，分两类：

#### 请求级指标（跟随每次 LLM 调用产生）

| 指标名 | 类型 | 额外标签 | 采集时机 | 说明 |
|--------|------|---------|---------|------|
| `llm_request_total` | Counter | `status` | 每次请求结束 | 请求计数，status=success\|error |
| `llm_request_duration_seconds` | Histogram | — | 请求成功时 | 请求总耗时（秒） |
| `llm_ttft_seconds` | Histogram | — | 收到首个有内容的 chunk | 首 Token 延迟（秒） |
| `llm_tokens_per_second` | Histogram | — | 流式请求成功时 | Token 吞吐量 |
| `llm_input_tokens_total` | Counter | — | 请求成功时 | 输入（prompt）Token 累计 |
| `llm_output_tokens_total` | Counter | — | 请求成功时 | 输出（completion）Token 累计 |
| `llm_fallback_total` | Counter | `from_provider`, `to_provider`, `reason` | 发生降级时 | 降级事件计数 |
| `llm_strategy_triggered_total` | Counter | `strategy` | 策略检测到异常时 | 策略触发计数 |

请求级指标的标签结构：`model` + `provider` + 配置中 `label_keys` 声明的自定义标签 + 该指标自身的 `extra_labels`。

#### 系统级指标（跟随后台探测 / 评分产生）

| 指标名 | 类型 | 标签 | 采集时机 | 说明 |
|--------|------|------|---------|------|
| `llm_provider_health` | Gauge | `provider` | 每次探测完成 | 1=健康, 0=不健康 |
| `llm_provider_score` | Gauge | `model`, `provider` | 每次请求排序时 | 复合评分（0~1） |
| `llm_probe_ttft_seconds` | Histogram | `provider` | 探测成功时 | 探测首 Token 延迟 |
| `llm_probe_total` | Counter | `provider`, `status` | 每次探测完成 | 探测计数，status=success\|error |

系统级指标不携带自定义 `label_keys`，因为后台探测没有业务调用上下文。

### 6.3 标签体系

```
                    请求级指标的标签组成
┌──────────────────────────────────────────────────────┐
│                                                      │
│  固定标签          自定义标签            指标专属标签  │
│  ──────           ────────            ──────────    │
│  model            (来自 label_keys    (来自指标定义   │
│  provider          配置声明)           extra_labels) │
│                                                      │
│  例:               例:                 例:           │
│  model=gpt-4      module=chat         status=success │
│  provider=openai  env=prod            strategy=ttft  │
│                                                      │
└──────────────────────────────────────────────────────┘
```

**标签生命周期**：

1. 配置文件中 `metrics.label_keys` 声明所有可能出现的自定义标签名
2. Prometheus 后端启动时，按 `固定标签 + label_keys + extra_labels` 注册仪表
3. 业务调用时通过 `labels={"module": "chat", "env": "prod"}` 传入实际值
4. 未传入的自定义标签默认填空字符串 `""`

### 6.4 Token 数据采集链路

Token 消耗数据从 SDK 一路透传到指标：

```
OpenAI SDK                          Anthropic SDK
    │                                    │
    │ stream_options=                     │ message_start → input_tokens
    │   {include_usage: true}             │ message_delta → output_tokens
    │                                    │
    ▼                                    ▼
OpenAIProvider                     AnthropicProvider
    │                                    │
    │ 构造 TokenUsage(                    │ 构造 TokenUsage(
    │   prompt_tokens=...,               │   prompt_tokens=input_tokens,
    │   completion_tokens=...,           │   completion_tokens=output_tokens,
    │   total_tokens=...                 │   total_tokens=...
    │ )                                  │ )
    │                                    │
    └──────────────┬─────────────────────┘
                   │
                   ▼
            StreamChunk.usage / ChatResponse.usage
                   │
                   ▼
            StreamMonitor / Engine
            (捕获 final_usage)
                   │
                   ▼
            RequestRecorder.record_success()
                   │
                   ├─► metrics.inc(INPUT_TOKENS, usage.prompt_tokens, ...)
                   └─► metrics.inc(OUTPUT_TOKENS, usage.completion_tokens, ...)
```

> 流式场景中，如果 SDK 返回了真实 usage（最后一个 chunk 携带），则使用真实值；否则按 `len(content) // 4` 估算 output_tokens。

### 6.5 两种 Metrics 后端

| 后端 | 适用场景 | 依赖 | 数据持久性 |
|------|---------|------|-----------|
| **SimpleCollector** | 开发/测试/轻量部署 | 无 | 进程内存，重启丢失 |
| **PrometheusCollector** | 生产环境 | `prometheus_client` | Prometheus 拉取后持久化 |

**SimpleCollector** 内部结构：

```python
_counters:    {"llm_request_total": {(("model","gpt-4"), ("provider","openai"), ("status","success")): 42.0}}
_histograms:  {"llm_ttft_seconds": {(("model","gpt-4"), ("provider","openai")): [0.5, 0.8, 0.3]}}
_gauges:      {"llm_provider_health": {(("provider","openai"),): 1.0}}
```

可通过 `collector.get_summary()` 获取全量快照，用于调试。

**PrometheusCollector** 自动注册逻辑：

```
启动时遍历 ALL_METRICS (12 个 MetricDef)
    │
    ├─ 计算标签列表：
    │   REQUEST scope → ["model", "provider"] + label_keys + extra_labels
    │   SYSTEM scope  → extra_labels
    │
    ├─ 按 kind 创建对应的 Prometheus 仪表：
    │   COUNTER   → prometheus_client.Counter(name, desc, labels)
    │   HISTOGRAM → prometheus_client.Histogram(name, desc, labels)
    │   GAUGE     → prometheus_client.Gauge(name, desc, labels)
    │
    └─ 存入 _instruments[name] 字典
```

### 6.6 指标扩展机制

新增一个指标只需两步，无需修改任何后端代码：

```python
# 第 1 步：在 metrics/registry.py 声明
MY_NEW_METRIC = MetricDef(
    "llm_my_new_metric",
    MetricKind.HISTOGRAM,
    "My new metric description",
    extra_labels=("some_label",),
)

# 别忘了加入 ALL_METRICS 元组

# 第 2 步：在采集点调用
metrics.observe(MY_NEW_METRIC, value, model=model, provider=provider, some_label="xxx")
```

SimpleCollector 和 PrometheusCollector 都会自动处理这个新指标——因为它们只有 `inc()`/`observe()`/`set()` 三个泛型方法，不关心具体是哪个指标。

---

## 七、告警机制

### 7.1 告警架构

```
触发源                         AlertDispatcher                    通道
─────                         ───────────────                    ────
RequestRecorder ──┐                 │
  (提供商降级)      │    dispatch()   │    AlertRules         ┌─► LogChannel (日志)
                   ├──────────────► Queue ──► should_send() ──┼─► FeishuChannel (飞书)
HealthChecker ────┘                 │    (去重/静默)          └─► WebhookChannel (通用)
  (探测失败)        │                │
                   │  后台消费任务    │
Engine ───────────┘                 │
  (全部提供商耗尽)                    │
```

### 7.2 告警事件结构

每个告警事件包含以下字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `level` | string | `info` \| `warning` \| `critical` |
| `title` | string | 告警标题，如 `provider_degraded:ttft_timeout`、`probe_failed`、`all_providers_exhausted` |
| `message` | string | 详细描述 |
| `provider` | string? | 相关提供商（可选） |
| `model` | string? | 相关模型（可选） |
| `timestamp` | float | 事件时间戳 |

### 7.3 告警触发场景

| 场景 | level | title | 触发组件 |
|------|-------|-------|---------|
| 单个提供商请求失败/超时/策略触发 | warning | `provider_degraded:{reason}` | RequestRecorder |
| 后台探测失败 | warning | `probe_failed` | HealthChecker |
| 所有提供商（含 fallback）全部耗尽 | critical | `all_providers_exhausted` | Engine |

### 7.4 去重与静默

通过 `AlertRules` 实现，避免同一问题短时间内重复告警：

- **去重键**：`(title, provider)` 二元组
- **静默窗口**：配置中 `silence_seconds`（默认 60 秒）
- **逻辑**：同一个 (title, provider) 在静默窗口内只发送第一次，后续被抑制

```
时间线示例（silence_seconds=60）：

00:00  provider_degraded:ttft_timeout @ openai  → ✅ 发送
00:05  provider_degraded:ttft_timeout @ openai  → ❌ 静默（距上次 5s < 60s）
00:10  provider_degraded:ttft_timeout @ ppio    → ✅ 发送（不同 provider，不同 key）
01:05  provider_degraded:ttft_timeout @ openai  → ✅ 发送（距上次 65s > 60s）
```

### 7.5 三种告警通道

#### Log 通道（默认兜底）

输出到 Python logging，无外部依赖：

```
[WARNING] provider_degraded:ttft_timeout | Provider openai degraded for model gpt-4: ... (provider=openai, model=gpt-4)
```

#### 飞书 Webhook 通道

POST 到飞书机器人：

```json
{
  "msg_type": "text",
  "content": {
    "text": "[WARNING] provider_degraded:ttft_timeout\nProvider openai degraded...\nprovider: openai | model: gpt-4"
  }
}
```

#### 通用 Webhook 通道

POST 完整的 AlertEvent JSON，可对接任意告警平台：

```json
{
  "level": "warning",
  "title": "provider_degraded:ttft_timeout",
  "message": "Provider openai degraded for model gpt-4: ...",
  "provider": "openai",
  "model": "gpt-4",
  "timestamp": 1712649600.0
}
```

### 7.6 告警配置

```yaml
alert:
  channels:
    - type: log                    # 始终保留，作为兜底
    - type: feishu
      webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
    - type: webhook
      url: "https://your-alert-platform.com/api/alert"
  rules:
    silence_seconds: 60            # 同一 (title, provider) 60 秒内不重复
```

> 如果未配置任何通道，默认自动添加 Log 通道，确保告警不会被静默吞掉。
