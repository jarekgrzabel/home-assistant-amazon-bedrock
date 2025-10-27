import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src import handler


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("API_KEYS", "test-key")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")


def test_build_bedrock_request_converts_messages():
    payload = {
        "model": "eu.amazon.nova-lite-v1:0",
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": "sys"}]},
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_call", "tool_call": {"name": "my_tool", "arguments": {"foo": "bar"}, "id": "abc"}}
                ]
            },
            {
                "role": "tool",
                "tool_call_id": "abc",
                "content": [{"type": "text", "text": "done"}]
            },
        ],
        "temperature": 0.3,
        "max_output_tokens": 256,
        "metadata": {"session": "123"},
        "inference_profile_id": "arn:aws:bedrock:eu-central-1:123456789012:inference-profile/my-profile",
    }

    request = handler.build_bedrock_request(payload)

    assert request["inferenceProfileId"] == "arn:aws:bedrock:eu-central-1:123456789012:inference-profile/my-profile"
    assert request["system"] == [{"text": "sys"}]
    assert request["messages"][0]["role"] == "user"
    assert request["messages"][1]["role"] == "assistant"
    assert request["messages"][1]["content"][0]["toolUse"]["name"] == "my_tool"
    assert request["messages"][2]["content"][0]["toolResult"]["toolUseId"] == "abc"
    assert request["inferenceConfig"] == {"temperature": 0.3, "maxTokens": 256}
    assert request["sessionState"] == {"metadata": {"session": "123"}}


def test_build_tool_config_from_payload():
    payload = {
        "model": "eu.amazon.nova-lite-v1:0",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "turn_on",
                    "description": "Turn on device",
                    "parameters": {"type": "object", "properties": {"entity_id": {"type": "string"}}},
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "turn_on"}},
    }

    request = handler.build_bedrock_request(payload)

    tool_config = request["toolConfig"]
    assert tool_config["tools"][0]["toolSpec"]["name"] == "turn_on"
    assert tool_config["tools"][0]["toolSpec"]["inputSchema"]["json"]["type"] == "object"
    assert tool_config["toolChoice"] == {"tool": {"name": "turn_on"}}








def test_prepare_payload_sets_tool_choice_to_none_for_question():
    payload = {
        "model": "eu.amazon.nova-lite-v1:0",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Czy światło w biurze się świeci?"}
        ],
        "functions": [
            {
                "name": "execute_services",
                "description": "Execute",
                "parameters": {"type": "object", "properties": {}}
            }
        ],
    }

    prepared = handler.prepare_payload(payload)

    assert prepared.get("_tools_disabled") is True
    assert "tool_choice" not in prepared


def test_prepare_payload_keeps_auto_for_action_request():
    payload = {
        "model": "eu.amazon.nova-lite-v1:0",
        "messages": [
            {"role": "user", "content": "Włącz światło w biurze"}
        ],
        "functions": [
            {
                "name": "execute_services",
                "description": "Execute",
                "parameters": {"type": "object", "properties": {}}
            }
        ],
    }

    prepared = handler.prepare_payload(payload)

    assert prepared["tool_choice"] == "auto"
def test_prepare_payload_sets_auto_tool_choice():
    payload = {
        "model": "eu.amazon.nova-lite-v1:0",
        "messages": [{"role": "user", "content": "hi"}],
        "functions": [
            {
                "name": "turn_on",
                "description": "turn something on",
                "parameters": {"type": "object", "properties": {"entity_id": {"type": "string"}}},
            }
        ],
    }

    prepared = handler.prepare_payload(payload)

    assert prepared.get("_tools_disabled") is True
    assert "tool_choice" not in prepared
    assert prepared["tools"][0]["function"]["name"] == "turn_on"
def test_prepare_payload_from_chat_completion():
    payload = {
        "model": "eu.amazon.nova-lite-v1:0",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "functions": [
            {
                "name": "do_it",
                "description": "test function",
                "parameters": {"type": "object", "properties": {"foo": {"type": "string"}}}
            }
        ],
        "function_call": {"name": "do_it"},
        "max_tokens": 123,
    }

    prepared = handler.prepare_payload(payload)

    assert prepared["tools"][0]["function"]["name"] == "do_it"
    assert prepared["tool_choice"] == {"type": "function", "function": {"name": "do_it"}}
    assert prepared["max_output_tokens"] == 123


def test_map_response_chat_mode():
    bedrock_response = {
        "responseId": "r-456",
        "output": ["cześć"],
        "usage": {"inputTokens": 2, "outputTokens": 4, "totalTokens": 6},
    }
    payload = {"messages": []}
    chat_response = handler.map_response("eu.amazon.nova-lite-v1:0", payload, bedrock_response, mode="chat")

    assert chat_response["object"] == "chat.completion"
    assert chat_response["choices"][0]["message"]["content"] == "cześć"
    assert chat_response["usage"]["total_tokens"] == 6
def test_lambda_handler_returns_openai_shape(monkeypatch):
    mock_client = MagicMock()
    mock_client.converse.return_value = {
        "responseId": "r-123",
        "output": [
            {
                "role": "assistant",
                "content": [
                    {"text": "hello"},
                    {
                        "toolUse": {
                            "toolUseId": "call-1",
                            "name": "turn_on",
                            "input": {"entity_id": "light.kitchen"},
                        }
                    },
                ],
            }
        ],
        "usage": {"inputTokens": 12, "outputTokens": 24, "totalTokens": 36},
    }
    monkeypatch.setattr(handler, "bedrock_client", lambda: mock_client)

    event = {
        "headers": {"Authorization": "Bearer test-key"},
        "body": json.dumps(
            {
                "model": "eu.amazon.nova-lite-v1:0",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "Turn on"}]}
                ],
            }
        ),
    }

    response = handler.lambda_handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["id"] == "r-123"
    assert body["usage"]["total_tokens"] == 36
    assert body["tool_calls"][0]["function"]["name"] == "turn_on"
    mock_client.converse.assert_called_once()
