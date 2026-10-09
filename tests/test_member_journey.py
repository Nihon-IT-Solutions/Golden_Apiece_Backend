"""One member's whole journey on the real plan sheets: level 1 and 2 of the sponsor tree, entry into the global
auto pool when level 2 completes (not before), then auto pool level 1 and 2 income and rewards."""
from decimal import Decimal

from sqlalchemy import func, select

from app.models import AutoPoolEntry
from app.seed import AUTOPOOL_PLAN, LEVEL_PLAN
from app.services import enter_autopool

from .conftest import assert_ledger_consistent, balance, comms, make_user, net_sum, reward, set_plan

D = Decimal


def entries_of(db, user):
    db.flush()
    return db.scalars(select(AutoPoolEntry).where(AutoPoolEntry.user_id == user.id)).all()


def test_99_members_at_level_2_are_not_enough_and_the_100th_triggers_the_entry(db, root, pkg):
    set_plan(db, "level", [(a, r, p) for _, a, r, p, _ in LEVEL_PLAN])
    set_plan(db, "autopool", [(a, r, 0) for _, a, r in AUTOPOOL_PLAN])
    you = root
    directs = [make_user(db, you, pkg) for _ in range(10)]
    for i in range(99):
        make_user(db, directs[i // 10], pkg)

    fund = reward(db, you, level=2, kind="autopool")
    assert (fund.status, fund.accrued_amount) == ("accruing", D("1188"))  # 99 x 12 of 1,200
    assert entries_of(db, you) == []

    make_user(db, directs[9], pkg)  # the 100th member at level 2

    assert (fund.status, fund.accrued_amount) == ("delivered", D("1200"))
    [entry] = entries_of(db, you)
    assert entry.source == "Level 2 completed"
    # level 2 paid exactly the sheet: 100 x 100 - 300 reward - 1,200 auto pool = 8,500
    assert net_sum(comms(db, you, level=2)) == D("8500")
    assert reward(db, you, level=2).status == "achieved"
    assert balance(db, you) == D("9400")  # 900 + 8,500
    assert_ledger_consistent(db)


def test_after_entering_the_pool_the_member_earns_auto_pool_level_1_and_2(db, root, pkg):
    set_plan(db, "level", [(a, r, p) for _, a, r, p, _ in LEVEL_PLAN])
    set_plan(db, "autopool", [(a, r, 0) for _, a, r in AUTOPOOL_PLAN])
    you = root
    directs = [make_user(db, you, pkg) for _ in range(10)]
    team = [make_user(db, d, pkg) for d in directs for _ in range(10)]
    [entry] = entries_of(db, you)
    assert entry.position == 0  # first in an empty global pool
    wallet_before = balance(db, you)

    # 5 entries placed under you complete auto pool level 1: 5 x 200 - 500 reward = 500
    for m in team[:5]:
        enter_autopool(db, m, "test")
    assert net_sum(comms(db, you, "autopool", level=1)) == D("500")
    r1 = reward(db, you, "autopool", 1, entry_id=entry.id)
    assert (r1.status, r1.accrued_amount) == ("achieved", D("500"))
    assert len(entries_of(db, you)) == 1  # completing 5 does NOT give another global entry

    # 25 more complete auto pool level 2: 25 x 200 - 3,000 reward = 2,000
    for m in team[5:30]:
        enter_autopool(db, m, "test")
    assert net_sum(comms(db, you, "autopool", level=2)) == D("2000")
    assert reward(db, you, "autopool", 2, entry_id=entry.id).status == "achieved"
    assert balance(db, you) - wallet_before == D("2500")
    assert db.scalar(select(func.count(AutoPoolEntry.id))) == 31
    assert_ledger_consistent(db)
