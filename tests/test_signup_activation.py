"""Members join without admin approval: they can log in at once, admins get a mail, and the uplines are only paid
once the package is paid (pin / e-wallet by the member, or activated by the admin)."""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import require_user
from app.database import get_db
from app.main import app
from app.models import Mail, Pin
from app.services import activate_with_payment, add_txn, register_member

from .conftest import assert_ledger_consistent, balance, comms, make_package, set_plan


def form(sponsor, pkg, **kw):
    return {"sponsor_username": sponsor.username, "package_id": pkg.id, "first_name": "New", "password": "secret1",
            **kw}


@pytest.fixture
def client(db):
    def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    yield TestClient(app)  # not used as a context manager -> startup seed() never runs
    app.dependency_overrides.clear()


def give_pin(db, code, pkg, owner, buyer):
    db.add(Pin(code=code, package_id=pkg.id, owner_id=owner.id, purchased_by_id=buyer.id, price=pkg.price))


def admin_mails(db, admin):
    db.flush()
    return db.scalars(select(Mail).where(Mail.recipient_id == admin.id)).all()


def test_signup_can_login_at_once_and_admin_is_mailed(client, db, admin, root, pkg):
    set_plan(db, "level", [(100, 0, 0)])
    db.commit()
    r = client.post("/api/auth/signup", json=form(root, pkg))
    assert r.status_code == 200 and r.json()["status"] == "pending"
    username = r.json()["username"]

    login = client.post("/api/auth/login", json={"username": username, "password": "secret1", "portal": "user"})
    assert login.status_code == 200 and login.json()["token"]

    [mail] = admin_mails(db, admin)
    assert mail.subject == f"New member {username} joined" and "self sign-up" in mail.body
    assert comms(db, root) == []  # nothing paid until the package is paid


def test_member_and_admin_registrations(db, admin, root, pkg):
    register_member(db, form(root, pkg), root, "pending")
    assert "joined by " + root.username in admin_mails(db, admin)[0].body
    register_member(db, form(root, pkg), admin, "admin")
    assert len(admin_mails(db, admin)) == 1  # the admin is not told about members they added themselves


def test_self_activation_with_pin_pays_the_uplines(db, root, pkg):
    set_plan(db, "level", [(100, 0, 0)])
    other = make_package(db, price=2000)
    u = register_member(db, form(root, pkg), None, "pending")
    give_pin(db, "PIN-1", other, u, root)
    db.flush()
    activate_with_payment(db, u, "pin", " pin-1 ")
    assert u.status == "active" and u.package_id == other.id and len(comms(db, root)) == 1
    assert db.scalar(select(Pin).where(Pin.code == "PIN-1")).status == "used"
    with pytest.raises(HTTPException, match="already active"):
        activate_with_payment(db, u, "pin", "PIN-1")
    assert len(comms(db, root)) == 1
    assert_ledger_consistent(db)


def test_self_activation_with_ewallet(db, root, pkg):
    set_plan(db, "level", [(100, 0, 0)])
    u = register_member(db, form(root, pkg, txn_password="tx1234"), None, "pending")
    with pytest.raises(HTTPException, match="Insufficient"):
        activate_with_payment(db, u, "ewallet", txn_password="tx1234")
    add_txn(db, u.id, "credit", "fund_transfer", 1200, "from upline")
    db.flush()
    with pytest.raises(HTTPException, match="transaction password"):
        activate_with_payment(db, u, "ewallet", txn_password="wrong")
    assert u.status == "pending"
    activate_with_payment(db, u, "ewallet", txn_password="tx1234")
    assert u.status == "active" and balance(db, u) == 200 and len(comms(db, root)) == 1
    assert_ledger_consistent(db)


def test_someone_elses_pin_is_refused(db, admin, root, pkg):
    u = register_member(db, form(root, pkg), None, "pending")
    give_pin(db, "PIN-2", pkg, root, root)
    db.flush()
    with pytest.raises(HTTPException, match="not found in your account"):
        activate_with_payment(db, u, "pin", "PIN-2")
    assert u.status == "pending"


def test_activate_endpoint(client, db, root, pkg):
    u = register_member(db, form(root, pkg), None, "pending")
    give_pin(db, "PIN-3", pkg, u, root)
    db.commit()
    app.dependency_overrides[require_user] = lambda: u
    r = client.post("/api/member/activate", json={"payment_method": "pin", "pin_code": "PIN-3"})
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert client.post("/api/member/activate", json={"payment_method": "cash"}).status_code == 400
