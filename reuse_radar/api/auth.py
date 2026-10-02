"""Curator authentication for the one write endpoint (reviews).

Curators get personal tokens through the REVIEWER_TOKENS environment variable,
`name:token,name:token`, set in Render's dashboard and never committed. Requests send
`Authorization: Bearer <token>`. Tokens are compared in constant time; the curator's name is
recorded on every review. Read endpoints stay anonymous.

Why not Django users and sessions: the curator group is tiny, the API has no admin or login UI,
and installing django.contrib.auth/sessions would add attack surface to an otherwise read-only
service. Tokens can be rotated by editing one environment variable.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

from django.core.exceptions import ImproperlyConfigured
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

MIN_TOKEN_LENGTH = 24


@dataclass(frozen=True)
class Curator:
    name: str
    is_authenticated: bool = True

    @property
    def pk(self) -> str:
        """DRF's throttles key authenticated users by `pk`; the curator name is unique."""
        return self.name


def configured_tokens() -> dict[str, str]:
    """token -> curator name, parsed from REVIEWER_TOKENS. Fails loudly on weak tokens."""
    tokens: dict[str, str] = {}
    for entry in os.environ.get("REVIEWER_TOKENS", "").split(","):
        if not entry.strip():
            continue
        name, sep, token = entry.strip().partition(":")
        if not sep or not name or len(token) < MIN_TOKEN_LENGTH:
            raise ImproperlyConfigured(
                f"REVIEWER_TOKENS entries must be name:token with tokens of at least "
                f"{MIN_TOKEN_LENGTH} characters (offending entry for {name or '?'!r})"
            )
        tokens[token] = name
    return tokens


class CuratorTokenAuthentication(BaseAuthentication):
    def authenticate(self, request: Request) -> tuple[Curator, str] | None:
        header = request.headers.get("Authorization", "")
        if not header:
            return None
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationFailed("use 'Authorization: Bearer <token>'")
        for known, name in configured_tokens().items():
            if hmac.compare_digest(known.encode(), token.strip().encode()):
                return Curator(name), token
        raise AuthenticationFailed("unknown token")

    def authenticate_header(self, request: Request) -> str:
        return "Bearer"


class IsCurator(BasePermission):
    message = "a curator token is required"

    def has_permission(self, request: Request, view: APIView) -> bool:
        return isinstance(request.user, Curator)
