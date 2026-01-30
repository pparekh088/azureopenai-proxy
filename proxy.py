import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx
from azure.identity.aio import DefaultAzureCredential
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.datastructures import Headers, QueryParams

SCOPE = "https://cognitiveservices.azure.com/.default"

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

AUTH_HEADERS = {"authorization", "api-key", "x-api-key"}

logger = logging.getLogger("azure_openai_proxy")
logging.basicConfig(level=logging.INFO, format="%(message)s")


@dataclass(frozen=True)
class ProxyConfig:
    azure_endpoint: str
    azure_host: str
    route_mode: str
    api_version: Optional[str]
    proxy_api_key: str


class TokenCache:
    def __init__(self, credential: DefaultAzureCredential, scope: str) -> None:
        self._credential = credential
        self._scope = scope
        self._lock = asyncio.Lock()
        self._token: Optional[str] = None
        self._expires_on: float = 0.0
        self._refresh_buffer = 120.0

    async def get_token(self) -> str:
        now = time.time()
        if self._token and now < (self._expires_on - self._refresh_buffer):
            return self._token

        async with self._lock:
            now = time.time()
            if self._token and now < (self._expires_on - self._refresh_buffer):
                return self._token
            token = await self._credential.get_token(self._scope)
            self._token = token.token
            self._expires_on = float(token.expires_on)
            return self._token


def _load_config() -> ProxyConfig:
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    if not endpoint:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT is required")

    proxy_key = os.getenv("PROXY_API_KEY")
    if not proxy_key:
        raise RuntimeError("PROXY_API_KEY is required")

    route_mode = os.getenv("ROUTE_MODE", "openai_compat").lower().strip()
    if route_mode not in {"openai_compat", "transparent"}:
        raise RuntimeError("ROUTE_MODE must be openai_compat or transparent")

    api_version = os.getenv("AZURE_OPENAI_API_VERSION")
    parsed = urlparse(endpoint)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT must be a valid URL")

    azure_host = parsed.netloc
    azure_endpoint = endpoint.rstrip("/")
    return ProxyConfig(
        azure_endpoint=azure_endpoint,
        azure_host=azure_host,
        route_mode=route_mode,
        api_version=api_version,
        proxy_api_key=proxy_key,
    )


def _is_authorized(headers: Headers, expected_key: str) -> bool:
    api_key = headers.get("x-api-key")
    if api_key:
        return secrets.compare_digest(api_key, expected_key)

    authorization = headers.get("authorization")
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return secrets.compare_digest(parts[1], expected_key)
    return False


def _rewrite_path(path: str, route_mode: str) -> str:
    if route_mode != "openai_compat":
        return path
    if path == "/v1":
        return "/openai"
    if path.startswith("/v1/"):
        return "/openai/" + path[len("/v1/") :]
    return path


def _append_api_version(raw_query: str, api_version: Optional[str]) -> str:
    if not api_version:
        return raw_query

    query_params = QueryParams(raw_query)
    if any(key.lower() == "api-version" for key in query_params.keys()):
        return raw_query

    encoded = quote(api_version, safe="")
    if raw_query:
        return f"{raw_query}&api-version={encoded}"
    return f"api-version={encoded}"


def _clean_request_headers(
    headers: Headers,
    azure_host: str,
    bearer_token: str,
) -> List[Tuple[str, str]]:
    excluded = set(HOP_BY_HOP_HEADERS)
    excluded.update({"host", "content-length"})
    excluded.update(AUTH_HEADERS)

    connection_header = headers.get("connection")
    if connection_header:
        for name in connection_header.split(","):
            excluded.add(name.strip().lower())

    cleaned: List[Tuple[str, str]] = []
    for name_bytes, value_bytes in headers.raw:
        name = name_bytes.decode("latin1")
        if name.lower() in excluded:
            continue
        value = value_bytes.decode("latin1")
        cleaned.append((name, value))

    cleaned.append(("Authorization", f"Bearer {bearer_token}"))
    cleaned.append(("Host", azure_host))
    return cleaned


def _clean_response_headers(headers: httpx.Headers) -> List[Tuple[str, str]]:
    excluded = {"content-length", "transfer-encoding", "connection"}
    cleaned: List[Tuple[str, str]] = []
    for name, value in headers.multi_items():
        if name.lower() in excluded:
            continue
        cleaned.append((name, value))
    return cleaned


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = _load_config()
    credential = DefaultAzureCredential()
    token_cache = TokenCache(credential, SCOPE)
    client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None))
    app.state.config = config
    app.state.credential = credential
    app.state.token_cache = token_cache
    app.state.http_client = client
    yield
    await client.aclose()
    await credential.close()


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy(request: Request) -> StreamingResponse:
    config: ProxyConfig = request.app.state.config

    if not _is_authorized(request.headers, config.proxy_api_key):
        raise HTTPException(status_code=401, detail="Unauthorized")

    start_time = time.time()

    try:
        bearer_token = await request.app.state.token_cache.get_token()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "token_error method=%s path=%s error=%s",
            request.method,
            request.url.path,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Bad Gateway")

    target_path = _rewrite_path(request.url.path, config.route_mode)
    raw_query = _append_api_version(request.url.query, config.api_version)
    target_url = f"{config.azure_endpoint}{target_path}"
    if raw_query:
        target_url = f"{target_url}?{raw_query}"

    outbound_headers = _clean_request_headers(
        request.headers,
        config.azure_host,
        bearer_token,
    )

    stream_cm = request.app.state.http_client.stream(
        request.method,
        target_url,
        headers=outbound_headers,
        content=request.stream(),
    )

    try:
        resp = await stream_cm.__aenter__()
    except httpx.RequestError as exc:
        logger.warning(
            "upstream_error method=%s path=%s error=%s",
            request.method,
            request.url.path,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Bad Gateway")

    response_headers = _clean_response_headers(resp.headers)
    status_code = resp.status_code
    latency_ms = int((time.time() - start_time) * 1000)
    logger.info(
        "method=%s path=%s status=%s latency_ms=%s",
        request.method,
        request.url.path,
        status_code,
        latency_ms,
    )

    async def iter_response() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await stream_cm.__aexit__(None, None, None)

    return StreamingResponse(
        iter_response(),
        status_code=status_code,
        headers=response_headers,
    )
