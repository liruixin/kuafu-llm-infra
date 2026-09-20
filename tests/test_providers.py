"""各 provider 的消息 / 工具格式转换测试。"""

from __future__ import annotations

from llm_provider_sdk.providers.anthropic_provider import AnthropicProvider
from llm_provider_sdk.providers.google_provider import GoogleProvider
from llm_provider_sdk.providers.openai_provider import _split_think_tag
from llm_provider_sdk.providers.openai_responses_provider import OpenAIResponsesProvider

MESSAGES = [
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "weather?"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"sh"}'}},
    ]},
    {"role": "tool", "tool_call_id": "c1", "content": '{"temp": 20}'},
    {"role": "assistant", "content": "20 degrees", "reasoning_content": "should be dropped"},
]
TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "d", "parameters": {"type": "object"}}}]


def test_responses_convert_messages():
    instructions, items = OpenAIResponsesProvider._convert_messages(MESSAGES)
    assert instructions == "be brief"
    assert items == [
        {"role": "user", "content": "weather?"},
        {"type": "function_call", "call_id": "c1", "name": "get_weather", "arguments": '{"city":"sh"}'},
        {"type": "function_call_output", "call_id": "c1", "output": '{"temp": 20}'},
        {"role": "assistant", "content": "20 degrees"},
    ]


def test_responses_convert_tools():
    assert OpenAIResponsesProvider._convert_tools(TOOLS) == [
        {"type": "function", "name": "get_weather", "description": "d", "parameters": {"type": "object"}},
    ]


def test_anthropic_convert_messages():
    system, converted = AnthropicProvider._convert_messages(MESSAGES)
    assert system == "be brief"
    assert converted == [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "get_weather", "input": {"city": "sh"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": '{"temp": 20}'}]},
        {"role": "assistant", "content": "20 degrees"},  # reasoning_content 被剥掉
    ]


def test_anthropic_merges_consecutive_tool_results():
    messages = [
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "content": "2"},
    ]
    _, converted = AnthropicProvider._convert_messages(messages)
    assert len(converted) == 1 and len(converted[0]["content"]) == 2


def test_google_convert_messages():
    system, contents = GoogleProvider._convert_messages(MESSAGES)
    assert system == "be brief"
    roles = [c.role for c in contents]
    assert roles == ["user", "model", "user", "model"]
    assert contents[1].parts[0].function_call.name == "get_weather"
    assert contents[2].parts[0].function_response.name == "get_weather"  # 从 tool_call_id 反查
    assert contents[2].parts[0].function_response.response == {"temp": 20}


def test_split_think_tag_across_frames():
    text, thought, in_think = _split_think_tag("a<think>b", False)
    assert (text, thought, in_think) == ("a", "b", True)
    text, thought, in_think = _split_think_tag("c</think>d", True)
    assert (text, thought, in_think) == ("d", "c", False)
    text, thought, in_think = _split_think_tag("<think>x</think>y", False)
    assert (text, thought, in_think) == ("y", "x", False)
