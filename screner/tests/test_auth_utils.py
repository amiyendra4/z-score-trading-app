import base64
import json
import time
from pathlib import Path

import pytest

from auth_utils import (
    TokenInputError,
    extract_access_token,
    inspect_saved_token,
    save_access_token,
)
from qh_client import QHAuthenticationError, QHClient


def _jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.signature"


def test_extracts_raw_bearer_and_qh_json_tokens():
    token = "abc12345678901234567890"
    assert extract_access_token(token) == token
    assert extract_access_token(f"Bearer {token}") == token
    assert extract_access_token(json.dumps({"access": token, "refresh": "ignore-me"})) == token
    assert extract_access_token(json.dumps({"data": {"access_token": token}})) == token


def test_extracts_access_from_copied_page_text():
    token = "abc12345678901234567890"
    text = f"Authentication response: {{'access': '{token}'}}"
    assert extract_access_token(text) == token


def test_rejects_text_without_a_token():
    with pytest.raises(TokenInputError):
        extract_access_token("not a token")


def test_save_and_inspect_future_jwt(tmp_path: Path):
    auth_file = tmp_path / ".secrets" / "qh_authorization.txt"
    now = int(time.time())
    token = _jwt(now + 3600)
    save_access_token(auth_file, json.dumps({"access": token}))

    assert auth_file.read_text(encoding="utf-8") == token
    status = inspect_saved_token(auth_file, now=now)
    assert status.exists is True
    assert status.usable is True
    assert status.expired is False
    assert status.expiry_known is True
    assert 3599 <= status.seconds_remaining <= 3600


def test_inspect_detects_expired_jwt(tmp_path: Path):
    auth_file = tmp_path / "qh_authorization.txt"
    now = int(time.time())
    save_access_token(auth_file, _jwt(now - 60))

    status = inspect_saved_token(auth_file, now=now)
    assert status.usable is False
    assert status.expired is True


def test_qh_client_missing_token_uses_auth_specific_error(tmp_path: Path):
    client = QHClient(
        auth_file=tmp_path / "missing.txt",
        cache_dir=tmp_path / "cache",
    )
    with pytest.raises(QHAuthenticationError):
        client._authorization_header()
