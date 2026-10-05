"""Real concurrency against PostgreSQL (the advisory lock cannot be exercised on SQLite).

Opt-in: set TEST_DATABASE_URL to an EMPTY throwaway Postgres database, e.g.
    $env:TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/mlm_test"
The test refuses to run if that database already has a `users` table, so it can never touch real data.
"""
import os
import threading
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import AutoPoolEntry, Commission, Package, PlanLevel, User
from app.services import activate_member, enter_autopool

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set (needs a throwaway Postgres database)")


@pytest.fixture
def pg():
    eng = create_engine(URL, connect_args={"prepare_threshold": None})
    if inspect(eng).has_table("users"):
        pytest.skip("TEST_DATABASE_URL is not empty - refusing to run against a database with data")
    Base.metadata.create_all(eng)
    try:
        yield sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    finally:
        Base.metadata.drop_all(eng)
        eng.dispose()


def run_parallel(n, fn):
    barrier, errors = threading.Barrier(n), []

    def worker(i):
        try:
            barrier.wait()
            fn(i)
        except Exception as e:  # noqa: BLE001 - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def setup(Session, members):
    with Session() as s:
        pkg = Package(name="P", code="P", price=1000, pv=100)
        s.add(pkg)
        s.add(PlanLevel(plan="level", level=1, amount=100, reward_amount=0, autopool_amount=0))
        s.add(PlanLevel(plan="autopool", level=1, amount=10, reward_amount=0, autopool_amount=0))
        root = User(username="ROOT", password_hash="x", txn_password_hash="x", first_name="R", status="active")
        s.add(root)
        s.flush()
        ids = []
        for i in range(members):
            u = User(username=f"M{i}", password_hash="x", txn_password_hash="x", first_name="M", status="pending",
                     sponsor_id=root.id, package_id=pkg.id)
            s.add(u)
            s.flush()
            ids.append(u.id)
        s.commit()
        return root.id, ids


def test_same_member_approved_by_many_requests_at_once_is_paid_once(pg):
    root_id, [mid] = setup(pg, 1)

    def approve(_):
        with pg() as s:
            activate_member(s, s.get(User, mid))
            s.commit()

    run_parallel(8, approve)
    with pg() as s:
        assert s.scalar(select(func.count(Commission.id)).where(Commission.user_id == root_id)) == 1
        assert s.get(User, root_id).group_pv == 100


def test_parallel_activations_never_exceed_level_capacity_and_group_pv_adds_up(pg):
    root_id, ids = setup(pg, 14)  # default width 10 -> only 10 heads may be paid

    def approve(i):
        with pg() as s:
            activate_member(s, s.get(User, ids[i]))
            s.commit()

    run_parallel(14, approve)
    with pg() as s:
        amounts = s.scalars(select(Commission.amount).where(Commission.user_id == root_id)).all()
        assert len(amounts) == 10 and sum(amounts) == Decimal("1000")
        assert s.get(User, root_id).group_pv == 1400  # no lost updates


def test_parallel_auto_pool_entries_get_unique_contiguous_positions(pg):
    _, ids = setup(pg, 12)
    with pg() as s:
        for uid in ids:
            s.get(User, uid).status = "active"
        s.commit()

    def enter(i):
        with pg() as s:
            enter_autopool(s, s.get(User, ids[i]))
            s.commit()

    run_parallel(12, enter)
    with pg() as s:
        assert sorted(s.scalars(select(AutoPoolEntry.position)).all()) == list(range(12))
