# 📌 Uwaga: aktualny kod został wygenerowany i zmodernizowany przez asystenta ChatGPT (Codex CLI).

# HABedrock Responses Proxy

Proxy zgodny z OpenAI Responses API, który deleguje zapytania do modelu Amazon Bedrock Nova (`amazon.nova-*-v1:0`). Aplikacja zapewnia kompatybilność z integracją Home Assistant Extended OpenAI Conversation przy minimalnej zmianie konfiguracji (podmiana `base_url` i `api_key`).

## Architektura
- **API Gateway HTTP API** z endpointem `POST /v1/responses`.
- **AWS Lambda (Python 3.11)** obsługująca logikę autoryzacji, mapowanie pól i wywołanie `bedrock-runtime:Converse`.
- **Amazon Bedrock Nova** jako model LLM.

Plik [`template.yaml`](template.yaml) udostępnia definicję AWS SAM, która tworzy HTTP API, funkcję Lambda oraz przypisuje minimalne uprawnienia `bedrock:Converse*`/`InvokeModel*` w wybranym regionie.

## Konfiguracja
Kluczowe zmienne środowiskowe funkcji Lambda:

- `API_KEYS` – lista akceptowanych tokenów rozdzielonych przecinkami; wykorzystywana do walidacji nagłówka `Authorization: Bearer <token>`.
- `BEDROCK_REGION` – region Bedrock (np. `us-east-1`).
- `CORS_ALLOW_ORIGINS` – lista dozwolonych originów CORS (domyślnie `https://*.homeassistant.local`).
- `SESSION_TABLE_NAME` – nazwa tabeli DynamoDB używanej do przechowywania historii rozmów. Pozostaw puste, aby wyłączyć pamięć.
- `SESSION_TTL_SECONDS` – czas życia wpisu w tabeli (domyślnie `300`, czyli 5 min). Po wygaśnięciu DynamoDB automatycznie usuwa historię.
- `MAX_HISTORY_MESSAGES` – maksymalna liczba najnowszych wiadomości przechowywana dla pojedynczej konwersacji (domyślnie `20`).

Jeżeli region/tenant wymaga **inference profile**, przekaż jego ARN w polu `inference_profile_id` (lub `inferenceProfileId`) w żądaniu `/v1/responses`. W przeciwnym razie użyty zostanie identyfikator modelu (`modelId`).

### Mapowanie pól
Lambda implementuje mapowanie zgodne z opisem w `AGENTS.md`:

| OpenAI Responses          | Bedrock Nova Converse                         |
|---------------------------|-----------------------------------------------|
| `messages`/`input`        | `messages` (`role`, `content[].text`)         |
| `system`                  | `system` (lista promptów)                     |
| `temperature`             | `inferenceConfig.temperature`                 |
| `max_output_tokens`       | `inferenceConfig.maxTokens`                   |
| `tools[].function`        | `toolConfig.toolSpec[]`                       |
| `tool_choice`             | `toolConfig.toolChoice`                       |
| `metadata`                | `sessionState.metadata` (passthrough)         |
| `toolUse` w odpowiedzi    | `tool_calls[]` (bez wykonania narzędzia)      |

Obsługiwane są komunikaty `tool` → `toolResult` oraz `assistant` z `tool_call`, zgodnie z wymaganiami Extended OpenAI Conversation.

## Pamięć konwersacji

### Identyfikator rozmowy
- Najpewniejszym sposobem na utrzymanie kontekstu jest przekazanie własnego `metadata.conversation_id` (np. identyfikatora pipeline’u Assist). Wtedy wszystkie kolejne żądania w danej rozmowie będą łączyć istniejącą historię z nowymi wiadomościami.
- Jeżeli klient nie przekaże identyfikatora, Lambda stosuje fallback `auth:<sha256(token)>`, co powoduje współdzielenie kontekstu przez wszystkie konwersacje wywoływane z danego klucza API.
- W odpowiedzi proxy zwraca użyty `conversation_id`, dzięki czemu klient może go zapisać i wykorzystać w kolejnych wywołaniach lub przy ręcznym resecie.

