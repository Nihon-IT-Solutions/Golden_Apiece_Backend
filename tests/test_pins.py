"""Franchise joining pins: bulk purchase at price less franchise commission, transfer and registration with a pin."""
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.auth import hash_password
from app.models import Commission, Package, Pin, User
from app.seed import PACKAGES
from app.services import add_txn, buy_pins, register_member, to_dec, transfer_pins

from .conftest import assert_ledger_consistent, balance, comms, make_package, make_user, set_plan

D = Decimal


@pytest.fixture
def franchise(db, root):
    root.is_franchise = True
    root.txn_password_hash = hash_password("pin123")
    db.flush()
    return root


def fund(db, user, amount):
    add_txn(db, user.id, "credit", "fund_credit", amount, "test fund")
    db.flush()


def sheet_packages(db):
    out = []
    for name, code, price, pv, product, product_amt, comm, franchise, gst in PACKAGES:
        p = Package(name=name, code=code, price=price, pv=pv, product_name=product, product_amount=product_amt,
                    commission_amount=comm, franchise_commission=franchise, gst_percent=gst)
        db.add(p)
        out.append(p)
    db.flush()
    return out


def form(sponsor, pin, **kw):
    return {"sponsor_username": sponsor.username, "package_id": 0, "first_name": "New", "password": "secret1",
            "pin_code": pin.code if pin else "", **kw}


# ---------------------------------------------------------------- the franchise pin sheet
def test_franchise_pin_sheet_15_of_each_package_costs_32655(db, franchise):
    fund(db, franchise, 32655)
    for pkg in sheet_packages(db):
        buy_pins(db, franchise, pkg, 15)
    # 35,205 pin amount - 2,550 franchise commission = 32,655
    assert balance(db, franchise) == D("0")
    assert db.scalar(select(func.sum(Pin.price))) == D("35205")
    assert sum(to_dec(c.amount) for c in comms(db, franchise, "franchise")) == D("2550")
    assert db.scalar(select(func.count(Pin.id)).where(Pin.owner_id == franchise.id, Pin.status == "unused")) == 45
    assert_ledger_consistent(db)


def test_pin_codes_are_unique_and_carry_the_package_code(db, franchise, pkg):
    fund(db, franchise, 100000)
    pins = buy_pins(db, franchise, pkg, 50)
    assert len({p.code for p in pins}) == 50
    assert all(p.code.startswith(f"{pkg.code}-") for p in pins)


@pytest.mark.parametrize("setup,message", [
    (lambda db, u, p: setattr(u, "is_franchise", False), "Only active franchise"),
    (lambda db, u, p: setattr(u, "status", "blocked"), "Only active franchise"),
    (lambda db, u, p: setattr(p, "is_active", False), "valid package"),
])
def test_only_active_franchises_buy_active_packages(db, franchise, pkg, setup, message):
    fund(db, franchise, 100000)
    setup(db, franchise, pkg)
    with pytest.raises(HTTPException, match=message):
        buy_pins(db, franchise, pkg, 1)
    assert db.scalar(select(Pin.id)) is None


def test_purchase_needs_only_the_net_amount_and_fails_cleanly_below_it(db, franchise):
    pkg = make_package(db, price=549)
    pkg.franchise_commission = 20
    fund(db, franchise, 529 * 2 - D("0.01"))
    with pytest.raises(HTTPException, match="Insufficient"):
        buy_pins(db, franchise, pkg, 2)
    assert db.scalar(select(Pin.id)) is None
    fund(db, franchise, D("0.01"))
    buy_pins(db, franchise, pkg, 2)
    assert balance(db, franchise) == D("0")


@pytest.mark.parametrize("qty", [0, 501])
def test_quantity_limits(db, franchise, pkg, qty):
    fund(db, franchise, 10**7)
    with pytest.raises(HTTPException, match="Quantity"):
        buy_pins(db, franchise, pkg, qty)


# ---------------------------------------------------------------- registering with a pin
def test_pin_registration_activates_pays_uplines_and_uses_the_pin(db, franchise, admin):
    set_plan(db, "level", [(100, 0, 0)])
    pkg = make_package(db, price=549)
    pkg.franchise_commission = 20
    fund(db, franchise, 529)
    [pin] = buy_pins(db, franchise, pkg, 1)
    u = register_member(db, form(franchise, pin, package_id=999999), franchise, "pin")
    assert (u.status, u.package_id) == ("active", pkg.id)  # the pin decides the package
    assert (pin.status, pin.used_for_id) == ("used", u.id) and pin.used_at is not None
    # 529 fund - 549 pin + 20 franchise commission + 100 level income; no second franchise commission
    assert balance(db, franchise) == D("100")
    assert len(comms(db, franchise, "franchise")) == 1
    assert_ledger_consistent(db)


def test_pin_cannot_be_used_twice(db, franchise, pkg):
    fund(db, franchise, 1000)
    [pin] = buy_pins(db, franchise, pkg, 1)
    register_member(db, form(franchise, pin), franchise, "pin")
    with pytest.raises(HTTPException, match="already been used"):
        register_member(db, form(franchise, pin, username="TWICE"), franchise, "pin")
    assert db.scalar(select(User.id).where(User.username == "TWICE")) is None


