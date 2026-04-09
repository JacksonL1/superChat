from __future__ import annotations

from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import settings

bearer_scheme = HTTPBearer(auto_error=False)


def _parse_required_scopes() -> set[str]:
    return {
        s.strip()
        for s in settings.auth_required_scopes.split(",")
        if s.strip()
    }


def _extract_scopes(payload: dict[str, Any]) -> set[str]:
    raw = payload.get("scope") or payload.get("scopes") or []
    if isinstance(raw, str):
        return {s for s in raw.split() if s}
    if isinstance(raw, list):
        return {str(s).strip() for s in raw if str(s).strip()}
    return set()


def verify_jwt_token(token: str) -> dict[str, Any]:
    options = {
        "require": ["exp", "iat"],
        "verify_signature": True,
        "verify_exp": True,
        "verify_iat": True,
    }

    try:
        payload = jwt.decode(
            token,
            settings.auth_jwt_secret,
            algorithms=[settings.auth_jwt_algorithm],
            audience=settings.auth_audience or None,
            issuer=settings.auth_issuer or None,
            options=options,
            leeway=settings.auth_clock_skew_seconds,
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired access token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    required_scopes = _parse_required_scopes()
    if required_scopes:
        token_scopes = _extract_scopes(payload)
        if not required_scopes.issubset(token_scopes):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient scope for this resource",
            )

    return payload


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, Any]:
    if not settings.auth_enabled:
        # 仅用于开发显式关闭，绝不根据来源地址自动信任。
        return {"sub": "anonymous", "scope": ""}

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return verify_jwt_token(credentials.credentials)
