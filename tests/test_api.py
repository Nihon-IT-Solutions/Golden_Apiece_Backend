"""The admin / registration endpoints drive the engine correctly (no lifespan, so nothing touches the live DB)."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.auth import get_current_user, require_admin
from app.database import get_db
from app.main import app
from app.models import AutoPoolEntry, Commission, PlanLevel

from .conftest import assert_ledger_consistent, make_user, set_plan

D = Decimal


@pytest.fixture
def client(db, admin):
    def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_admin] = lambda: admin
    app.dependency_overrides[get_current_user] = lambda: admin
    yield TestClient(app)  # not used as a context manager -> startup seed() never runs
    app.dependency_overrides.clear()


def level_commissions(db, user):
    return db.scalar(select(func.count(Commission.id)).where(Commission.user_id == user.id)) or 0


def test_double_approve_pays_once_and_second_call_is_refused(client, db, root, pkg):
    set_plan(db, "level", [(100, 0, 0)])
    m = make_user(db, root, pkg, activate=False)
    db.commit()
    r1 = client.post(f"/api/admin/approvals/{m.id}", json={"action": "approve"})
    r2 = client.post(f"/api/admin/approvals/{m.id}", json={"action": "approve"})
    assert r1.status_code == 200 and r2.status_code == 404
    assert level_commissions(db, root) == 1
    assert_ledger_consistent(db)


def test_rejected_member_pays_nothing_and_cannot_be_approved_later(client, db, root, pkg):
    set_plan(db, "level", [(100, 0, 0)])
    m = make_user(db, root, pkg, activate=False)
    db.commit()
    assert client.post(f"/api/admin/approvals/{m.id}", json={"action": "reject"}).json()["message"].endswith("rejected")
    assert client.post(f"/api/admin/approvals/{m.id}", json={"action": "approve"}).status_code == 404
    assert level_commissions(db, root) == 0


def test_admin_registration_endpoint_activates_and_pays(client, db, root, pkg):
    set_plan(db, "level", [(100, 0, 0)])
    db.commit()
    r = client.post("/api/register", json={"sponsor_username": root.username, "package_id": pkg.id,
                                           "first_name": "Api", "password": "secret1"})
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert level_commissions(db, root) == 1


def test_blocking_and_unblocking_never_repays(client, db, root, pkg):
    set_plan(db, "level", [(100, 0, 0)])
    m = make_user(db, root, pkg)
    db.commit()
    client.post(f"/api/admin/members/{m.id}/status", json={"action": "block"})
    client.post(f"/api/admin/members/{m.id}/status", json={"action": "unblock"})
    assert m.status == "active" and level_commissions(db, root) == 1


def test_admin_auto_pool_endpoint_requires_an_active_member(client, db, root, pkg):
    m = make_user(db, root, pkg, activate=False)
    db.commit()
    assert client.post(f"/api/admin/members/{m.id}/autopool").status_code == 400
    r = client.post(f"/api/admin/members/{root.id}/autopool")
    assert r.status_code == 200 and db.scalar(select(func.count(AutoPoolEntry.id))) == 1


def test_plan_save_rejects_held_amounts_above_the_level_total(client, db):
    r = client.put("/api/admin/settings/plan/level", json=[{"level": 1, "amount": 10, "reward_amount": 101}])
    assert r.status_code == 400  # default width 10 -> level 1 total is 100


def test_plan_save_renumbers_levels_and_drops_auto_pool_fund_on_the_auto_pool_plan(client, db):
    r = client.put("/api/admin/settings/plan/autopool", json=[
        {"level": 7, "amount": 50, "reward_amount": 10, "autopool_amount": 5},
        {"level": 3, "amount": 100, "reward_amount": 20}])
    assert r.status_code == 200
    rows = db.scalars(select(PlanLevel).where(PlanLevel.plan == "autopool").order_by(PlanLevel.level)).all()
    assert [(p.level, D(p.amount), D(p.autopool_amount)) for p in rows] == [(1, D(100), D(0)), (2, D(50), D(0))]


def test_unknown_plan_is_404(client):
    assert client.put("/api/admin/settings/plan/binary", json=[]).status_code == 404
