"""Global auto pool: placement, per-entry level income, rewards and edge cases."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import AutoPoolEntry, Reward
from app.seed import AUTOPOOL_PLAN
from app.services import autopool_level_counts, enter_autopool, to_dec

from .conftest import assert_ledger_consistent, comms, make_user, net_sum, reward, set_plan, set_setting

D = Decimal


@pytest.fixture
def members(db, root, pkg):
    return [root] + [make_user(db, root, pkg) for _ in range(9)]


def test_entries_fill_left_to_right_and_parent_is_position_minus_one_div_width(db, members):
    set_setting(db, "autopool_width", 2)
    entries = [enter_autopool(db, m) for m in members[:7]]
    assert [e.position for e in entries] == list(range(7))
    pos = {e.id: e.position for e in entries}
    assert [pos.get(e.parent_id) for e in entries] == [None, 0, 0, 1, 1, 2, 2]
    assert_ledger_consistent(db)


def test_each_ancestor_entry_is_paid_at_its_depth(db, members):
    set_setting(db, "autopool_width", 2)
    set_plan(db, "autopool", [(200, 0, 0), (100, 0, 0)])
    top = members[0]
    for m in members[:7]:
        enter_autopool(db, m)
    assert [(c.level, to_dec(c.amount)) for c in comms(db, top, "autopool")] == [(1, D(200))] * 2 + [(2, D(100))] * 4
    assert net_sum(comms(db, members[1], "autopool")) == D("400")  # its own two children at level 1
    assert comms(db, members[6], "autopool") == []
    assert_ledger_consistent(db)


def test_only_as_many_ancestors_as_plan_levels_are_paid(db, members):
    set_setting(db, "autopool_width", 1)  # a single chain
    set_plan(db, "autopool", [(10, 0, 0), (5, 0, 0)])
    for m in members[:5]:
        enter_autopool(db, m)
    assert [c.level for c in comms(db, members[0], "autopool")] == [1, 2]
    assert_ledger_consistent(db)


def test_reward_is_held_per_entry_and_achieved_when_its_level_fills(db, members):
    set_setting(db, "autopool_width", 2)
    set_plan(db, "autopool", [(200, 100, 0)])
    e0 = enter_autopool(db, members[0])
    enter_autopool(db, members[1])
    r = reward(db, members[0], "autopool", 1, entry_id=e0.id)
    assert (r.accrued_amount, r.status) == (D("50"), "accruing")
    enter_autopool(db, members[2])
    assert (r.accrued_amount, r.status) == (D("100"), "achieved")
    assert net_sum(comms(db, members[0], "autopool")) == D("300")
    assert_ledger_consistent(db)


def test_same_member_with_two_entries_earns_on_each_separately(db, members):
    set_setting(db, "autopool_width", 1)
    set_plan(db, "autopool", [(100, 20, 0), (50, 0, 0)])
    x, y = members[0], members[1]
    e0, e1 = enter_autopool(db, x), enter_autopool(db, x)  # x below x
    enter_autopool(db, y)
    by_entry = {(c.entry_id, c.level): to_dec(c.amount) for c in comms(db, x, "autopool")}
    assert by_entry == {(e0.id, 1): D("80"), (e1.id, 1): D("80"), (e0.id, 2): D("50")}
    rewards = db.scalars(select(Reward).where(Reward.user_id == x.id, Reward.plan == "autopool")).all()
    assert sorted(r.entry_id for r in rewards) == sorted([e0.id, e1.id])
    assert_ledger_consistent(db)


@pytest.mark.parametrize("bad_status", ["blocked", "pending"])
def test_inactive_ancestor_is_skipped_but_higher_ones_still_paid(db, members, bad_status):
    set_setting(db, "autopool_width", 1)
    set_plan(db, "autopool", [(100, 0, 0), (50, 0, 0)])
    enter_autopool(db, members[0])
    enter_autopool(db, members[1])
    members[1].status = bad_status
    enter_autopool(db, members[2])
    assert comms(db, members[1], "autopool") == []
    assert [c.level for c in comms(db, members[0], "autopool")] == [1, 2]


def test_head_missed_while_blocked_still_counts_so_the_reward_completes(db, members):
    set_plan(db, "autopool", [(200, 500, 0)])  # default width 5
    top = members[0]
    e0 = enter_autopool(db, top)
    enter_autopool(db, members[1])
    top.status = "blocked"
    enter_autopool(db, members[2])  # head 2 is not paid
    top.status = "active"
    for m in members[3:6]:
        enter_autopool(db, m)
    r = reward(db, top, "autopool", 1, entry_id=e0.id)
    assert (r.status, r.accrued_amount) == ("achieved", D("500"))
    assert len(comms(db, top, "autopool")) == 4
    assert net_sum(comms(db, top, "autopool")) == D("300")  # plan net 500 less the one 200 head it missed
    assert_ledger_consistent(db)


def test_last_head_missed_while_blocked_still_completes_the_reward(db, members):
    set_plan(db, "autopool", [(200, 500, 0)])
    top = members[0]
    e0 = enter_autopool(db, top)
    for m in members[1:5]:
        enter_autopool(db, m)
    top.status = "blocked"
    enter_autopool(db, members[5])
    r = reward(db, top, "autopool", 1, entry_id=e0.id)
    assert (r.status, r.accrued_amount) == ("achieved", D("400")) and r.achieved_at is not None
    assert_ledger_consistent(db)


def test_new_entry_never_reuses_a_position_after_an_entry_was_deleted(db, members):
    set_setting(db, "autopool_width", 2)
    entries = [enter_autopool(db, m) for m in members[:3]]
    db.delete(entries[1])
    db.flush()
    e = enter_autopool(db, members[3])
    assert e.position == 3
    assert e.parent_id is None  # its parent position (1) is gone; it is placed, not crashed


def test_auto_pool_income_never_funds_another_auto_pool_entry(db, members):
    set_setting(db, "autopool_width", 1)
    set_plan(db, "autopool", [(100, 0, 60)])  # auto pool fund on the auto pool plan would recurse forever
    for m in members[:4]:
        enter_autopool(db, m)
    assert db.scalar(select(AutoPoolEntry.id).offset(4)) is None
    assert all(to_dec(c.held_amount) == 0 for c in comms(db, members[0], "autopool"))
    assert db.scalar(select(Reward.id).where(Reward.plan == "autopool", Reward.kind == "autopool")) is None
    assert_ledger_consistent(db)


def test_real_auto_pool_level_1_pays_the_published_net(db, members):
    set_plan(db, "autopool", [(a, r, 0) for _, a, r in AUTOPOOL_PLAN])
    e0 = enter_autopool(db, members[0])
    for m in members[1:6]:  # default width 5
        enter_autopool(db, m)
    assert net_sum(comms(db, members[0], "autopool", level=1)) == D("500")  # 5 x 200 - 500 reward
    assert reward(db, members[0], "autopool", 1, entry_id=e0.id).status == "achieved"
    assert_ledger_consistent(db)


@pytest.mark.parametrize("position,total,width,depth,expected", [
    (0, 1, 2, 3, [0, 0, 0]),
    (0, 7, 2, 3, [2, 4, 0]),
    (0, 5, 2, 2, [2, 2]),
    (1, 7, 2, 2, [2, 0]),
    (2, 6, 2, 1, [1]),
    (0, 31, 5, 3, [5, 25, 0]),
    (3, 10, 3, 1, [0]),
])
def test_autopool_level_counts(position, total, width, depth, expected):
    assert autopool_level_counts(position, total, width, depth) == expected
