# azureopenai-proxy

Azure OpenAI pass-through reverse proxy that accepts OpenAI-compatible requests
and forwards them to Azure OpenAI using Entra ID (DefaultAzureCredential). It
preserves methods, paths, query strings, headers, and bodies, including
streaming SSE responses without buffering or transformation.

## Features

- OpenAI-compatible routing with optional /v1 -> /openai rewrite
- Entra ID authentication via DefaultAzureCredential
- Mandatory streaming pass-through (httpx.AsyncClient.stream + StreamingResponse)
- Proxy API key protection for inbound access

## Environment variables

- `AZURE_OPENAI_ENDPOINT` (required): `https://<resource>.openai.azure.com`
- `PROXY_API_KEY` (required): proxy API key for inbound clients
- `AZURE_OPENAI_API_VERSION` (optional): appended as `api-version` if missing
- `ROUTE_MODE` (optional): `openai_compat` (default) or `transparent`

Example:

```bash
export AZURE_OPENAI_ENDPOINT="https://my-resource.openai.azure.com"
export AZURE_OPENAI_API_VERSION="2024-06-01"
export PROXY_API_KEY="local-dev-key"
export ROUTE_MODE="openai_compat"
```

## Local development

```bash
az login
pip install -r requirements.txt
uvicorn proxy:app --host 0.0.0.0 --port 8000
```

Health check:

```bash
curl http://localhost:8000/healthz
```

## Curl tests

Non-streaming request:

```bash
curl http://localhost:8000/v1/deployments/<deployment>/chat/completions \
  -H "Authorization: Bearer $PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello"}],"max_tokens":32}'
```

Streaming request (SSE pass-through):

```bash
curl -N http://localhost:8000/v1/deployments/<deployment>/chat/completions \
  -H "x-api-key: $PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Write a haiku"}],"stream":true,"max_tokens":64}'
```

You should see incremental SSE chunks as they arrive (no buffering).

## Routing modes

- `openai_compat` (default): rewrites `/v1/<anything>` to `/openai/<anything>`
- `transparent`: no path rewriting

Query strings are passed through as-is. If `api-version` is missing and
`AZURE_OPENAI_API_VERSION` is set, the proxy appends it.

## OpenCode setup

- Base URL: `http://localhost:8000/v1/deployments/<deployment>`
- API key: `PROXY_API_KEY`
- If your client already uses `/openai/...` paths, set `ROUTE_MODE=transparent`

## Azure deployment notes

- Use Managed Identity for the proxy host.
- Assign the managed identity the Azure RBAC role
  `Cognitive Services OpenAI User` (or your org equivalent) on the Azure OpenAI
  resource.
- Set `AZURE_OPENAI_ENDPOINT`, `PROXY_API_KEY`, and optionally
  `AZURE_OPENAI_API_VERSION`.