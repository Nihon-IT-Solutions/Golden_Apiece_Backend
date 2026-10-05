"""Shared fixtures. Tests run on an in-memory SQLite database and never touch the real (Supabase) database."""
import os
import warnings
from collections import defaultdict
from decimal import Decimal

# Must be set before `app` is imported so the app's own engine can never point at the live database.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SEED_DEMO_DATA"] = "false"

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import SAWarning
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import AppSetting, AutoPoolEntry, Commission, Package, PlanLevel, Reward, User, WalletTransaction
from app.services import activate_member, to_dec

warnings.filterwarnings("ignore", category=SAWarning, message=".*Decimal objects natively.*")

D = Decimal


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(eng, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def SessionTest(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def db(SessionTest):
    s = SessionTest()
    yield s
    s.rollback()
    s.close()


# ---------------------------------------------------------------- builders
def set_setting(db, key, value):
    row = db.get(AppSetting, key)
    if row:
        row.value = str(value)
    else:
        db.add(AppSetting(key=key, value=str(value)))
    db.flush()


def set_plan(db, plan, rows):
    """rows: (amount, reward_amount, autopool_amount) per level, starting at level 1."""
    for old in db.scalars(select(PlanLevel).where(PlanLevel.plan == plan)).all():
        db.delete(old)
    db.flush()
    for i, (amount, reward, pool) in enumerate(rows, start=1):
        db.add(PlanLevel(plan=plan, level=i, amount=amount, reward_amount=reward, autopool_amount=pool,
                         reward_name=f"{plan} L{i}"))
    db.flush()


_seq = {"n": 0}


def make_package(db, price=1000, pv=100, franchise=0, active=True):
    _seq["n"] += 1
    p = Package(name=f"P{_seq['n']}", code=f"P{_seq['n']}", price=price, pv=pv, franchise_commission=franchise,
                is_active=active)
    db.add(p)
    db.flush()
    return p


def make_user(db, sponsor=None, package=None, status="pending", role="user", username=None, activate=True):
    _seq["n"] += 1
    u = User(username=username or f"U{_seq['n']:05d}", password_hash="x", txn_password_hash="x", role=role,
             status=status, first_name="T", sponsor_id=sponsor.id if sponsor else None,
             package_id=package.id if package else None)
    db.add(u)
    db.flush()
    db.refresh(u)
    if activate and status == "pending":
        activate_member(db, u)
        db.flush()
    return u


@pytest.fixture
def admin(db):
    return make_user(db, role="admin", status="active", username="ADMIN")


@pytest.fixture
def pkg(db):
    return make_package(db)


@pytest.fixture
def root(db, admin, pkg):
    """First real member, sponsored by the admin."""
    return make_user(db, admin, pkg)


# ---------------------------------------------------------------- queries
def comms(db, user, plan="level", level=None):
    db.flush()
    stmt = select(Commission).where(Commission.user_id == user.id, Commission.commission_type == plan)
    if level is not None:
        stmt = stmt.where(Commission.level == level)
    return db.scalars(stmt.order_by(Commission.id)).all()


def net_sum(rows):
    return sum((to_dec(c.amount) for c in rows), D("0"))


def held_sum(rows):
    return sum((to_dec(c.held_amount) for c in rows), D("0"))


def reward(db, user, plan="level", level=1, kind="reward", entry_id=None):
    db.flush()
    stmt = select(Reward).where(Reward.user_id == user.id, Reward.plan == plan, Reward.level == level,
                                Reward.kind == kind)
    stmt = stmt.where(Reward.entry_id == entry_id) if entry_id else stmt.where(Reward.entry_id.is_(None))
    return db.scalar(stmt)


def balance(db, user):
    db.flush()
    total = D("0")
    for t in db.scalars(select(WalletTransaction).where(WalletTransaction.user_id == user.id)):
        total += to_dec(t.amount) if t.type == "credit" else -to_dec(t.amount)
    return total


def assert_ledger_consistent(db, check_per_head=True):
    """Invariants that must hold after ANY sequence of engine operations."""
    db.flush()
    plans = {(p.plan, p.level): p for p in db.scalars(select(PlanLevel))}
    widths = {"level": int(db.get(AppSetting, "level_plan_width").value) if db.get(AppSetting, "level_plan_width") else 10,
              "autopool": int(db.get(AppSetting, "autopool_width").value) if db.get(AppSetting, "autopool_width") else 5}
    heads, held = defaultdict(int), defaultdict(lambda: D("0"))
    paid = defaultdict(lambda: D("0"))
    for c in db.scalars(select(Commission)):
        amt, h = to_dec(c.amount), to_dec(c.held_amount)
        assert amt >= 0 and h >= 0, f"negative commission {c.id}"
        paid[c.user_id] += amt
        if c.commission_type == "franchise":
            continue
        if check_per_head:
            assert amt + h == to_dec(plans[(c.commission_type, c.level)].amount), \
                f"commission {c.id}: net + held != per-head amount"
        key = (c.user_id, c.commission_type, c.level, c.entry_id)
        heads[key] += 1
        held[key] += h
    for (uid, plan, level, _), n in heads.items():
        assert n <= widths[plan] ** level, f"user {uid} {plan} L{level}: {n} heads exceeds capacity"
    # every rupee held back sits in a reward / auto pool fund of the same user, plan, level (and entry)
    accrued = defaultdict(lambda: D("0"))
    for r in db.scalars(select(Reward)):
        assert to_dec(r.accrued_amount) <= to_dec(r.target_amount), f"reward {r.id} over target"
        assert (r.status == "accruing") == (r.achieved_at is None), f"reward {r.id} status/achieved_at mismatch"
        accrued[(r.user_id, r.plan, r.level, r.entry_id)] += to_dec(r.accrued_amount)
    for key in set(held) | set(accrued):
        assert held[key] == accrued[key], f"{key}: held {held[key]} != rewards {accrued[key]}"
    # every commission rupee reached the wallet, and nothing else was booked as commission
    booked = defaultdict(lambda: D("0"))
    for t in db.scalars(select(WalletTransaction).where(WalletTransaction.category == "commission")):
        assert t.type == "credit"
        booked[t.user_id] += to_dec(t.amount)
    for uid in set(paid) | set(booked):
        assert paid[uid] == booked[uid], f"user {uid}: commissions {paid[uid]} != wallet {booked[uid]}"
    # auto pool matrix: unique positions, each parent is (position - 1) // width
    entries = {e.position: e for e in db.scalars(select(AutoPoolEntry))}
    for pos, e in entries.items():
        parent = entries.get((pos - 1) // widths["autopool"]) if pos else None
        assert e.parent_id == (parent.id if parent else None), f"auto pool position {pos} has the wrong parent"
