"""Registration allow-list.

docs/threat-model.md assumes a single-tenant internal deployment with "no public
registration". These tests make that true rather than merely asserted: only
addresses an administrator has approved may create an account.
"""

from __future__ import annotations

from backend.app.config import settings
from backend.app.models import ApprovedEmail, Role, User

PASSWORD = "a-long-enough-password"


def _register(client, email: str, **extra):
    return client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD, **extra}
    )


# --------------------------------------------------------------------------- #
# The gate itself
# --------------------------------------------------------------------------- #


def test_an_unapproved_address_is_refused(client):
    resp = _register(client, "stranger@example.com")

    assert resp.status_code == 403
    assert "not approved" in resp.json()["detail"]
    # The message has to say *why*, or an operator cannot act on it.
    assert "administrator" in resp.json()["detail"]


def test_an_approved_address_can_register(client, approve_email):
    approve_email("newjoiner@example.com")

    resp = _register(client, "newjoiner@example.com")
    assert resp.status_code == 201, resp.text
    assert resp.json()["email"] == "newjoiner@example.com"


def test_approval_is_case_insensitive(client, approve_email):
    """Mail systems treat local-parts case-insensitively; a list that lets
    Priya@ through but not priya@ is a bug waiting to be filed."""
    approve_email("priya@example.com")

    assert _register(client, "PRIYA@example.com").status_code == 201


def test_refusal_does_not_create_the_account(client, db):
    _register(client, "stranger@example.com")

    assert db.query(User).filter(User.email == "stranger@example.com").one_or_none() is None


def test_refusals_are_audited(client, db):
    """A burst of these is how you notice someone probing for a way in."""
    from backend.app.models import AuditEvent

    _register(client, "stranger@example.com")

    rows = db.query(AuditEvent).filter(AuditEvent.action == "registration.refused").all()
    assert rows, "a refused registration left no evidence"
    assert rows[0].detail["email"] == "stranger@example.com"
    assert rows[0].detail["reason"] == "not_on_approved_list"


def test_open_mode_lets_anyone_register(client, open_registration):
    """The switch still works -- turning the list off must not require emptying it."""
    assert _register(client, "anyone@example.com").status_code == 201


def test_using_an_approval_stamps_it(client, approve_email, db):
    """So an admin can see which invitations are still outstanding."""
    approve_email("newjoiner@example.com")
    entry = db.query(ApprovedEmail).filter(ApprovedEmail.email == "newjoiner@example.com").one()
    assert entry.used_at is None

    _register(client, "newjoiner@example.com")

    db.refresh(entry)
    assert entry.used_at is not None


# --------------------------------------------------------------------------- #
# Administering the list
# --------------------------------------------------------------------------- #


def test_only_an_admin_can_manage_the_list(client, auth):
    reviewer_headers, _ = auth(Role.REVIEWER)

    assert client.get("/api/v1/auth/approved-emails", headers=reviewer_headers).status_code == 403
    assert (
        client.post(
            "/api/v1/auth/approved-emails",
            headers=reviewer_headers,
            json={"email": "sneaky@example.com"},
        ).status_code
        == 403
    )


def test_an_admin_can_approve_and_the_address_then_works(client, auth):
    """The demo an examiner sees: refused, approved live, then accepted."""
    admin_headers, _ = auth(Role.ADMIN)

    assert _register(client, "contractor@example.com").status_code == 403

    added = client.post(
        "/api/v1/auth/approved-emails",
        headers=admin_headers,
        json={"email": "contractor@example.com", "note": "external auditor"},
    )
    assert added.status_code == 201
    assert added.json()["note"] == "external auditor"

    assert _register(client, "contractor@example.com").status_code == 201


def test_approving_twice_is_refused(client, auth, approve_email):
    admin_headers, _ = auth(Role.ADMIN)
    approve_email("dup@example.com")

    resp = client.post(
        "/api/v1/auth/approved-emails", headers=admin_headers, json={"email": "dup@example.com"}
    )
    assert resp.status_code == 409


def test_revoking_blocks_future_registration(client, auth):
    admin_headers, _ = auth(Role.ADMIN)
    entry = client.post(
        "/api/v1/auth/approved-emails", headers=admin_headers, json={"email": "temp@example.com"}
    ).json()

    client.delete(f"/api/v1/auth/approved-emails/{entry['id']}", headers=admin_headers)

    assert _register(client, "temp@example.com").status_code == 403


def test_revoking_does_not_disable_an_existing_account(client, auth, approve_email):
    """Tidying the list must not silently lock someone out -- deactivating a user
    is a separate, deliberate action."""
    admin_headers, _ = auth(Role.ADMIN)
    approve_email("staff@example.com")
    assert _register(client, "staff@example.com").status_code == 201

    entries = client.get("/api/v1/auth/approved-emails", headers=admin_headers).json()
    entry = next(e for e in entries if e["email"] == "staff@example.com")
    resp = client.delete(f"/api/v1/auth/approved-emails/{entry['id']}", headers=admin_headers)

    assert "still works" in resp.json()["detail"], "the consequence must be spelled out"
    login = client.post(
        "/api/v1/auth/login", json={"email": "staff@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200, "an existing account was collaterally disabled"


def test_administering_the_list_is_audited(client, auth, db):
    from backend.app.models import AuditEvent

    admin_headers, _admin = auth(Role.ADMIN)
    entry = client.post(
        "/api/v1/auth/approved-emails", headers=admin_headers, json={"email": "tracked@example.com"}
    ).json()
    client.delete(f"/api/v1/auth/approved-emails/{entry['id']}", headers=admin_headers)

    actions = {
        e.action
        for e in db.query(AuditEvent).filter(AuditEvent.action.like("registration.%")).all()
    }
    assert "registration.email_approved" in actions
    assert "registration.email_revoked" in actions


def test_the_policy_endpoint_is_public_and_honest(client):
    """The login screen reads this to explain itself instead of just refusing."""
    body = client.get("/api/v1/auth/registration-policy").json()

    assert body["mode"] == settings.registration_mode
    assert body["self_service"] is False
    assert "approved" in body["message"]


def test_rate_limiting_still_applies_before_the_allow_list(client):
    """An unapproved address must not be a free way to hammer the endpoint.

    The loop has to exceed the registration budget, which is deliberately wide
    (only refusals accumulate, since successes are refunded).
    """
    statuses = [_register(client, f"probe{i}@example.com").status_code for i in range(25)]
    assert 429 in statuses, "probing for approved addresses was not throttled"
