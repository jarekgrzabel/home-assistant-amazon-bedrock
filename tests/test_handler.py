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
    monkeypatch.setenv("API_KEYS", "test-key,abc123")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
    monkeypatch.setenv("DEFAULT_MODEL_ID", "eu.amazon.nova-pro-v1:0")
    monkeypatch.setenv("MAX_HISTORY_MESSAGES", "20")
    monkeypatch.delenv("SESSION_TABLE_NAME", raising=False)
    monkeypatch.delenv("SESSION_TTL_SECONDS", raising=False)
    handler._CONVERSATION_STORE = None


def test_build_bedrock_request_converts_messages():
    payload = {
        "model": "eu.amazon.nova-lite-v1:0",
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": "sys"}]},
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "abc",
                        "type": "function",
                        "function": {
                            "name": "my_tool",
                            "arguments": '{"foo": "bar"}'
                        }
                    }
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
    assistant_content = request["messages"][1]["content"][0]
    assert "toolUse" in assistant_content
    assert assistant_content["toolUse"]["name"] == "my_tool"
    assert assistant_content["toolUse"]["input"] == {"foo": "bar"}
    assert request["messages"][2]["content"][0]["toolResult"]["toolUseId"] == "abc"
    assert request["inferenceConfig"] == {"temperature": 0.3, "maxTokens": 256}
    assert request["sessionState"] == {"metadata": {"session": "123"}}


def test_build_bedrock_request_requires_matching_tool_message():
    payload = {
        "model": "eu.amazon.nova-lite-v1:0",
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "abc",
                        "type": "function",
                        "function": {
                            "name": "my_tool",
                            "arguments": '{"foo": "bar"}'
                        }
                    }
                ]
            },
            {
                "role": "tool",
                "tool_call_id": "missing",
                "content": [{"type": "text", "text": "done"}]
            },
        ],
    }

    with pytest.raises(handler.ProxyError):
        handler.build_bedrock_request(payload)


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


def test_build_tool_config_ignores_none_choice():
    payload = {
        "model": "eu.amazon.nova-lite-v1:0",
        "messages": [{"role": "user", "content": "status"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "execute_services",
                    "parameters": {"type": "object"}
                }
            }
        ],
        "tool_choice": "none"
    }

    request = handler.build_bedrock_request(payload)

    tool_config = request["toolConfig"]
    assert "toolChoice" not in tool_config








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
    assert "tools" not in prepared




def test_prepare_payload_keeps_auto_after_confirmation():
    payload = {
        "model": "eu.amazon.nova-lite-v1:0",
        "messages": [
            {"role": "assistant", "content": "Czy mam wyłączyć światło w biurze? Potwierdzasz?"},
            {"role": "user", "content": "tak"}
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

    assert prepared.get("_tools_disabled") is None
    assert prepared["tool_choice"] == "auto"

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

    assert prepared.get("_tools_disabled") is None
    assert prepared["tool_choice"] == "auto"
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


def test_map_response_chat_mode_empty_text_returns_empty_string():
    bedrock_response = {
        "responseId": "r-789",
        "output": [
            {
                "role": "assistant",
                "content": [{"text": ""}],
            }
        ],
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
    }
    payload = {"messages": []}
    chat_response = handler.map_response("eu.amazon.nova-lite-v1:0", payload, bedrock_response, mode="chat")

    assert chat_response["choices"][0]["message"]["content"] == ""
    assert chat_response["choices"][0]["finish_reason"] == "stop"


def test_map_response_drops_tool_calls_when_tools_disabled():
    payload = {"model": "eu.amazon.nova-lite-v1:0", "_tools_disabled": True}
    bedrock_response = {
        "responseId": "r-state",
        "output": [
            {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "abc",
                            "name": "execute_services",
                            "input": {
                                "list": [
                                    {
                                        "domain": "light",
                                        "service": "turn_on",
                                        "service_data": {"entity_id": "switch.biuro_l1"}
                                    }
                                ]
                            }
                        }
                    }
                ],
            }
        ],
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
    }

    result = handler.map_response("eu.amazon.nova-lite-v1:0", payload, bedrock_response)

    assert result["tool_calls"] == []


def test_map_tool_calls_normalizes_domains():
    payload = {"model": "eu.amazon.nova-lite-v1:0"}
    bedrock_response = {
        "responseId": "r-action",
        "output": [
            {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tu1",
                            "name": "execute_services",
                            "input": {
                                "list": [
                                    {
                                        "domain": "light",
                                        "service": "turn_on",
                                        "service_data": {"entity_id": "switch.biuro_l1"}
                                    }
                                ]
                            }
                        }
                    }
                ],
            }
        ],
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
    }

    result = handler.map_response("eu.amazon.nova-lite-v1:0", payload, bedrock_response)

    assert len(result["tool_calls"]) == 1
    args = json.loads(result["tool_calls"][0]["function"]["arguments"])
    assert args["list"][0]["domain"] == "switch"
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
    assert "session_id" not in body

    args, kwargs = mock_client.converse.call_args
    assert "sessionId" not in kwargs


def test_lambda_handler_handles_models_list():
    event = {
        "headers": {"Authorization": "Bearer test-key"},
        "requestContext": {"http": {"method": "GET", "path": "/prod/v1//models"}},
    }

    response = handler.lambda_handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["object"] == "list"
    assert body["data"][0]["id"] == "eu.amazon.nova-pro-v1:0"


def test_lambda_handler_handles_models_detail():
    event = {
        "headers": {"Authorization": "Bearer test-key"},
        "requestContext": {
            "http": {
                "method": "GET",
                "path": "/prod/v1//models/eu.amazon.nova-pro-v1:0",
            }
        },
    }

    response = handler.lambda_handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["object"] == "model"
    assert body["id"] == "eu.amazon.nova-pro-v1:0"


