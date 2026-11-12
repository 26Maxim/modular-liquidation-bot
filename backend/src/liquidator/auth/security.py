"""Session management, CSRF protection, and Role-Based Access Control (RBAC)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Set

from fastapi import Depends, HTTPException, Request, Response, status
from liquidator.core.config import AppSettings
from liquidator.core.models import UserRole
from liquidator.storage.repository import Repository
from pydantic import BaseModel

SESSION_COOKIE_NAME = "liquidator_session"
CSRF_COOKIE_NAME = "liquidator_csrf"
MAX_SENSITIVE_ACTION_AGE_SECONDS = 900  # 15 minutes


class AuthenticatedUser(BaseModel):
    user_email: str
    user_sub: str
    role: UserRole
    session_id: str
    last_auth_time: float


def generate_csrf_token(secret_key: str, session_id: str) -> str:
    """Derives a CSRF token bound to the active session id."""
    token = hmac.new(
        secret_key.encode("utf-8"),
        session_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return token


def verify_csrf_token(secret_key: str, session_id: str, submitted_token: str) -> bool:
    expected = generate_csrf_token(secret_key, session_id)
    return hmac.compare_digest(expected, submitted_token)


class SecurityManager:
    def __init__(self, settings: AppSettings, repository: Repository):
        self.settings = settings
        self.repository = repository

    async def create_user_session(
        self,
        response: Response,
        user_email: str,
        user_sub: str,
        role: UserRole,
    ) -> str:
        session_id = secrets.token_urlsafe(48)
        duration = self.settings.SESSION_ABSOLUTE_TIMEOUT_HOURS * 3600
        await self.repository.create_session(
            session_id=session_id,
            user_email=user_email,
            user_sub=user_sub,
            role=role,
            duration_seconds=duration,
        )

        is_production = self.settings.ENVIRONMENT.lower() == "production"

        # Set HttpOnly session cookie
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=session_id,
            max_age=duration,
            httponly=True,
            secure=is_production,
            samesite="lax",
        )

        # Set CSRF token cookie readable by frontend scripts
        csrf_token = generate_csrf_token(self.settings.SECRET_KEY, session_id)
        response.set_cookie(
            key=CSRF_COOKIE_NAME,
            value=csrf_token,
            max_age=duration,
            httponly=False,
            secure=is_production,
            samesite="lax",
        )

        return session_id

    async def clear_session(self, request: Request, response: Response) -> None:
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        if session_id:
            await self.repository.delete_session(session_id)
        response.delete_cookie(SESSION_COOKIE_NAME)
        response.delete_cookie(CSRF_COOKIE_NAME)

    async def get_current_user(self, request: Request) -> AuthenticatedUser:
        """Dependency resolving current session to AuthenticatedUser."""
        # Development offline bypass (Strictly rejected in production!)
        if self.settings.DEV_AUTH_BYPASS:
            if self.settings.ENVIRONMENT.lower() == "production":
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="DEV_AUTH_BYPASS is strictly prohibited in production mode!",
                )
            return AuthenticatedUser(
                user_email="admin@dev.local",
                user_sub="dev_admin_sub",
                role=UserRole.ADMINISTRATOR,
                session_id="dev_session_id",
                last_auth_time=time.time(),
            )

        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        if not session_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required.",
            )

        session_record = await self.repository.get_session(session_id)
        if not session_record:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired or invalid. Please sign in again.",
            )

        # Enforce idle timeout
        now = time.time()
        idle_limit = self.settings.SESSION_IDLE_TIMEOUT_MINUTES * 60
        if now - session_record.last_active_at > idle_limit:
            await self.repository.delete_session(session_id)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session timed out due to inactivity.",
            )

        return AuthenticatedUser(
            user_email=session_record.user_email,
            user_sub=session_record.user_sub,
            role=UserRole(session_record.role),
            session_id=session_record.session_id,
            last_auth_time=session_record.last_auth_time,
        )

    def require_role(self, allowed_roles: Set[UserRole]):
        """FastAPI dependency enforcing RBAC permissions."""
        async def dependency(user: AuthenticatedUser = Depends(self.get_current_user)):
            if user.role not in allowed_roles:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Forbidden: Action requires role in {[r.value for r in allowed_roles]}, but user has {user.role.value}.",
                )
            return user
        return dependency

    def verify_sensitive_action(
        self,
        user: AuthenticatedUser,
        typed_confirmation: str,
        expected_confirmation: str,
        max_age_seconds: int = MAX_SENSITIVE_ACTION_AGE_SECONDS,
    ) -> None:
        """
        Enforces recent authentication, role check, and typed confirmation.
        """
        if user.role not in {UserRole.OPERATOR, UserRole.ADMINISTRATOR}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Sensitive actions require Operator or Administrator privileges.",
            )

        if typed_confirmation != expected_confirmation:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Confirmation string mismatch. Expected '{expected_confirmation}'.",
            )

        time_since_auth = time.time() - user.last_auth_time
        if time_since_auth > max_age_seconds and not self.settings.DEV_AUTH_BYPASS:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Sensitive action requires re-authentication (last authenticated {int(time_since_auth // 60)}m ago).",
            )
