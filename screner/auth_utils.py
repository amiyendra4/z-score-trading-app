from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


QH_AUTH_URL = "https://qh-api.corp.hertshtengroup.com/apis/auth/"


class TokenInputError(ValueError):
    """Raised when pasted QH authentication text does not contain an access token."""


@dataclass(frozen=True)
class TokenStatus:
    exists: bool
    usable: bool
    expired: bool
    expiry_known: bool
    expires_at_utc: datetime | None
    expires_at_london: datetime | None
    seconds_remaining: int | None
    preview: str | None
    message: str


def _strip_bearer(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


def _token_from_json(value: Any) -> str | None:
    if isinstance(value, dict):
        # Prefer the exact keys used by QH/OAuth access-token responses.
        for key in ("access", "access_token", "accessToken"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return _strip_bearer(candidate)

        authorization = value.get("Authorization") or value.get("authorization")
        if isinstance(authorization, str) and authorization.strip():
            return _strip_bearer(authorization)

        # Auth payloads are sometimes wrapped in data/result objects.
        for nested in value.values():
            candidate = _token_from_json(nested)
            if candidate:
                return candidate

    if isinstance(value, list):
        for nested in value:
            candidate = _token_from_json(nested)
            if candidate:
                return candidate
    return None


def extract_access_token(pasted: str) -> str:
    """Extract the QH access token from raw token, Bearer value, JSON, or copied page text."""
    text = (pasted or "").strip()
    if not text:
        raise TokenInputError("Paste the access value or the full JSON returned by QH auth.")

    if text.lower().startswith("bearer "):
        token = _strip_bearer(text)
        if token:
            return token

    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if decoded is not None:
        if isinstance(decoded, str):
            token = _strip_bearer(decoded)
            if token:
                return token
        token = _token_from_json(decoded)
        if token:
            return token

    # Support copying a larger browser response/page that contains the access field.
    match = re.search(
        r'["\'](?:access|access_token|accessToken)["\']\s*:\s*["\']([^"\']+)["\']',
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return _strip_bearer(match.group(1))

    # Finally accept a token pasted by itself. JWTs and typical OAuth opaque tokens
    # contain no whitespace and are comfortably longer than an ordinary word.
    compact = _strip_bearer(text)
    if len(compact) >= 20 and not any(ch.isspace() for ch in compact):
        return compact

    raise TokenInputError(
        "Could not find an access token. Paste the QH 'access' value, a Bearer token, "
        "or the full auth JSON."
    )


def save_access_token(auth_file: Path, pasted: str) -> str:
    token = extract_access_token(pasted)
    auth_file = Path(auth_file)
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = auth_file.with_suffix(".tmp")
    temporary.write_text(token, encoding="utf-8")
    temporary.replace(auth_file)
    return token


def read_access_token(auth_file: Path) -> str:
    auth_file = Path(auth_file)
    if not auth_file.exists():
        raise FileNotFoundError(str(auth_file))
    token = _strip_bearer(auth_file.read_text(encoding="utf-8"))
    if not token:
        raise TokenInputError("The saved QH access token is empty.")
    return token


def _jwt_expiry(token: str) -> datetime | None:
    """Read a JWT exp claim without verifying the signature. Used only for UI expiry hints."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload_part = parts[1]
        padding = "=" * (-len(payload_part) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_part + padding)
        payload = json.loads(payload_bytes.decode("utf-8"))
        exp = payload.get("exp")
        if exp is None:
            return None
        return datetime.fromtimestamp(float(exp), tz=timezone.utc)
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def inspect_saved_token(auth_file: Path, now: float | None = None) -> TokenStatus:
    auth_file = Path(auth_file)
    if not auth_file.exists():
        return TokenStatus(
            exists=False,
            usable=False,
            expired=False,
            expiry_known=False,
            expires_at_utc=None,
            expires_at_london=None,
            seconds_remaining=None,
            preview=None,
            message="No QH access token is saved yet.",
        )

    try:
        token = read_access_token(auth_file)
    except (OSError, TokenInputError):
        return TokenStatus(
            exists=True,
            usable=False,
            expired=False,
            expiry_known=False,
            expires_at_utc=None,
            expires_at_london=None,
            seconds_remaining=None,
            preview=None,
            message="The saved QH token is empty or unreadable.",
        )

    preview = f"{token[:6]}…{token[-4:]}" if len(token) >= 12 else "saved"
    expires_at_utc = _jwt_expiry(token)
    if expires_at_utc is None:
        return TokenStatus(
            exists=True,
            usable=True,
            expired=False,
            expiry_known=False,
            expires_at_utc=None,
            expires_at_london=None,
            seconds_remaining=None,
            preview=preview,
            message="QH access token saved. Expiry is not readable from this token format.",
        )

    current = time.time() if now is None else float(now)
    seconds_remaining = int(expires_at_utc.timestamp() - current)
    expired = seconds_remaining <= 0
    london = expires_at_utc.astimezone(ZoneInfo("Europe/London"))
    if expired:
        message = f"Saved QH access token expired at {london:%d %b %Y %H:%M %Z}."
    else:
        message = f"QH access token saved; expires {london:%d %b %Y %H:%M %Z}."

    return TokenStatus(
        exists=True,
        usable=not expired,
        expired=expired,
        expiry_known=True,
        expires_at_utc=expires_at_utc,
        expires_at_london=london,
        seconds_remaining=seconds_remaining,
        preview=preview,
        message=message,
    )


def delete_saved_token(auth_file: Path) -> None:
    try:
        Path(auth_file).unlink()
    except FileNotFoundError:
        pass
