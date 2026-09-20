# kuafu-llm-infra

OpenAI 兼容的统一 LLM 调用入口，内部按配置做多提供商降级。

支持四种协议：`openai`（Chat Completions）、`openai_responses`（Responses）、`anthropic`、`google`。
入参统一用 OpenAI chat 格式，各协议的转换在 provider 层完成。

## 安装

```bash
pip install kuafu-llm-sdk
```

## 使用

```python
from kuafu_llm_infra import create_client

client = create_client("llm_stability.yaml")                 # 本地配置
client = create_client(redis_url="redis://host:6379/0")       # 从 Redis 读配置，后台每 10s 拉取

# 非流式
response = await client.chat.completions.create(
    business_key="chat",
    messages=[{"role": "user", "content": "hello"}],
)
print(response.choices[0].message.content)
print(response.choices[0].message.reasoning_content)   # 模型思考内容（有则透传）
print(response.usage)

# 流式
stream = await client.chat.completions.create(business_key="chat", messages=[...], stream=True)
async for chunk in stream:
    print(chunk.choices[0].delta.content, end="")

# 工具调用：tools / tool_choice 用 OpenAI 格式，返回的 tool_calls 也是 OpenAI 格式
response = await client.chat.completions.create(business_key="chat", messages=[...], tools=[...])
for tc in response.choices[0].message.tool_calls or []:
    print(tc.id, tc.function.name, tc.function.arguments)

# 热更新：写入 Redis 并立即生效
await client.push_config(new_config_dict)

await client.shutdown()
```

调用失败抛 `kuafu_llm_infra.AllProvidersExhausted`，消息里带每个提供商的失败原因。

## 配置

```yaml
llm_stability:
  providers:                      # 提供商凭证，一个提供商可暴露多个协议端点
    ppio:
      api_key: "sk-xxx"
      endpoints:
        openai:
          base_url: "https://api.ppinfra.com/openai/v1"
        anthropic:
          base_url: "https://api.ppinfra.com/anthropic"

  models:                         # 模型 → 提供商列表，priority 越小越先尝试
    deepseek-v3:
      providers:
        - provider: ppio
          endpoint: openai
          model_id: "deepseek/deepseek-v3"   # 该提供商实际的模型 id，省略则同模型名
          priority: 1

  strategies:                     # business_key → 主模型 + 降级链
    chat:
      primary: deepseek-v3
      fallback: [gpt-4.1]
      timeout:
        per_request: 60           # 单次请求超时（秒）
```

完整示例见 `examples/llm_stability.yaml`。

## 降级逻辑

```
business_key → 模型链 [primary, fallback...]
  每个模型下的提供商按 priority 升序
    → 请求（per_request 超时）
    → 成功：返回
    → 任何异常：记日志，换下一个
全部失败 → AllProvidersExhausted
```

流式：已向用户输出正文后失败不再切换（避免重复输出），流正常结束。

## trace

每次请求生成一个 trace_id，写进该请求所有日志行（含 httpx / openai 等三方库的）。

```python
response = await client.chat.completions.create(business_key="chat", messages=[...])
print(response.trace_id)                       # 流式用 stream.trace_id

# 也可以自己传，和业务侧日志串起来
await client.chat.completions.create(..., trace_id="order-123")
```

查一次请求的全链路：

```bash
grep <trace_id> logs/*.log
```

## 日志

```python
from kuafu_llm_infra import setup_logging
setup_logging(log_dir="logs")   # infra.log = 库日志，third-party.log = SDK/HTTP 库日志
```

每次请求记录：模型链、每个提供商的成功/失败、耗时、token 数。

## 开发

```bash
uv sync
uv run pytest
```