### Przechowywanie historii
- Historia rozmowy jest normalizowana do naprzemiennych wpisów `{"role": "user"|"assistant", "content": "..."}` i zapisywana w DynamoDB.
- Przed przekazaniem do Nova kontekst odbudowywany jest z system promptu, zapisanych wpisów oraz aktualnego pytania użytkownika. Wpisy narzędzi są streszczane, aby uniknąć konfliktu z konfiguracją `toolConfig`.
- TTL ustawiony w `SESSION_TTL_SECONDS` (domyślnie 5 min) odpowiada za automatyczne wygaszanie kontekstu – po jego upływie nowe zapytanie zaczyna „od zera”.

### Reset konwersacji
- Aby wymusić wyczyszczenie historii, dodaj do żądania flagę `metadata.clear_conversation` (lub `reset_conversation` / `end_conversation`). Lambda usunie wpis w DynamoDB przed obsługą aktualnego zapytania.
- Alternatywnie możesz skasować rekord w DynamoDB, posługując się identyfikatorem zwracanym w polu `conversation_id`.

## Uruchomienie z AWS SAM

```bash
sam build
sam deploy --guided
```

Po wdrożeniu `Outputs.ResponsesEndpoint` zawiera adres `https://<api_id>.execute-api.<region>.amazonaws.com/v1/responses`. Skonfiguruj Home Assistant, ustawiając `base_url` na ten adres i `api_key` na jedną z wartości `API_KEYS`.

## Chat Completions
Proxy obsługuje zarówno OpenAI Responses API (`POST /v1/responses`), jak i klasyczne Chat Completions (`POST /v1/chat/completions`). Extended OpenAI Conversation korzysta z endpointu chat, dlatego `base_url` ustaw na `https://<api_id>.execute-api.<region>.amazonaws.com/prod/v1`.

## Uruchomienie z CloudFormation

Szablon [`cloudformation.yaml`](cloudformation.yaml) odwzorowuje architekturę bez zależności SAM i zawiera zabezpieczenia minimalizujące uprawnienia. Przed wdrożeniem przygotuj artefakt Lambdy w S3 oraz skonfiguruj parametry:

- `LambdaCodeS3Bucket` / `LambdaCodeS3Key` (`LambdaCodeS3ObjectVersion` opcjonalnie) – lokalizacja pakietu ZIP z katalogu `src`.
- `ApiKeys` – lista kluczy Bearer; parametr posiada `NoEcho`, aby chronić wartości.
- `AllowedCorsOrigins` – lista dozwolonych originów (domyślnie `https://*.homeassistant.local`).
- `BedrockRegion` i `BedrockModelId` – wskazują region oraz konkretny model Nova.
- `BedrockBaseModelId` – nazwa bazowego modelu (bez prefiksu regionalnego), wykorzystywana do przypisania uprawnień IAM. Szablon domyślnie przyznaje dostęp do aliased `eu.amazon…` oraz bazowego `amazon.nova-lite-v1:0`.
- `LogRetentionDays`, `LambdaTimeoutSeconds`, `LambdaMemorySize` – umożliwiają dostrojenie bezpieczeństwa i kosztów.

Przykładowe wdrożenie:

```bash
aws cloudformation deploy \
  --stack-name habedrock-responses \
  --template-file cloudformation.yaml \
  --parameter-overrides \
      LambdaCodeS3Bucket=my-artifacts \
      LambdaCodeS3Key=habedrock/handler.zip \
      ApiKeys="changeme123" \
      AllowedCorsOrigins="https://ha.example.com" \
      BedrockRegion=us-east-1 \
      BedrockModelId=eu.amazon.nova-lite-v1:0 \
  --capabilities CAPABILITY_IAM
```

Po zakończeniu wdrożenia wyjście `ApiEndpoint` zawiera adres wymagany do konfiguracji Home Assistant.

## Testy

Testy jednostkowe (`pytest`) obejmują konwersję wiadomości, konfigurację narzędzi oraz pełny przebieg `lambda_handler` z mockiem Bedrock.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## Dalsze prace
- Obsługa `stream=true` (`converse_stream` + SSE).
- Wsparcie wizji i multimodalności.
- Cache konwersacji, guardrails, retry/backoff.
