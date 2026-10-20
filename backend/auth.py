"""Signed anonymous browser identities for isolating local Agent resources."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import Request, Response
from backend.config import ROOT

OWNER_COOKIE_NAME = "uca_owner"
OWNER_COOKIE_MAX_AGE_S = 2_592_000
_DEV_SECRET = secrets.token_bytes(32)


@dataclass(frozen=True)
class RequestPrincipal:
    owner_id: str


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _cookie_payload(token: bytes, issued_at: int) -> bytes:
    return token + b"." + str(int(issued_at)).encode("ascii")


def create_owner_cookie(secret: str | bytes, *, now: int | None = None) -> str:
    key = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if not key:
        raise ValueError("owner cookie secret 不能为空")
    issued_at = int(time.time() if now is None else now)
    payload = _cookie_payload(secrets.token_bytes(32), issued_at)
    encoded = _b64url(payload)
    signature = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64url(signature)}"


def renew_owner_cookie(cookie: str, secret: str | bytes, *, now: int | None = None,
                       max_age_s: int = OWNER_COOKIE_MAX_AGE_S) -> str | None:
    """Refresh a valid cookie's timestamp while preserving its owner token."""
    if owner_id_from_cookie(cookie, secret, now=now, max_age_s=max_age_s) is None:
        return None
    try:
        encoded, _signature = cookie.split(".", 1)
        token, _issued_at = _unb64url(encoded).rsplit(b".", 1)
    except (ValueError, base64.binascii.Error):
        return None
    current = int(time.time() if now is None else now)
    renewed = _b64url(_cookie_payload(token, current))
    key = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    signature = hmac.new(key, renewed.encode("ascii"), hashlib.sha256).digest()
    return f"{renewed}.{_b64url(signature)}"


def owner_id_from_cookie(
    cookie: str | None,
    secret: str | bytes,
    *,
    now: int | None = None,
    max_age_s: int = OWNER_COOKIE_MAX_AGE_S,
) -> str | None:
    if not cookie or not isinstance(cookie, str):
        return None
    key = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if not key:
        return None
    try:
        encoded, given_signature = cookie.split(".", 1)
        payload = _unb64url(encoded)
        signature = _unb64url(given_signature)
        token, issued_at_raw = payload.rsplit(b".", 1)
        issued_at = int(issued_at_raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError, base64.binascii.Error):
        return None
    if len(token) != 32:
        return None
    expected = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return None
    current = int(time.time() if now is None else now)
    if issued_at > current + 60 or current - issued_at > max(0, int(max_age_s)):
        return None
    return hashlib.sha256(token).hexdigest()


def verify_owner_cookie(
    cookie: str | None,
    secret: str | bytes,
    *,
    now: int | None = None,
    max_age_s: int = OWNER_COOKIE_MAX_AGE_S,
) -> bool:
    return owner_id_from_cookie(cookie, secret, now=now, max_age_s=max_age_s) is not None


def make_principal_resolver(
    secret: str | bytes,
    *,
    secure: bool = False,
    max_age_s: int = OWNER_COOKIE_MAX_AGE_S,
):
    """Build a FastAPI dependency that validates or issues the owner cookie."""

    def resolve_principal(request: Request, response: Response) -> RequestPrincipal:
        cookie = request.cookies.get(OWNER_COOKIE_NAME)
        owner_id = owner_id_from_cookie(cookie, secret, max_age_s=max_age_s)
        value = None
        if owner_id is None:
            value = create_owner_cookie(secret)
            owner_id = owner_id_from_cookie(value, secret, max_age_s=max_age_s)
            assert owner_id is not None
        elif max_age_s > 1:
            issued_at = int(_unb64url(cookie.split(".", 1)[0]).rsplit(b".", 1)[1])
            if int(time.time()) - issued_at >= max_age_s // 2:
                value = renew_owner_cookie(cookie, secret, max_age_s=max_age_s)
        if value is not None:
            response.set_cookie(
                OWNER_COOKIE_NAME,
                value,
                max_age=max_age_s,
                httponly=True,
                secure=secure,
                samesite="lax",
                path="/",
            )
        return RequestPrincipal(owner_id=owner_id)

    return resolve_principal


def configured_owner_cookie_secret() -> str | bytes:
    configured = os.getenv("OWNER_COOKIE_SECRET", "").strip()
    if configured:
        return configured
    path = ROOT / ".cache" / "owner-cookie.secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as secret_file:
            secret_file.write(_DEV_SECRET)
    except FileExistsError:
        pass
    try:
        secret = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("无法读取本地 owner-cookie 秘密") from exc
    if len(secret) < 32:
        raise RuntimeError("本地 owner-cookie 秘密格式无效")
    return secret