def test_only_the_holder_can_use_a_pin(db, franchise, pkg):
    fund(db, franchise, 1000)
    [pin] = buy_pins(db, franchise, pkg, 1)
    other = make_user(db, franchise, pkg)
    for code in (pin.code, "NOPE-1234", ""):
        with pytest.raises(HTTPException, match="Pin not found"):
            register_member(db, form(other, None, pin_code=code), other, "pin")


def test_pin_code_is_case_insensitive_and_survives_a_retired_package(db, franchise, pkg):
    fund(db, franchise, 1000)
    [pin] = buy_pins(db, franchise, pkg, 1)
    pkg.is_active = False
    u = register_member(db, form(franchise, None, pin_code=f"  {pin.code.lower()} "), franchise, "pin")
    assert u.status == "active"


# ---------------------------------------------------------------- transfer
def test_transfer_moves_oldest_unused_pins_and_receiver_can_use_them(db, franchise, pkg):
    fund(db, franchise, 3000)
    pins = buy_pins(db, franchise, pkg, 3)
    member = make_user(db, franchise, pkg)
    register_member(db, form(franchise, pins[0]), franchise, "pin")
    moved = transfer_pins(db, franchise, member, pkg, 2)
    assert [p.id for p in moved] == [pins[1].id, pins[2].id]
    assert all(p.owner_id == member.id and p.purchased_by_id == franchise.id for p in moved)
    u = register_member(db, form(member, moved[0]), member, "pin")
    assert u.status == "active" and u.sponsor_id == member.id
    with pytest.raises(HTTPException, match="Pin not found"):
        register_member(db, form(franchise, moved[1]), franchise, "pin")


def test_transfer_rejects_bad_targets_and_short_stock(db, franchise, pkg, admin):
    fund(db, franchise, 1000)
    buy_pins(db, franchise, pkg, 1)
    member = make_user(db, franchise, pkg)
    for to in (None, franchise, admin):
        with pytest.raises(HTTPException, match="valid member"):
            transfer_pins(db, franchise, to, pkg, 1)
    with pytest.raises(HTTPException, match="only 1 unused"):
        transfer_pins(db, franchise, member, pkg, 2)
    assert db.scalar(select(Pin.owner_id)) == franchise.id


# ---------------------------------------------------------------- endpoints
@pytest.fixture
def member_client(db, franchise):
    from fastapi.testclient import TestClient

    from app.auth import get_current_user
    from app.database import get_db
    from app.main import app

    def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: franchise
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_buy_transfer_and_register_through_the_api(member_client, db, franchise, pkg):
    fund(db, franchise, 2000)
    member = make_user(db, franchise, pkg)
    db.commit()
    bad = member_client.post("/api/member/pins/buy", json={"package_id": pkg.id, "quantity": 2, "txn_password": "x"})
    assert bad.status_code == 400
    r = member_client.post("/api/member/pins/buy", json={"package_id": pkg.id, "quantity": 2, "txn_password": "pin123"})
    assert r.status_code == 200 and len(r.json()["codes"]) == 2
    listing = member_client.get("/api/member/pins").json()
    assert listing["summary"] == [{"package_id": pkg.id, "package": pkg.name, "unused": 2, "used": 0}]
    r = member_client.post("/api/register", json={"sponsor_username": franchise.username, "package_id": pkg.id,
                                                  "first_name": "Pin", "password": "secret1", "payment_method": "pin",
                                                  "pin_code": r.json()["codes"][0]})
    assert r.status_code == 200 and r.json()["status"] == "active"
    r = member_client.post("/api/member/pins/transfer", json={"to_username": member.username.lower(),
                                                              "package_id": pkg.id, "quantity": 1,
                                                              "txn_password": "pin123"})
    assert r.status_code == 200
    assert db.scalar(select(func.count(Pin.id)).where(Pin.owner_id == member.id)) == 1
    assert_ledger_consistent(db)


def test_admin_pin_list_and_plan_budget_warning(db, admin):
    from fastapi.testclient import TestClient

    from app.auth import get_current_user, require_admin
    from app.database import get_db
    from app.main import app
    from app.seed import LEVEL_PLAN

    def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_admin] = lambda: admin
    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        client = TestClient(app)
        sheet_packages(db)
        db.commit()
        plan = [{"level": lv, "amount": a, "reward_amount": r, "autopool_amount": p} for lv, a, r, p, _ in LEVEL_PLAN]
        warning = client.put("/api/admin/settings/plan/level", json=plan).json()["warning"]
        assert "370" in warning and "322" in warning
        assert client.get("/api/admin/settings").json()["level_plan_warning"] == warning
        plan[-1]["amount"] = plan[-2]["amount"] = 6  # 100+100+50+30+30+6+6 = 322
        assert client.put("/api/admin/settings/plan/level", json=plan).json()["warning"] is None
        r = client.get("/api/admin/pins").json()
        assert r["summary"] == {"unused": 0, "used": 0, "amount": 0.0, "commission": 0.0, "net": 0.0}
    finally:
        app.dependency_overrides.clear()
