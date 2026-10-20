"""Shared plumbing for HTTP (cloud) providers: client handling, error mapping, redaction.

Uses ``httpx`` directly (no vendor SDKs) to keep the Pi install small and make tests
deterministic with ``httpx.MockTransport``. API keys are held as ``SecretStr`` and only
ever placed in request headers; they never appear in exceptions, logs or status.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx
from pydantic import SecretStr

from littlebot.brain.providers.base import (
    AuthenticationError, BaseProvider, NetworkError, ProviderError, ProviderTimeoutError, RateLimitError,
)

log = logging.getLogger("littlebot.brain.providers")

_KEYLIKE = re.compile(r"\b(?:sk|key|tok)[-_][\w*.-]{6,}", re.I)


class HttpProvider(BaseProvider):
    is_cloud = True

    def __init__(
        self,
        *,
        api_key: SecretStr | str | None,
        model: str,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if isinstance(api_key, str):
            api_key = SecretStr(api_key)
        self._key: SecretStr | None = api_key if (api_key and api_key.get_secret_value().strip()) else None
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._transport = transport

    # -- helpers -----------------------------------------------------------
    def configured(self) -> bool:
        return self._key is not None

    def _secret(self) -> str:
        if self._key is None:
            raise AuthenticationError(f"{self.name}: no API key configured", kind="no_key")
        return self._key.get_secret_value().strip()

    def _redact(self, text: str) -> str:
        if self._key is not None:
            secret = self._key.get_secret_value().strip()
            if secret:
                text = text.replace(secret, "[redacted]")
        return _KEYLIKE.sub("[redacted]", text)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(transport=self._transport, follow_redirects=False)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client and not self._client.is_closed:
            await self._client.aclose()
        if self._owns_client:
            self._client = None

    async def _post_json(self, url: str, *, headers: dict[str, str], body: dict[str, Any],
                         timeout: float) -> tuple[dict[str, Any], int]:
        """POST JSON, map failures to typed errors, return (json, latency_ms)."""
        client = self._get_client()
        t0 = time.monotonic()
        try:
            resp = await client.post(
                url, headers=headers, json=body,
                timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            )
        except httpx.TimeoutException:
            raise ProviderTimeoutError(f"{self.name}: request timed out after {timeout:.0f}s") from None
        except httpx.TransportError as e:  # connect / DNS / reset / protocol errors
            raise NetworkError(f"{self.name}: network unavailable ({type(e).__name__})") from None
        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code >= 400:
            raise self._status_error(resp)
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError(f"{self.name}: malformed JSON response", status_code=resp.status_code,
                                kind="malformed") from None
        if not isinstance(data, dict):
            raise ProviderError(f"{self.name}: unexpected response shape", status_code=resp.status_code,
                                kind="malformed")
        return data, latency_ms

    def _status_error(self, resp: httpx.Response) -> ProviderError:
        code = resp.status_code
        detail = ""
        err_type = ""
        try:
            payload = resp.json()
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, dict):
                detail = str(err.get("message") or "")
                err_type = str(err.get("type") or err.get("code") or "")
        except ValueError:
            pass
        detail = self._redact(detail)[:160]
        msg = f"{self.name}: HTTP {code}" + (f" {err_type}" if err_type else "") + (f": {detail}" if detail else "")
        if code in (401, 403):
            return AuthenticationError(msg, status_code=code)
        if code == 429:
            retry_after: float | None = None
            try:
                retry_after = float(resp.headers.get("retry-after", ""))
            except ValueError:
                pass
            kind = "quota_exceeded" if "quota" in (err_type + detail).lower() else None
            return RateLimitError(msg, status_code=code, retry_after=retry_after, kind=kind)
        if code == 402:
            return ProviderError(msg, status_code=code, kind="billing")
        if code == 404:
            return ProviderError(msg, status_code=code, kind="not_found")
        if code in (400, 413, 422):
            return ProviderError(msg, status_code=code, kind="bad_request")
        err = ProviderError(msg, status_code=code, kind="overloaded" if code == 529 else "server_error")
        err.retryable = code >= 500
        return err
