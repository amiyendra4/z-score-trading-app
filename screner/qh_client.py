from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from auth_utils import TokenInputError, inspect_saved_token, read_access_token


DEFAULT_BASE_URL = "https://qh-api.corp.hertshtengroup.com/apis/ohlc/"


class QHAPIError(RuntimeError):
    """A safe, user-facing QH API error."""


class QHAuthenticationError(QHAPIError):
    """Raised when QH needs a new Microsoft-issued access token."""


@dataclass(frozen=True)
class QHResult:
    payload: object
    rate_limit: dict[str, str | None]
    from_cache: bool


class QHClient:
    def __init__(
        self,
        auth_file: Path,
        cache_dir: Path,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: int = 30,
        max_retries: int = 4,
        max_requests_per_minute: int = 30,
    ) -> None:
        self.auth_file = Path(auth_file)
        self.cache_dir = Path(cache_dir)
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_requests_per_minute = max_requests_per_minute
        self.session = requests.Session()

    def _authorization_header(self) -> str:
        status = inspect_saved_token(self.auth_file)
        if not status.exists:
            raise QHAuthenticationError(
                "No QH access token is saved. Use the QH Access panel in the sidebar to sign in and save one."
            )
        if status.expired:
            raise QHAuthenticationError(
                "The saved QH access token has expired. Use the QH Access panel in the sidebar to refresh it."
            )
        try:
            token = read_access_token(self.auth_file)
        except (OSError, TokenInputError) as exc:
            raise QHAuthenticationError(
                "The saved QH access token is empty or unreadable. Save a new token from the QH Access panel."
            ) from exc
        return f"Bearer {token}"

    @staticmethod
    def _rate_limit_headers(response: requests.Response) -> dict[str, str | None]:
        return {
            "limit": response.headers.get("X-RateLimit-Limit"),
            "remaining": response.headers.get("X-RateLimit-Remaining"),
            "reset": response.headers.get("X-RateLimit-Reset"),
        }

    def _cache_path(self, params: dict[str, Any]) -> Path:
        canonical = json.dumps(
            {"url": self.base_url, "params": params}, sort_keys=True, default=str
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, path: Path, ttl_seconds: int) -> QHResult | None:
        if not path.exists() or time.time() - path.stat().st_mtime > ttl_seconds:
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            return QHResult(record["payload"], record.get("rate_limit", {}), True)
        except (OSError, json.JSONDecodeError, KeyError):
            return None

    def _write_cache(
        self, path: Path, payload: object, rate_limit: dict[str, str | None]
    ) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"payload": payload, "rate_limit": rate_limit}), encoding="utf-8"
        )
        temporary.replace(path)

    def _respect_staging_rate_limit(self) -> None:
        """Keep this app within the documented 30/minute OHLC limit."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / "request_times.json"
        now = time.time()
        try:
            request_times = [
                float(item)
                for item in json.loads(path.read_text(encoding="utf-8"))
                if now - float(item) < 60.0
            ]
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            request_times = []

        if len(request_times) >= self.max_requests_per_minute:
            delay = max(0.0, 60.1 - (now - min(request_times)))
            time.sleep(delay)
            now = time.time()
            request_times = [item for item in request_times if now - item < 60.0]

        request_times.append(now)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(request_times), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _retry_delay(response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(120.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        reset = response.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                reset_value = float(reset)
                seconds = (
                    reset_value - time.time()
                    if reset_value > 1_000_000_000
                    else reset_value
                )
                return min(120.0, max(0.0, seconds))
            except ValueError:
                pass
        return min(60.0, (2**attempt) + random.uniform(0.0, 1.0))

    def fetch_ohlc(
        self,
        instruments: str,
        interval: str = "5m",
        start: int | None = None,
        end: int | None = None,
        count: int | None = None,
        cache_ttl_seconds: int = 60,
    ) -> QHResult:
        """Fetch the directly quoted QH instrument exactly as entered."""
        params: dict[str, Any] = {"instruments": instruments, "interval": interval}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        if count is not None:
            params["count"] = count

        cache_path = self._cache_path(params)
        cached = self._read_cache(cache_path, cache_ttl_seconds)
        if cached is not None:
            return cached

        headers = {
            "Authorization": self._authorization_header(),
            "Accept": "application/json",
        }
        last_response: requests.Response | None = None
        for attempt in range(self.max_retries + 1):
            self._respect_staging_rate_limit()
            try:
                response = self.session.get(
                    self.base_url,
                    params=params,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise QHAPIError(f"Could not reach the QH API: {exc}") from exc
                time.sleep(min(30.0, (2**attempt) + random.uniform(0.0, 1.0)))
                continue

            last_response = response
            if response.status_code == 429:
                if attempt >= self.max_retries:
                    break
                time.sleep(self._retry_delay(response, attempt))
                continue
            if response.status_code in {401, 403}:
                raise QHAuthenticationError(
                    "QH rejected the saved access token. Open QH sign-in from the sidebar, "
                    "copy the new access response, and save it there."
                )
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                message = response.text[:300].strip()
                raise QHAPIError(
                    f"QH API returned HTTP {response.status_code}: {message or response.reason}"
                ) from exc
            try:
                payload = response.json()
            except requests.JSONDecodeError as exc:
                raise QHAPIError("QH API returned a response that was not valid JSON.") from exc

            rate_limit = self._rate_limit_headers(response)
            self._write_cache(cache_path, payload, rate_limit)
            return QHResult(payload, rate_limit, False)

        rate_limit = self._rate_limit_headers(last_response) if last_response else {}
        raise QHAPIError(
            "QH API rate limit was still active after retry/backoff. "
            f"Reset: {rate_limit.get('reset') or 'unknown'}. Please try again later."
        )