def test_lambda_handler_normalizes_double_slash_for_chat(monkeypatch):
    mock_client = MagicMock()
    mock_client.converse.return_value = {
        "responseId": "r-999",
        "output": [
            {
                "role": "assistant",
                "content": [
                    {"text": "sure"},
                    {
                        "toolUse": {
                            "toolUseId": "call-42",
                            "name": "execute_services",
                            "input": {"entity_id": "light.office"},
                        }
                    },
                ],
            }
        ],
        "usage": {"inputTokens": 5, "outputTokens": 7, "totalTokens": 12},
    }
    monkeypatch.setattr(handler, "bedrock_client", lambda: mock_client)

    event = {
        "headers": {"Authorization": "Bearer test-key"},
        "requestContext": {
            "http": {"method": "POST", "path": "/prod/v1//chat/completions"}
        },
        "body": json.dumps(
            {
                "model": "eu.amazon.nova-lite-v1:0",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "Toggle"}]}
                ],
            }
        ),
    }

    response = handler.lambda_handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["finish_reason"] == "tool_calls"


def test_lambda_handler_persists_history(monkeypatch):
    class FakeStore:
        enabled = True

        def __init__(self):
            self.saved_history = None
            self.deleted = []
            self.loaded_for = None

        def load(self, conversation_id):
            self.loaded_for = conversation_id
            return [
                {"role": "user", "content": "previous question"},
                {"role": "assistant", "content": "previous answer"},
            ]

        def save(self, conversation_id, history):
            self.saved_history = (conversation_id, history)

        def delete(self, conversation_id):
            self.deleted.append(conversation_id)

    fake_store = FakeStore()
    handler._CONVERSATION_STORE = fake_store

    mock_client = MagicMock()
    mock_client.converse.return_value = {
        "responseId": "r-321",
        "output": [
            {
                "role": "assistant",
                "content": [
                    {"text": "Hello again"},
                ],
            }
        ],
        "usage": {"inputTokens": 5, "outputTokens": 5, "totalTokens": 10},
    }
    monkeypatch.setattr(handler, "bedrock_client", lambda: mock_client)

    event = {
        "headers": {"Authorization": "Bearer test-key"},
        "requestContext": {"http": {"method": "POST", "path": "/prod/v1/responses"}},
        "body": json.dumps(
            {
                "model": "eu.amazon.nova-lite-v1:0",
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                ],
                "metadata": {"conversation_id": "conv-1"},
            }
        ),
    }

    response = handler.lambda_handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["conversation_id"] == "conv-1"
    assert fake_store.loaded_for == "conv-1"
    saved_conv_id, saved_history = fake_store.saved_history
    assert saved_conv_id == "conv-1"
    assert saved_history[-2] == {"role": "user", "content": "hi"}
    assert saved_history[-1] == {"role": "assistant", "content": "Hello again"}
    assert fake_store.deleted == []


def test_lambda_handler_clears_history_on_flag(monkeypatch):
    class FakeStore:
        enabled = True

        def __init__(self):
            self.deleted = []
            self.saved_history = None

        def load(self, conversation_id):
            return [{"role": "user", "content": "old"}]

        def save(self, conversation_id, history):
            self.saved_history = (conversation_id, history)

        def delete(self, conversation_id):
            self.deleted.append(conversation_id)

    fake_store = FakeStore()
    handler._CONVERSATION_STORE = fake_store

    mock_client = MagicMock()
    mock_client.converse.return_value = {
        "responseId": "r-clear",
        "output": [
            {"role": "assistant", "content": [{"text": "Fresh start"}]},
        ],
        "usage": {"inputTokens": 3, "outputTokens": 4, "totalTokens": 7},
    }
    monkeypatch.setattr(handler, "bedrock_client", lambda: mock_client)

    event = {
        "headers": {"Authorization": "Bearer test-key"},
        "requestContext": {"http": {"method": "POST", "path": "/prod/v1/responses"}},
        "body": json.dumps(
            {
                "model": "eu.amazon.nova-lite-v1:0",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "start over"}]}
                ],
                "metadata": {"conversation_id": "conv-clear", "clear_conversation": True},
            }
        ),
    }

    response = handler.lambda_handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["conversation_id"] == "conv-clear"
    assert fake_store.deleted == ["conv-clear"]
    saved_conv_id, saved_history = fake_store.saved_history
    assert saved_conv_id == "conv-clear"
    assert saved_history[0] == {"role": "user", "content": "start over"}
    assert saved_history[-1] == {"role": "assistant", "content": "Fresh start"}


def test_conversation_id_falls_back_to_authorization(monkeypatch):
    class CaptureStore:
        enabled = True

        def __init__(self):
            self.loaded = None
            self.saved = None

        def load(self, conversation_id):
            self.loaded = conversation_id
            return []

        def save(self, conversation_id, history):
            self.saved = (conversation_id, history)

        def delete(self, conversation_id):
            pass

    capture_store = CaptureStore()
    handler._CONVERSATION_STORE = capture_store

    mock_client = MagicMock()
    mock_client.converse.return_value = {
        "responseId": "r-auth",
        "output": [{"role": "assistant", "content": [{"text": "hello"}]}],
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
    }
    monkeypatch.setattr(handler, "bedrock_client", lambda: mock_client)

    event = {
        "headers": {"Authorization": "Bearer abc123"},
        "requestContext": {"http": {"method": "POST", "path": "/prod/v1/responses"}},
        "body": json.dumps(
            {
                "model": "eu.amazon.nova-lite-v1:0",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            }
        ),
    }

    response = handler.lambda_handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert capture_store.loaded.startswith("auth:")
    assert capture_store.saved[0] == capture_store.loaded
    assert body["conversation_id"].startswith("auth:")
