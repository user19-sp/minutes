"""One-click demo sign-in.

The login screen offers a demo button so a reader can look around without
registering. Availability is decided by the server, not the frontend, for one
reason: on a fresh database where `scripts/seed_demo.py` has not been run, the
seeded account does not exist, and a button that fails on click is worse than no
button.
"""

from __future__ import annotations

import pytest

from backend.app.config import settings
from backend.app.models import Role, User
from backend.app.security.auth import hash_password


@pytest.fixture
def demo_account(db):
    """Create the account `seed_demo.py` would have created."""
    user = User(
        email=settings.demo_email.lower(),
        full_name="Demo Reviewer",
        hashed_password=hash_password(settings.demo_password),
        role=Role.REVIEWER,
    )
    db.add(user)
    db.commit()
    return user


def test_unavailable_when_the_account_does_not_exist(client):
    """A fresh database must not advertise a login that cannot work."""
    body = client.get("/api/v1/auth/demo").json()

    assert body["available"] is False
    assert "seed_demo" in body["reason"], "should say how to fix it"
    assert "password" not in body, "no credentials when unavailable"


def test_available_once_the_account_exists(client, demo_account):
    body = client.get("/api/v1/auth/demo").json()

    assert body["available"] is True
    assert body["email"] == settings.demo_email
    assert body["role"] == "reviewer"


def test_the_advertised_credentials_actually_work(client, demo_account):
    """What the endpoint hands out must sign in -- otherwise the button lies."""
    demo = client.get("/api/v1/auth/demo").json()

    resp = client.post(
        "/api/v1/auth/login",
        json={"email": demo["email"], "password": demo["password"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["email"] == settings.demo_email


def test_demo_mode_off_hides_it_entirely(client, demo_account, monkeypatch):
    """Turning demo mode off must suppress it even when the account exists --
    that is the switch a real deployment uses."""
    monkeypatch.setattr(settings, "demo_mode", False)

    body = client.get("/api/v1/auth/demo").json()
    assert body["available"] is False
    assert "disabled" in body["reason"]
    assert "password" not in body


def test_an_inactive_demo_account_is_not_offered(client, demo_account, db):
    demo_account.is_active = False
    db.commit()

    assert client.get("/api/v1/auth/demo").json()["available"] is False


def test_the_endpoint_is_public(client, demo_account):
    """It has to be readable by the login screen, before anyone has a token."""
    resp = client.get("/api/v1/auth/demo")  # no Authorization header
    assert resp.status_code == 200


def test_the_demo_account_is_an_ordinary_reviewer(client, demo_account):
    """It must not be a privileged back door -- it is a normal account that
    happens to be seeded, bound by exactly the same role rules."""
    demo = client.get("/api/v1/auth/demo").json()
    token = client.post(
        "/api/v1/auth/login", json={"email": demo["email"], "password": demo["password"]}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/v1/auth/me", headers=headers).json()["role"] == "reviewer"
    # A reviewer cannot read the global audit trail; admin-only still holds.
    assert client.get("/api/v1/audit", headers=headers).status_code == 403


def test_demo_login_is_still_rate_limited(client, demo_account):
    """The button uses the normal login path, so brute-force protection applies
    to it too -- it is not a bypass."""
    demo = client.get("/api/v1/auth/demo").json()

    statuses = [
        client.post(
            "/api/v1/auth/login", json={"email": demo["email"], "password": "wrong-password"}
        ).status_code
        for _ in range(8)
    ]
    assert 429 in statuses
