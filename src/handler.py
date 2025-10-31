import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import boto3  # type: ignore
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore
    BotoCoreError = ClientError = Exception  # type: ignore

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

PII_PATTERNS = [
    re.compile(r"(api|token|key)[=:]\s*[A-Za-z0-9-_]{8,}", flags=re.IGNORECASE),
    re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b"),
    re.compile(r"\b\+?\d{2,3}[\s-]?\d{3}[\s-]?\d{3}[\s-]?\d{3,4}\b"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
]


class ProxyError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def redact_pii(value: str) -> str:
    redacted = value
    for pattern in PII_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def get_api_keys() -> List[str]:
    raw = os.getenv("API_KEYS", "")
    return [key.strip() for key in raw.split(",") if key.strip()]


def extract_authorization(headers: Dict[str, Any]) -> str:
    authorization = headers.get("authorization") or headers.get("Authorization")
    if not authorization:
        raise ProxyError("Missing Authorization header", status_code=401)
    if not authorization.lower().startswith("bearer "):
        raise ProxyError("Authorization header must use Bearer scheme", status_code=401)
    return authorization.split(" ", 1)[1]


def validate_token(token: str) -> None:
    valid_keys = get_api_keys()
    if token not in valid_keys:
        raise ProxyError("Unauthorized", status_code=401)


def decode_body(event: Dict[str, Any]) -> Dict[str, Any]:
    body = event.get("body")
    if body is None:
        raise ProxyError("Request body is required")
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except Exception as exc:  # pragma: no cover
            raise ProxyError(f"Unable to decode body: {exc}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProxyError(f"Invalid JSON payload: {exc}")






def strip_thinking_content(value: str) -> str:
    if not value:
        return value
    cleaned = re.sub(r'<thinking>.*?</thinking>', '', value, flags=re.DOTALL)
    return cleaned.strip()


def normalize_path(path: Optional[str]) -> str:
    if not path:
        return ""
    normalized = re.sub(r'/+', '/', path)
    if len(normalized) > 1 and normalized.endswith('/'):
        normalized = normalized.rstrip('/')
    return normalized


def extract_api_path(path: str) -> str:
    marker = "/v1"
    if not path:
        return ""
    idx = path.find(marker)
    if idx == -1:
        return path
    return path[idx:]


def get_default_model_id() -> str:
    return (
        os.getenv("DEFAULT_MODEL_ID")
        or os.getenv("BEDROCK_MODEL_ID")
        or "eu.amazon.nova-lite-v1:0"
    )


def build_model_descriptor(model_id: str) -> Dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "system",
    }


def extract_text_fragments(content: Any) -> List[str]:
    fragments: List[str] = []
    if content is None:
        return fragments
    if isinstance(content, str):
        fragments.append(content)
    elif isinstance(content, dict):
        if "text" in content:
            fragments.append(str(content.get("text") or ""))
        elif "toolResult" in content:
            tool_result = content.get("toolResult") or {}
            status = tool_result.get("status", "")
            result_content = tool_result.get("content") or []
            for item in result_content:
                fragments.extend(extract_text_fragments(item))
            fragments.append(f"[tool_result status={status}]")
    elif isinstance(content, list):
        for item in content:
            fragments.extend(extract_text_fragments(item))
    return [frag for frag in fragments if frag]


def summarize_tool_calls(message: Dict[str, Any]) -> Optional[str]:
    summaries: List[str] = []
    for call in message.get("tool_calls") or []:
        name = call.get("function", {}).get("name")
        arguments = call.get("function", {}).get("arguments")
        summaries.append(f"[tool_call {name} args={arguments}]")
    return " ".join(summaries) if summaries else None


def message_to_plain_entry(message: Dict[str, Any]) -> Optional[Dict[str, str]]:
    role = message.get("role")
    if role == "system":
        return None
    if role not in {"user", "assistant", "tool"}:
        return None

    fragments: List[str] = []
    fragments.extend(extract_text_fragments(message.get("content")))

    if role == "assistant" and not fragments:
        summary = summarize_tool_calls(message)
        if summary:
            fragments.append(summary)

    if role == "tool":
        fragments.extend(extract_text_fragments(message.get("content")))
        role = "user"

    if not fragments:
        return None

    text = " ".join(fragments).strip()
    if not text:
        return None
    return {"role": role, "content": text}


def plain_entry_to_message(entry: Dict[str, str]) -> Dict[str, Any]:
    return {
        "role": entry.get("role", "user"),
        "content": [
            {
                "type": "text",
                "text": entry.get("content", "")
            }
        ]
    }


def append_plain_entry(history: List[Dict[str, str]], entry: Optional[Dict[str, str]]) -> None:
    if not entry:
        return
    if history and history[-1] == entry:
        return
    history.append(entry)


class NullConversationStore:
    enabled = False

    def load(self, conversation_id: str) -> List[Dict[str, str]]:
        return []

    def save(self, conversation_id: str, history: List[Dict[str, str]]) -> None:
        return None

    def delete(self, conversation_id: str) -> None:
        return None


class DynamoConversationStore:
    enabled = True

    def __init__(self, table_name: str, ttl_seconds: int, max_messages: int, region: Optional[str]):
        self.table_name = table_name
        self.ttl_seconds = ttl_seconds
        self.max_messages = max_messages
        client_kwargs: Dict[str, Any] = {}
        if region:
            client_kwargs["region_name"] = region
        self.client = boto3.client("dynamodb", **client_kwargs) if boto3 else None

    def load(self, conversation_id: str) -> List[Dict[str, str]]:
        if not self.client:
            LOGGER.warning("DynamoDB client unavailable; history disabled")
            return []
        try:
            response = self.client.get_item(
                TableName=self.table_name,
                Key={"conversation_id": {"S": conversation_id}},
                ConsistentRead=True,
            )
        except Exception as exc:
            LOGGER.warning("History lookup failed: %s", redact_pii(str(exc)))
            return []
        item = response.get("Item")
        if not item:
            return []
        ttl_value = item.get("ttl", {}).get("N")
        if ttl_value and int(ttl_value) < int(time.time()):
            self.delete(conversation_id)
            return []
        history_blob = item.get("history", {}).get("S")
        if not history_blob:
            return []
        try:
            history = json.loads(history_blob)
        except json.JSONDecodeError:
            return []
        if isinstance(history, list):
            plain_history: List[Dict[str, str]] = []
            for entry in history:
                if isinstance(entry, dict) and entry.get("role") in {"user", "assistant"}:
                    content = entry.get("content")
                    if isinstance(content, str):
                        append_plain_entry(plain_history, {"role": entry["role"], "content": content})
            return plain_history
        return []

    def save(self, conversation_id: str, history: List[Dict[str, str]]) -> None:
        if not self.client:
            return
        trimmed = history[-self.max_messages :]
        expires_at = int(time.time()) + self.ttl_seconds
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item={
                    "conversation_id": {"S": conversation_id},
                    "history": {"S": json.dumps(trimmed)},
                    "ttl": {"N": str(expires_at)},
                    "updated_at": {"N": str(int(time.time()))},
                },
            )
        except Exception as exc:
            LOGGER.warning("History update failed: %s", redact_pii(str(exc)))

    def delete(self, conversation_id: str) -> None:
        if not self.client:
            return
        try:
            self.client.delete_item(
                TableName=self.table_name,
                Key={"conversation_id": {"S": conversation_id}},
            )
        except Exception as exc:
            LOGGER.warning("History delete failed: %s", redact_pii(str(exc)))


