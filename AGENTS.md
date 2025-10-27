# AGENTS.md — Proxy OpenAI Responses → Amazon Nova (Bedrock)

## Cel
Zapewnienie kompatybilnego z OpenAI **Responses API** endpointu `/v1/responses`, który wewnętrznie korzysta z Amazon Bedrock (Amazon Nova). Dzięki temu Home Assistant **Extended OpenAI Conversation** działa bez zmian konfiguracji poza `base_url` i `api_key`.  
Źródła: Extended OpenAI Conversation repo; Responses API; Bedrock Nova Converse.  
[ref] :contentReference[oaicite:7]{index=7}

## Architektura
- Klient: Home Assistant (Extended OpenAI Conversation).  
- API: API Gateway HTTP API → Lambda (Python) → Bedrock Runtime.  
- Model: `amazon.nova-*-v1:0` (np. `nova-lite`).  
Wzorzec integracyjny API Gateway → Lambda → Bedrock.  
[ref] :contentReference[oaicite:8]{index=8}

## Endpoints
### POST /v1/responses
- Wejście: podzbiór **OpenAI Responses API** (`model`, `input`/`messages`, `temperature`, `max_output_tokens`, `tools`, `tool_choice`, `metadata`, `stream`=false).  
- Wyjście: obiekt `response` z polami: `id`, `object`, `model`, `created`, `output[0].content[0].text`, `status`, `usage`, `tool_calls[]`.

### Autoryzacja
- `Authorization: Bearer <token>` — walidowany w Lambdzie.  
- CORS: ograniczony do domen HA (konfigurowalne).

## Mapowanie pól
| OpenAI Responses | Bedrock (Nova Converse) |
|---|---|
| `input` / `messages` | `messages` (`role`, `content:[{text}]`) |
| `system` | pierwszy komunikat `role: system` |
| `temperature` | `inferenceConfig.temperature` |
| `max_output_tokens` | `inferenceConfig.maxTokens` |
| `tools[].function` | `toolConfig.toolSpecs[]` (`name`, `description`, `inputSchema.json`) |
| tool calls w odpowiedzi | `output[].content[].toolUse` → `tool_calls[]` |

Dokumentacja Responses i Nova opisują powyższe elementy.  
[ref] :contentReference[oaicite:9]{index=9}

## Zachowanie tool calling
- Gdy Nova emituje `toolUse`, proxy zwraca `tool_calls[]` bez wykonywania akcji.  
- Wykonanie narzędzi realizuje Extended OpenAI Conversation (wywołania usług HA).  
- Kolejna runda rozmowy z wynikiem narzędzia powinna być przesłana jako dodatkowy `assistant`/`tool` → `user`.  
Uwagi o sposobie użycia narzędzi w Extended OpenAI Conversation w repo.  
[ref] :contentReference[oaicite:10]{index=10}

## Obsługiwane opcje
- Temperatury, limity tokenów, metadane passthrough.  
- Tryb stream (opcjonalny etap 2): `converse_stream` i SSE.  
- Zliczanie tokenów: na podstawie `usage` z Bedrock.

## Błędy
- 400 — niepoprawny JSON/nieobsługiwane pola.  
- 401 — brak/autoryzacja niepoprawna.  
- 502 — błąd Bedrock (z redakcją treści).  
Zwracaj w formacie: `{"error": {"message":"...", "type":"proxy_error"}}`.

## Obserwowalność
- CloudWatch: strukturalne logi JSON.  
- Redakcja PII (regexy dla kluczy, numerów telefonów, e-mail).

## Bezpieczeństwo
- Klucze w `API_KEYS` (Lambda env).  
- Least privilege IAM: `bedrock:Converse*`/`Invoke*` tylko w wybranym regionie/kontach.

## Testowanie
- Jednostkowe testy konwersji messages/tools/odpowiedzi.  
- E2E: Home Assistant z prostą funkcją `homeassistant.turn_on` i potwierdzeniem `tool_calls`.  
- Weryfikacja kompatybilności z HA OpenAI integration zachowuje się podobnie.  
[ref] :contentReference[oaicite:11]{index=11}

## Roadmap
- SSE streaming; vision i multimodal; cache rozmów; guardrails; retry z backoff.


