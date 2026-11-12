"""Unit tests for Google OIDC, PKCE, Sessions, CSRF, and RBAC security gates."""

import time

import pytest
from fastapi import HTTPException
from liquidator.auth.oidc import GoogleOIDCClient, generate_pkce_pair
from liquidator.auth.security import (
    AuthenticatedUser,
    SecurityManager,
    generate_csrf_token,
    verify_csrf_token,
)
from liquidator.core.config import AppSettings
from liquidator.core.models import UserRole


def test_pkce_generation():
    """Verify high-entropy PKCE pair generation."""
    pkce = generate_pkce_pair()
    assert len(pkce.code_verifier) >= 64
    assert len(pkce.code_challenge) >= 40
    # Must be URL-safe (no '=' or '/')
    assert "=" not in pkce.code_challenge


def test_allowlist_authorization_and_role_mapping():
    """Verify allowlist enforcement and admin role assignment."""
    settings = AppSettings(
        AUTH_ALLOWLIST="operator@example.com,lead@example.com",
        ADMIN_ALLOWLIST="admin@example.com",
        DEV_AUTH_BYPASS=False,
    )
    oidc = GoogleOIDCClient(settings)

    # Authorized operator
    auth_op, role_op, _ = oidc.verify_and_authorize_identity({
        "email": "operator@example.com",
        "email_verified": True,
        "sub": "google_sub_123",
    })
    assert auth_op is True
    assert role_op == UserRole.OPERATOR

    # Authorized admin
    auth_adm, role_adm, _ = oidc.verify_and_authorize_identity({
        "email": "admin@example.com",
        "email_verified": True,
        "sub": "google_sub_admin",
    })
    assert auth_adm is True
    assert role_adm == UserRole.ADMINISTRATOR

    # Unauthorized email
    auth_unauth, role_unauth, msg = oidc.verify_and_authorize_identity({
        "email": "attacker@example.com",
        "email_verified": True,
        "sub": "google_sub_999",
    })
    assert auth_unauth is False
    assert role_unauth is None
    assert "not authorized" in msg


def test_empty_allowlist_denies_all_access():
    """Verify empty allowlist fails closed and denies all logins."""
    settings = AppSettings(
        AUTH_ALLOWLIST="",
        ADMIN_ALLOWLIST="",
        DEV_AUTH_BYPASS=False,
    )
    oidc = GoogleOIDCClient(settings)

    auth, role, msg = oidc.verify_and_authorize_identity({
        "email": "anyone@example.com",
        "email_verified": True,
        "sub": "sub_1",
    })
    assert auth is False
    assert "allowlist is empty" in msg


def test_csrf_token_generation_and_validation():
    """Verify session-bound CSRF token validation."""
    secret = "test-secret-key-12345"
    session_id = "sess_abc_123"
    token = generate_csrf_token(secret, session_id)

    assert verify_csrf_token(secret, session_id, token) is True
    # Tampered token or wrong session must fail
    assert verify_csrf_token(secret, "wrong_session", token) is False
    assert verify_csrf_token(secret, session_id, token + "x") is False


def test_sensitive_action_verification():
    """Verify recent authentication, role check, and typed confirmation."""
    settings = AppSettings(DEV_AUTH_BYPASS=False)
    sec_mgr = SecurityManager(settings, None)

    # Valid operator with recent auth
    user_recent = AuthenticatedUser(
        user_email="admin@example.com",
        user_sub="sub_admin",
        role=UserRole.ADMINISTRATOR,
        session_id="sess_1",
        last_auth_time=time.time() - 60,  # 1 min ago
    )

    # Success case
    sec_mgr.verify_sensitive_action(
        user=user_recent,
        typed_confirmation="CONFIRM_LIVE",
        expected_confirmation="CONFIRM_LIVE",
    )

    # Failure case 1: Mismatched confirmation string
    with pytest.raises(HTTPException) as exc1:
        sec_mgr.verify_sensitive_action(
            user=user_recent,
            typed_confirmation="WRONG_STRING",
            expected_confirmation="CONFIRM_LIVE",
        )
    assert exc1.value.status_code == 400

    # Failure case 2: Stale session (> 15 mins)
    user_stale = AuthenticatedUser(
        user_email="admin@example.com",
        user_sub="sub_admin",
        role=UserRole.ADMINISTRATOR,
        session_id="sess_2",
        last_auth_time=time.time() - 1000,  # > 16 mins ago
    )
    with pytest.raises(HTTPException) as exc2:
        sec_mgr.verify_sensitive_action(
            user=user_stale,
            typed_confirmation="CONFIRM_LIVE",
            expected_confirmation="CONFIRM_LIVE",
        )
    assert exc2.value.status_code == 403
    assert "re-authentication" in exc2.value.detail