_CONVERSATION_STORE: Optional[Any] = None


def conversation_store() -> Any:
    global _CONVERSATION_STORE
    if _CONVERSATION_STORE is not None:
        return _CONVERSATION_STORE
    table_name = os.getenv("SESSION_TABLE_NAME")
    if not table_name:
        _CONVERSATION_STORE = NullConversationStore()
        return _CONVERSATION_STORE
    ttl_raw = os.getenv("SESSION_TTL_SECONDS", "300")
    max_messages_raw = os.getenv("MAX_HISTORY_MESSAGES", "20")
    try:
        ttl_seconds = max(60, int(ttl_raw))
    except ValueError:
        ttl_seconds = 300
    try:
        max_messages = max(2, int(max_messages_raw))
    except ValueError:
        max_messages = 20
    if boto3 is None:
        LOGGER.warning("boto3 unavailable; disabling history store")
        _CONVERSATION_STORE = NullConversationStore()
        return _CONVERSATION_STORE
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    _CONVERSATION_STORE = DynamoConversationStore(table_name, ttl_seconds, max_messages, region)
    return _CONVERSATION_STORE


def extract_conversation_id(headers: Dict[str, Any], payload: Dict[str, Any]) -> Optional[str]:
    metadata = payload.get("metadata") or {}
    candidates = [
        metadata.get("conversation_id"),
        metadata.get("conversationId"),
        metadata.get("session_id"),
        metadata.get("sessionId"),
        headers.get("x-conversation-id"),
        headers.get("X-Conversation-Id"),
        payload.get("conversation_id"),
        metadata.get("pipeline_id"),
        metadata.get("pipelineId"),
        metadata.get("conversation"),
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    auth_header = headers.get("authorization") or headers.get("Authorization")
    if auth_header:
        token = auth_header.split(" ", 1)[-1]
        if token:
            hashed = hashlib.sha256(token.encode("utf-8")).hexdigest()
            return f"auth:{hashed}"
    return None
    return None


def should_clear_conversation(payload: Dict[str, Any]) -> bool:
    metadata = payload.get("metadata") or {}
    flags = [
        metadata.get("clear_conversation"),
        metadata.get("reset_conversation"),
        metadata.get("end_conversation"),
        payload.get("clear_conversation"),
    ]
    return any(flag for flag in flags if isinstance(flag, bool) or flag)


def prepare_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if payload is None:
        return {}
    # Support OpenAI Chat Completions fields by normalizing into responses schema.
    if 'functions' in payload and 'tools' not in payload:
        payload['tools'] = [
            {
                'type': 'function',
                'function': fn
            }
            for fn in payload.get('functions', [])
        ]
    if 'function_call' in payload and 'tool_choice' not in payload:
        func_call = payload.get('function_call')
        if isinstance(func_call, str):
            payload['tool_choice'] = func_call
        elif isinstance(func_call, dict):
            name = func_call.get('name')
            if name:
                payload['tool_choice'] = {'type': 'function', 'function': {'name': name}}
    if 'tools' in payload and payload.get('tools') and 'tool_choice' not in payload:
        payload['tool_choice'] = 'auto'
    if 'max_tokens' in payload and 'max_output_tokens' not in payload:
        payload['max_output_tokens'] = payload['max_tokens']
    last_user_text = get_last_user_text(payload)
    if last_user_text and looks_like_state_query(last_user_text):
        current_choice = payload.get('tool_choice')
        explicit_request = False
        if payload.get('function_call'):
            explicit_request = True
        elif isinstance(current_choice, dict):
            explicit_request = current_choice.get('type') == 'function'
        elif isinstance(current_choice, str) and current_choice and current_choice not in {'auto'}:
            explicit_request = True
        if not explicit_request:
            payload['_tools_disabled'] = True
            if current_choice in {'auto', None}:
                payload.pop('tool_choice', None)
            payload.pop('function_call', None)
            if payload.get('tools'):
                payload['_tools_original'] = payload['tools']
                payload.pop('tools', None)
    return payload
def normalize_content(content: Any) -> List[Dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, list):
        norm = []
        for item in content:
            if isinstance(item, dict):
                norm.append(item)
            else:
                norm.append({"type": "text", "text": str(item)})
        return norm
    if isinstance(content, dict):
        return [content]
    return [{"type": "text", "text": str(content)}]


def convert_tool_message(message: Dict[str, Any]) -> Dict[str, Any]:
    tool_call_id = message.get("tool_call_id")
    if not tool_call_id:
        raise ProxyError("Tool message requires tool_call_id")
    content_items = normalize_content(message.get("content"))
    tool_content = []
    for item in content_items:
        text = item.get("text")
        if text is None:
            continue
        if isinstance(text, str) and not text.strip():
            continue
        tool_content.append({"text": text})
    if not tool_content:
        tool_content.append({"text": "[brak danych]"})
    return {
        "role": "user",
        "content": [
            {
                "toolResult": {
                    "toolUseId": tool_call_id,
                    "status": "success",
                    "content": tool_content
                }
            }
        ]
    }


def convert_assistant_message(message: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    content_items = normalize_content(message.get("content"))
    parts: List[Dict[str, Any]] = []
    for item in content_items:
        if isinstance(item, dict) and item.get("type") == "text":
            text_value = item.get("text", "")
            if isinstance(text_value, str) and not text_value.strip():
                continue
            parts.append({"text": text_value})
        elif isinstance(item, dict) and "text" in item and len(item) == 1:
            text_value = item.get("text", "")
            if isinstance(text_value, str) and not text_value.strip():
                continue
            parts.append({"text": text_value})
        elif isinstance(item, str):
            if item.strip():
                parts.append({"text": item})
        else:
            parts.append({"text": json.dumps(item)})

    tool_use_ids: List[str] = []
    for call in message.get("tool_calls") or []:
        function_data = call.get("function", {})
        tool_name = function_data.get("name")
        arguments = function_data.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                pass
        tool_id = call.get("id") or str(uuid.uuid4())
        parts.append(
            {
                "toolUse": {
                    "toolUseId": tool_id,
                    "name": tool_name,
                    "input": arguments or {}
                }
            }
        )
        tool_use_ids.append(tool_id)

    if not parts:
        parts.append({"text": "[brak treści]"})
    return {"role": "assistant", "content": parts}, tool_use_ids


def convert_user_message(message: Dict[str, Any]) -> Dict[str, Any]:
    content_items = normalize_content(message.get("content") or message.get("input"))
    parts: List[Dict[str, Any]] = []
    for item in content_items:
        item_type = item.get("type", "text")
        if item_type == "text":
            text_value = item.get("text", "")
            if isinstance(text_value, str) and not text_value.strip():
                continue
            parts.append({"text": text_value})
        else:
            parts.append({"text": json.dumps(item)})
    if not parts:
        raise ProxyError("User message must include non-empty text")
    return {"role": "user", "content": parts}


def normalize_messages(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_messages = payload.get("messages")
    if not raw_messages and "input" in payload:
        raw_input = payload["input"]
        if isinstance(raw_input, str):
            raw_messages = [{"role": "user", "content": [{"type": "text", "text": raw_input}]}]
        else:
            raw_messages = raw_input
    if not raw_messages:
        raise ProxyError("Either 'messages' or 'input' is required")

    system_prompts: List[Dict[str, Any]] = []
    converted_messages: List[Dict[str, Any]] = []

    pending_tool_use_ids: Set[str] = set()

    for message in raw_messages:
        role = message.get("role")
        if not role:
            raise ProxyError("Message role is required")
        if role == "system":
            content_items = normalize_content(message.get("content") or message.get("input"))
            for item in content_items:
                text = item.get("text") if isinstance(item, dict) else str(item)
                if text is not None:
                    system_prompts.append({"text": text})
            continue
        if role == "assistant":
            converted_message, tool_use_ids = convert_assistant_message(message)
            pending_tool_use_ids = set(tool_use_ids)
            converted_messages.append(converted_message)
        elif role == "tool":
            tool_call_id = message.get("tool_call_id")
            if tool_call_id and tool_call_id in pending_tool_use_ids:
                converted_messages.append(convert_tool_message(message))
                pending_tool_use_ids.discard(tool_call_id)
            else:
                raise ProxyError("Tool message without matching tool_call_id")
        else:
            converted_messages.append(convert_user_message(message))

    return {"messages": converted_messages, "system": system_prompts}


def build_tool_config(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if payload.get("_tools_disabled"):
        return None
    tools = payload.get("tools")
    if not tools:
        return None
    specs = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        function_def = tool.get("function", {})
        name = function_def.get("name")
        if not name:
            raise ProxyError("Tool function requires a name")
        spec: Dict[str, Any] = {"name": name}
        description = function_def.get("description")
        if description:
            spec["description"] = description
        parameters = function_def.get("parameters")
        if parameters:
            spec["inputSchema"] = {"json": parameters}
        specs.append(spec)
    if not specs:
        return None
    tool_config: Dict[str, Any] = {"tools": [{"toolSpec": spec} for spec in specs]}

    tool_choice = payload.get("tool_choice")
    if tool_choice:
        choice: Dict[str, Any] = {}
        if isinstance(tool_choice, str):
            if tool_choice == "auto":
                choice = {"auto": {}}
            elif tool_choice == "none":
                choice = {}
            else:
                choice = {"tool": {"name": tool_choice}}
        elif isinstance(tool_choice, dict):
            mode = tool_choice.get("type")
            if mode == "auto":
                choice = {"auto": {}}
            elif mode == "none":
                choice = {}
            elif mode == "function":
                tool_name = tool_choice.get("function", {}).get("name")
                if tool_name:
                    choice = {"tool": {"name": tool_name}}
        if choice:
            tool_config["toolChoice"] = choice
    return tool_config


def build_inference_config(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    config: Dict[str, Any] = {}
    if "temperature" in payload:
        config["temperature"] = payload["temperature"]
    if "max_output_tokens" in payload:
        config["maxTokens"] = payload["max_output_tokens"]
    elif "max_tokens" in payload:
        config["maxTokens"] = payload["max_tokens"]
    return config or None


def build_bedrock_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    model_id = payload.get("model")
    if not model_id:
        raise ProxyError("Field 'model' is required")

    normalized = normalize_messages(payload)
    request: Dict[str, Any] = {
        "messages": normalized["messages"],
    }
    inference_profile_id = payload.get("inference_profile_id") or payload.get("inferenceProfileId")
    if inference_profile_id:
        request["inferenceProfileId"] = inference_profile_id
    else:
        request["modelId"] = model_id

    if normalized["system"]:
        request["system"] = normalized["system"]

    inference_config = build_inference_config(payload)
    if inference_config:
        request["inferenceConfig"] = inference_config

    tool_config = build_tool_config(payload)
    if tool_config:
        request["toolConfig"] = tool_config

    metadata = payload.get("metadata")
    if metadata:
        request["sessionState"] = {"metadata": metadata}

    return request


def bedrock_client() -> Any:
    if boto3 is None:  # pragma: no cover - dependency guard for tests
        raise ProxyError("boto3 is required to call Bedrock", status_code=500)
    region = os.getenv("BEDROCK_REGION", "us-east-1")
    return boto3.client("bedrock-runtime", region_name=region)

def sanitize_tool_arguments(arguments: Any) -> Any:
    if isinstance(arguments, dict):
        actions = arguments.get("list")
        if isinstance(actions, list):
            for action in actions:
                if not isinstance(action, dict):
                    continue
                service_data = action.get("service_data")
                if not isinstance(service_data, dict):
                    continue
                entity_id = service_data.get("entity_id")
                if isinstance(entity_id, str) and "." in entity_id:
                    domain_from_entity = entity_id.split(".", 1)[0]
                    action["domain"] = domain_from_entity
                    service_value = action.get("service")
                    if not isinstance(service_value, str) or "." in service_value:
                        action["service"] = "turn_on"
                elif isinstance(entity_id, list):
                    # ensure domain for batched entity_ids
                    unique_domains = {
                        eid.split(".", 1)[0]
                        for eid in entity_id
                        if isinstance(eid, str) and "." in eid
                    }
                    if len(unique_domains) == 1:
                        action["domain"] = unique_domains.pop()
    return arguments


def map_tool_calls(outputs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tool_calls: List[Dict[str, Any]] = []
    for output in outputs:
        if isinstance(output, dict):
            if "content" in output:
                content_list = output.get("content", [])
            elif isinstance(output.get("message"), dict):
                content_list = output["message"].get("content", [])
            else:
                content_list = []
        else:
            continue
        for content in content_list:
            tool_use = content.get("toolUse")
            if not tool_use:
                continue
            arguments = tool_use.get("input") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"raw": arguments}
            arguments = sanitize_tool_arguments(arguments)
            tool_calls.append(
                {
                    "id": tool_use.get("toolUseId", str(uuid.uuid4())),
                    "type": "function",
                    "function": {
                        "name": tool_use.get("name"),
                        "arguments": json.dumps(arguments)
                    }
                }
            )
    return tool_calls


def collect_output_text(outputs: List[Any]) -> str:
    text_parts: List[str] = []
    for output in outputs:
        if isinstance(output, str):
            text_parts.append(output)
            continue
        if not isinstance(output, dict):
            continue
        if "content" in output:
            raw_content = output.get("content", [])
        elif isinstance(output.get("message"), dict):
            raw_content = output["message"].get("content", [])
        else:
            raw_content = []
        if isinstance(raw_content, str):
            text_parts.append(raw_content)
            continue
        for item in raw_content:
            if isinstance(item, dict) and "text" in item:
                text_parts.append(item.get("text", ""))
    if not text_parts:
        return ""
    return strip_thinking_content("".join(text_parts))


def map_response(model_id: str, payload: Dict[str, Any], response: Dict[str, Any], mode: str = "responses") -> Dict[str, Any]:
    raw_output = response.get("output", [])
    if isinstance(raw_output, dict):
        if isinstance(raw_output.get("message"), dict):
            outputs = [raw_output["message"]]
        elif isinstance(raw_output.get("messages"), list):
            outputs = raw_output["messages"]
        else:
            outputs = [raw_output]
    else:
        outputs = raw_output

    tool_calls = map_tool_calls(outputs)

    tools_disabled = bool(payload.get("_tools_disabled"))
    if tools_disabled and tool_calls:
        LOGGER.info("Dropping %d tool_calls due to state-only query", len(tool_calls))
        tool_calls = []

    usage_data = response.get("usage", {})
    usage = {
        "input_tokens": usage_data.get("inputTokens", 0),
        "output_tokens": usage_data.get("outputTokens", 0),
        "total_tokens": usage_data.get("totalTokens", 0)
    }

    metadata = payload.get("metadata")
    output_text = collect_output_text(outputs)
    assistant_output: List[Dict[str, Any]] = [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": output_text or ""}],
        }
    ]

    openai_response = {
        "id": response.get("responseId", f"resp-{uuid.uuid4()}") or f"resp-{uuid.uuid4()}",
        "object": "response",
        "model": model_id,
        "created": int(time.time()),
        "status": "completed",
        "usage": usage,
        "output": assistant_output,
        "tool_calls": tool_calls,
    }
    if metadata is not None:
        openai_response["metadata"] = metadata
    if output_text:
        openai_response["output_text"] = output_text

    if mode == "chat":
        assistant_text = output_text if output_text is not None else ""
        message_content = assistant_text
        choice_message: Dict[str, Any] = {"role": "assistant", "content": message_content}
        if tool_calls:
            choice_message["tool_calls"] = tool_calls
            if message_content is None:
                choice_message["content"] = None
        finish_reason = "tool_calls" if tool_calls else "stop"
        chat_response = {
            "id": openai_response["id"],
            "object": "chat.completion",
            "model": openai_response["model"],
            "created": openai_response["created"],
            "usage": openai_response["usage"],
            "choices": [
                {
                    "index": 0,
                    "message": choice_message,
                    "finish_reason": finish_reason
                }
            ]
        }
        return chat_response

    return openai_response


def success_response(body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.getenv("CORS_ALLOW_ORIGINS", "*")
        },
        "body": json.dumps(body)
    }


def error_response(error: ProxyError) -> Dict[str, Any]:
    return {
        "statusCode": error.status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": {"message": error.message, "type": "proxy_error"}})
    }


def internal_error_response(message: str) -> Dict[str, Any]:
    return {
        "statusCode": 502,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": {"message": message, "type": "proxy_error"}})
    }


def lambda_handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    try:
        headers = event.get("headers") or {}
        token = extract_authorization(headers)
        validate_token(token)

        http_info = (event.get("requestContext") or {}).get("http") or {}
        raw_path = http_info.get("path")
        normalized_path = normalize_path(raw_path)
        api_path = extract_api_path(normalized_path)
        method = (http_info.get("method") or "POST").upper()

        if method == "GET":
            trimmed = api_path.rstrip("/")
            if trimmed == "/v1/models":
                model_id = get_default_model_id()
                body = {
                    "object": "list",
                    "data": [build_model_descriptor(model_id)],
                }
                return success_response(body)
            if trimmed.startswith("/v1/models/"):
                requested_id = trimmed[len("/v1/models/") :]
                if not requested_id:
                    raise ProxyError("Model id cannot be empty", status_code=400)
                return success_response(build_model_descriptor(requested_id))
            raise ProxyError("Unsupported GET path", status_code=404)

        payload = decode_body(event)
        payload = prepare_payload(payload)
        message_roles: List[str] = []
        if isinstance(payload.get("messages"), list):
            message_roles = [
                msg.get("role")
                for msg in payload.get("messages", [])
                if isinstance(msg, dict) and msg.get("role")
            ]
        conversation_id = extract_conversation_id(headers, payload)
        clear_conversation = should_clear_conversation(payload)
        store = conversation_store()
        history_plain: List[Dict[str, str]] = []
        if conversation_id and store.enabled:
            if clear_conversation:
                store.delete(conversation_id)
            else:
                history_plain = store.load(conversation_id)

        messages_with_history = payload.get("messages") or []
        system_messages = [msg for msg in messages_with_history if isinstance(msg, dict) and msg.get("role") == "system"]
        non_system_messages = [msg for msg in messages_with_history if isinstance(msg, dict) and msg.get("role") != "system"]
        history_messages_for_request: List[Dict[str, Any]] = []
        if history_plain:
            history_messages_for_request = [plain_entry_to_message(entry) for entry in history_plain]
            payload["messages"] = system_messages + history_messages_for_request + non_system_messages

        payload_summary = {
            "model": payload.get("model"),
            "message_roles": message_roles,
            "tools": len(payload.get("tools") or []),
            "stream": payload.get("stream", False),
            "path": api_path or normalized_path,
            "conversation_id": conversation_id,
            "history_entries": len(history_plain),
            "clear_requested": bool(clear_conversation),
        }
        LOGGER.info("PAYLOAD_SUMMARY %s", payload_summary)
        if payload.get("stream"):
            raise ProxyError("stream=true is not yet supported", status_code=400)

        request = build_bedrock_request(payload)
        LOGGER.info("Dispatching to Bedrock: %s", redact_pii(json.dumps({k: v for k, v in request.items() if k != "messages"})))

        client = bedrock_client()
        bedrock_response = client.converse(**request)
        response_summary = {
            "output_types": [type(item).__name__ for item in bedrock_response.get("output", [])],
            "keys": list(bedrock_response.keys()),
        }
        LOGGER.info("Received response from Bedrock: %s", response_summary)
        LOGGER.debug("BEDROCK_RAW %s", redact_pii(json.dumps(bedrock_response)))

        effective_model_id = request.get("modelId") or payload.get("model") or "unknown-model"
        mode = "chat" if "chat/completions" in api_path else "responses"
        openai_response = map_response(effective_model_id, payload, bedrock_response, mode=mode)

        if conversation_id and store.enabled:
            updated_plain_history = list(history_plain)
            for raw_message in non_system_messages:
                append_plain_entry(updated_plain_history, message_to_plain_entry(raw_message))
            output_text = openai_response.get("output_text")
            assistant_entry_plain: Optional[Dict[str, str]] = None
            if output_text:
                assistant_entry_plain = {"role": "assistant", "content": output_text}
            else:
                tool_summaries = []
                for call in openai_response.get("tool_calls") or []:
                    name = call.get("function", {}).get("name")
                    arguments = call.get("function", {}).get("arguments")
                    tool_summaries.append(f"[tool_call {name} args={arguments}]")
                if tool_summaries:
                    assistant_entry_plain = {"role": "assistant", "content": " ".join(tool_summaries)}
            append_plain_entry(updated_plain_history, assistant_entry_plain)
            store.save(conversation_id, updated_plain_history)
            openai_response["conversation_id"] = conversation_id

        return success_response(openai_response)

    except ProxyError as exc:
        LOGGER.warning("Proxy error: %s", redact_pii(str(exc)))
        return error_response(exc)
    except (ClientError, BotoCoreError) as exc:
        message = "Upstream Bedrock error"
        LOGGER.error("Bedrock error: %s", redact_pii(str(exc)))
        return internal_error_response(message)
    except Exception as exc:  # pragma: no cover
        LOGGER.exception("Unexpected error")
        return internal_error_response("Unexpected error")


def get_last_user_text(payload: Dict[str, Any]) -> str:
    messages = payload.get("messages") or []
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
                elif isinstance(item, str):
                    texts.append(item)
            if texts:
                return " ".join(t for t in texts if t)
        if isinstance(content, dict):
            text_value = content.get("text")
            if text_value:
                return text_value
    return ""


ACTION_PATTERNS = [
    r"\bwłąc(?:z|zyć|yc)\b", r"\bwlacz\b", r"\bzaświeć\b", r"\bzaswiec\b",
    r"\bwyłąc(?:z|zyć|yc)\b", r"\bwyklacz\b", r"\bwyłącz\b", r"\bwyłacz\b",
    r"\buruchom\b", r"\bzałącz\b", r"\bzałacz\b",
    r"\bzamknij\b", r"\botwórz\b", r"\botworz\b",
    r"\bstart\b", r"\bstop\b",
    r"\bturn on\b", r"\bturn off\b", r"\bswitch on\b", r"\bswitch off\b", r"\btoggle\b",
    r"\bopen\b", r"\bclose\b", r"\bactivate\b", r"\bdeactivate\b"
]

STATE_HINTS = [
    "czy", "jaki", "jakie", "jak", "stan", "status", "jest", "są", "są?", "działa", "działa?"
]

CONFIRMATION_WORDS = {"tak", "nie", "yes", "no", "ok", "okay", "y", "n"}


def looks_like_action(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    for pattern in ACTION_PATTERNS:
        if re.search(pattern, lowered):
            return True
    return False


def looks_like_state_query(text: str) -> bool:
    if not text:
        return False
    stripped = text.strip().lower()
    if looks_like_action(stripped):
        return False
    if stripped in CONFIRMATION_WORDS:
        return False
    if "?" in stripped:
        return True
    return any(hint in stripped for hint in STATE_HINTS)
