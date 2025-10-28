import base64
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


def extract_last_user_text(payload: Dict[str, Any]) -> str:
    messages = payload.get("messages") or []
    for message in reversed(messages):
        if message.get("role") != "user":
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
        # fallback
        if "content" in message and isinstance(message["content"], str):
            return message["content"]
    return ""

def is_action_request(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    action_patterns = [
        r"\bwłąc(?:z|zyć|yc)\b", r"\bwlacz\b", r"\bwyłąc(?:z|zyć|yc)\b", r"\bwyklacz\b",
        r"\buruchom\b", r"\bzałącz\b", r"\bzałacz\b",
        r"\bzamknij\b", r"\botwórz\b", r"\botworz\b",
        r"\bstart\b", r"\bstop\b",
        r"\bturn on\b", r"\bturn off\b", r"\bswitch on\b", r"\bswitch off\b", r"\btoggle\b",
        r"\bopen\b", r"\bclose\b"
    ]
    for pattern in action_patterns:
        if re.search(pattern, lowered):
            return True
    return False


def extract_last_assistant_text(payload: Dict[str, Any]) -> str:
    messages = payload.get("messages") or []
    for message in reversed(messages):
        if message.get("role") != "assistant":
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

def is_confirmation(text: str) -> bool:
    if not text:
        return False
    lowered = text.strip().lower()
    confirmations = {
        "tak", "tak proszę", "tak prosze", "ok", "okej", "potwierdzam", "potwierdz",
        "proszę", "prosze", "zrób", "zrob", "wykonaj", "zrób to", "zrob to",
        "zrób proszę", "zrob prosze", "yes", "sure", "please do", "do it"
    }
    normalized = lowered.replace('!', '').replace('.', '').replace(',', '')
    return normalized in confirmations

def assistant_requested_confirmation(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    keywords = [
        "potwierdzasz", "potwierdzić", "potwierdzic", "czy mam", "czy chcesz",
        "czy życzysz", "czy życzysz sobie", "czy wykonać", "czy wykonac",
        "mam wyłączyć", "mam wylaczyc", "mam włączyć", "mam wlaczyc",
        "czy mam wyłączyć", "czy mam wlaczyc", "czy mam właczyć"
    ]
    return any(keyword in lowered for keyword in keywords)
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
    # Prevent tool calls for pure state queries
    tool_choice = payload.get('tool_choice')
    tool_is_auto = tool_choice == 'auto' or (
        isinstance(tool_choice, dict) and 'auto' in tool_choice
    )
    if tool_is_auto:
        last_user_text = extract_last_user_text(payload)
        action_intent = False
        if last_user_text:
            action_intent = is_action_request(last_user_text)
            if not action_intent:
                last_assistant_text = extract_last_assistant_text(payload)
                if is_confirmation(last_user_text) and assistant_requested_confirmation(last_assistant_text):
                    action_intent = True
        if not action_intent:
            payload['_tools_disabled'] = True
            payload.pop('tool_choice', None)
    if 'max_tokens' in payload and 'max_output_tokens' not in payload:
        payload['max_output_tokens'] = payload['max_tokens']
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
        tool_content.append({"text": text})
    if not tool_content:
        tool_content.append({"text": ""})
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
    tool_use_ids: List[str] = []
    for item in content_items:
        item_type = item.get("type", "text")
        if item_type == "text":
            parts.append({"text": item.get("text", "")})
        elif item_type == "tool_call":
            tool_call = item.get("tool_call", {})
            tool_name = tool_call.get("name")
            tool_input = tool_call.get("arguments")
            tool_id = tool_call.get("id") or str(uuid.uuid4())
            if isinstance(tool_input, str):
                try:
                    tool_input = json.loads(tool_input)
                except json.JSONDecodeError:
                    pass
            parts.append(
                {
                    "toolUse": {
                        "toolUseId": tool_id,
                        "name": tool_name,
                        "input": tool_input or {}
                    }
                }
            )
            tool_use_ids.append(tool_id)
        else:
            parts.append({"text": json.dumps(item)})
    tool_calls = message.get("tool_calls") or []
    for call in tool_calls:
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
        parts.append({"text": ""})
    return {"role": "assistant", "content": parts}, tool_use_ids


def convert_user_message(message: Dict[str, Any]) -> Dict[str, Any]:
    content_items = normalize_content(message.get("content") or message.get("input"))
    parts: List[Dict[str, Any]] = []
    for item in content_items:
        item_type = item.get("type", "text")
        if item_type == "text":
            parts.append({"text": item.get("text", "")})
        else:
            parts.append({"text": json.dumps(item)})
    if not parts:
        parts.append({"text": ""})
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
                fallback = convert_user_message({"content": message.get("content")})
                converted_messages.append(fallback)
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
    text_chunks: List[str] = []
    openai_output: List[Dict[str, Any]] = []

    for output in outputs:
        if isinstance(output, str):
            text_chunks.append(output)
            openai_output.append({"role": "assistant", "content": [{"type": "text", "text": output}]})
            continue
        if not isinstance(output, dict):
            continue
        role = output.get("role", "assistant")
        if isinstance(output, dict):
            if "content" in output:
                raw_content = output.get("content", [])
            elif isinstance(output.get("message"), dict):
                raw_content = output["message"].get("content", [])
            else:
                raw_content = []
        else:
            raw_content = []
        if isinstance(raw_content, str):
            raw_content = [{"text": raw_content}]
        content_items = []
        for item in raw_content:
            if isinstance(item, dict) and "text" in item:
                text_value = item.get("text", "")
                text_chunks.append(text_value)
                content_items.append({"type": "text", "text": text_value})
            elif isinstance(item, dict) and "toolUse" in item:
                content_items.append({"type": "tool_call", "tool_call": item["toolUse"]})
            else:
                content_items.append({"type": "text", "text": json.dumps(item)})
        if not content_items:
            content_items.append({"type": "text", "text": ""})
        openai_output.append({"role": role, "content": content_items})

    tool_calls = map_tool_calls(outputs)

    usage_data = response.get("usage", {})
    usage = {
        "input_tokens": usage_data.get("inputTokens", 0),
        "output_tokens": usage_data.get("outputTokens", 0),
        "total_tokens": usage_data.get("totalTokens", 0)
    }

    metadata = payload.get("metadata")

    openai_response = {
        "id": response.get("responseId", f"resp-{uuid.uuid4()}") or f"resp-{uuid.uuid4()}",
        "object": "response",
        "model": model_id,
        "created": int(time.time()),
        "status": "completed",
        "usage": usage,
        "output": openai_output or [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": ""}]
            }
        ],
        "tool_calls": tool_calls,
    }
    if metadata is not None:
        openai_response["metadata"] = metadata
    if text_chunks:
        openai_response["output_text"] = strip_thinking_content("".join(text_chunks))

    if mode == "chat":
        assistant_text = openai_response.get("output_text", "")
        message_content = assistant_text if assistant_text else None
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

        payload = decode_body(event)
        payload = prepare_payload(payload)
        LOGGER.info("ORIGINAL_PAYLOAD %s", payload)
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
        LOGGER.info("BEDROCK_RAW %s", redact_pii(json.dumps(bedrock_response)))

        effective_model_id = request.get("modelId") or payload.get("model") or "unknown-model"
        request_path = (event.get("requestContext", {}).get("http", {}) or {}).get("path", "")
        mode = "chat" if "chat/completions" in request_path else "responses"
        openai_response = map_response(effective_model_id, payload, bedrock_response, mode=mode)
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
