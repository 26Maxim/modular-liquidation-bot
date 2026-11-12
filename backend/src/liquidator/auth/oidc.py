"""Google OpenID Connect (OIDC) with PKCE and State/Nonce Verification."""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any, Dict, Optional, Tuple

import httpx
from liquidator.core.config import AppSettings
from liquidator.core.models import UserRole
from pydantic import BaseModel


class PKCEPair(BaseModel):
    code_verifier: str
    code_challenge: str


def generate_pkce_pair() -> PKCEPair:
    """Generate high-entropy code_verifier and S256 code_challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return PKCEPair(code_verifier=verifier, code_challenge=challenge)


def generate_state_and_nonce() -> Tuple[str, str]:
    """Generate cryptographically secure state and nonce tokens."""
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    return state, nonce


class GoogleOIDCClient:
    """
    Handles Google OpenID Connect Authorization Code Flow with PKCE.
    """

    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
    ISSUER = "https://accounts.google.com"

    def __init__(self, settings: AppSettings):
        self.settings = settings

    def get_authorization_url(self, state: str, nonce: str, code_challenge: str) -> str:
        """Construct the Google OAuth authorization URL."""
        params = {
            "client_id": self.settings.GOOGLE_CLIENT_ID or "",
            "response_type": "code",
            "scope": "openid email profile",
            "redirect_uri": self.settings.GOOGLE_REDIRECT_URI,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "online",
            "prompt": "select_account",
        }
        query_str = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.AUTH_URL}?{query_str}"

    async def exchange_code_for_tokens(
        self,
        code: str,
        code_verifier: str,
    ) -> Dict[str, Any]:
        """Exchange authorization code and PKCE code_verifier for id_token and access_token."""
        data = {
            "client_id": self.settings.GOOGLE_CLIENT_ID,
            "client_secret": self.settings.GOOGLE_CLIENT_SECRET,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": self.settings.GOOGLE_REDIRECT_URI,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(self.TOKEN_URL, data=data)
            if res.status_code != 200:
                raise ValueError(f"Failed to exchange token with Google: {res.text}")
            return res.json()

    async def get_user_info(self, access_token: str) -> Dict[str, Any]:
        """Fetch user profile information using access token."""
        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(self.USERINFO_URL, headers=headers)
            if res.status_code != 200:
                raise ValueError(f"Failed to fetch user info: {res.text}")
            return res.json()

    def verify_and_authorize_identity(
        self, user_info: Dict[str, Any]
    ) -> Tuple[bool, Optional[UserRole], str]:
        """
        Verifies that the user's email is verified and appears on the allowlist.
        Returns: (is_authorized, role, error_message)
        """
        email = user_info.get("email", "")
        email_verified = user_info.get("email_verified", False)
        sub = user_info.get("sub", "")

        if not email or not email_verified:
            return False, None, "Unverified Google email address."

        # Fail closed on empty allowlist
        if not self.settings.parsed_allowlist and not self.settings.DEV_AUTH_BYPASS:
            return False, None, "Access denied: Authentication allowlist is empty."

        role = self.settings.get_role_for_identity(email=email, sub=sub)
        if not role:
            return False, None, f"Identity '{email}' is not authorized on this system."

        return True, role, ""
