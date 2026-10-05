"""register_member validation, e-wallet payment, franchise commission, package break-up and the engine lock."""
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import services
from app.auth import hash_password
from app.models import Commission, User
from app.services import add_txn, engine_lock, package_breakup, register_member, to_dec

from .conftest import assert_ledger_consistent, balance, comms, make_package, set_plan

D = Decimal


def form(sponsor, pkg, **kw):
    return {"sponsor_username": sponsor.username, "package_id": pkg.id, "first_name": "New", "password": "secret1",
            **kw}


@pytest.fixture
def registrar(db, root):
    root.txn_password_hash = hash_password("pin123")
    add_txn(db, root.id, "credit", "fund_credit", 1500, "test fund")
    db.flush()
    return root


def test_admin_registration_activates_and_pays_the_sponsor(db, root, pkg):
    set_plan(db, "level", [(100, 0, 0)])
    u = register_member(db, form(root, pkg, username=" new1 "), None, "admin")
    assert (u.username, u.status, u.sponsor_id) == ("NEW1", "active", root.id)
    assert len(comms(db, root)) == 1
    assert_ledger_consistent(db)


def test_pending_registration_pays_nothing(db, root, pkg):
    set_plan(db, "level", [(100, 0, 0)])
    u = register_member(db, form(root, pkg), None, "pending")
    assert u.status == "pending" and u.username.startswith("GA") and comms(db, root) == []


def test_ewallet_registration_debits_registrar_activates_and_pays_franchise(db, registrar, pkg):
    set_plan(db, "level", [(100, 0, 0)])
    registrar.is_franchise = True
    pkg.franchise_commission = 50
    u = register_member(db, form(registrar, pkg, txn_password_confirm="pin123"), registrar, "ewallet")
    assert u.status == "active"
    # 1500 fund - 1000 package + 100 level income + 50 franchise
    assert balance(db, registrar) == D("650")
    f = db.scalar(select(Commission).where(Commission.commission_type == "franchise"))
    assert (f.user_id, f.from_user_id, to_dec(f.amount)) == (registrar.id, u.id, D("50"))
    assert_ledger_consistent(db)


def test_non_franchise_registrar_gets_no_franchise_commission(db, registrar, pkg):
    pkg.franchise_commission = 50
    register_member(db, form(registrar, pkg, txn_password_confirm="pin123"), registrar, "ewallet")
    assert db.scalar(select(Commission.id).where(Commission.commission_type == "franchise")) is None


def test_ewallet_with_exactly_enough_balance_is_allowed(db, registrar):
    exact = make_package(db, price=1500)
    register_member(db, form(registrar, exact, txn_password_confirm="pin123"), registrar, "ewallet")
    assert balance(db, registrar) == D("0")


@pytest.mark.parametrize("change,message", [
    (lambda f, db: f.update(txn_password_confirm="wrong"), "Invalid transaction password"),
    (lambda f, db: f.update(package_id=make_package(db, price=1500.01).id), "Insufficient e-wallet balance"),
])
def test_ewallet_rejections_leave_no_trace(db, registrar, pkg, change, message):
    f = form(registrar, pkg, txn_password_confirm="pin123", username="NOPE")
    change(f, db)
    with pytest.raises(HTTPException, match=message):
        register_member(db, f, registrar, "ewallet")
    assert db.scalar(select(User.id).where(User.username == "NOPE")) is None
    assert balance(db, registrar) == D("1500")


def test_ewallet_needs_a_registrar(db, root, pkg):
    with pytest.raises(HTTPException, match="logged-in member"):
        register_member(db, form(root, pkg), None, "ewallet")


@pytest.mark.parametrize("mutate,message", [
    (lambda f: f.update(sponsor_username="NOBODY"), "Sponsor username not found"),
    (lambda f: f.update(package_id=999999), "valid package"),
    (lambda f: f.update(password="123"), "at least 6"),
    (lambda f: f.update(username="admin"), "already taken"),
])
def test_invalid_registrations_are_rejected(db, root, pkg, admin, mutate, message):
    f = form(root, pkg)
    mutate(f)
    with pytest.raises(HTTPException, match=message):
        register_member(db, f, None, "admin")


def test_inactive_sponsor_or_package_is_rejected(db, root, pkg):
    root.status = "blocked"
    with pytest.raises(HTTPException, match="not active"):
        register_member(db, form(root, pkg), None, "admin")
    root.status = "active"
    with pytest.raises(HTTPException, match="valid package"):
        register_member(db, form(root, make_package(db, active=False)), None, "admin")


def test_unknown_payment_type_is_rejected(db, root, pkg):
    with pytest.raises(HTTPException, match="Unknown payment"):
        register_member(db, form(root, pkg), None, "free")


def test_package_breakup_balances_to_the_paisa():
    from app.models import Package
    p = Package(price=999, commission_amount=322, product_amount=200, franchise_commission=100, gst_percent=18,
                product_name="X")
    b = package_breakup(p)
    assert b["gst_amount"] == 179.82
    assert b["balance"] == round(999 - 322 - 200 - 100 - 179.82, 2)


# ---------------------------------------------------------------- engine lock
def test_engine_lock_is_taken_once_per_transaction_on_postgres(db, monkeypatch):
    calls = []
    real_execute = db.execute
    monkeypatch.setattr(services, "_is_postgres", lambda _db: True)
    monkeypatch.setattr(db, "execute", lambda stmt, params=None, *a, **k:
                        calls.append((str(stmt), params)) if "advisory" in str(stmt) else real_execute(stmt, params))
    engine_lock(db)
    engine_lock(db)  # re-entrant within the same transaction
    assert calls == [("SELECT pg_advisory_xact_lock(:k)", {"k": 724001})]
    db.commit()
    engine_lock(db)  # a new transaction takes it again
    assert len(calls) == 2


def test_engine_lock_keeps_pending_changes(db, root):
    root.first_name = "Changed"
    engine_lock(db)
    assert root.first_name == "Changed"
