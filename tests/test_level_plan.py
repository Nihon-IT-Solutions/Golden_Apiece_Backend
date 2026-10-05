"""Sponsor level income: per-head pay, capacity, held rewards / auto pool fund, PV and idempotency."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import AutoPoolEntry, Commission, PlanLevel, WalletTransaction
from app.seed import LEVEL_PLAN
from app.services import _share, activate_member, setting_int, to_dec

from .conftest import (assert_ledger_consistent, balance, comms, held_sum, make_package, make_user, net_sum, reward,
                       set_plan, set_setting)

D = Decimal


# ---------------------------------------------------------------- rounding
@pytest.mark.parametrize("total", ["0", "0.01", "1", "100", "1200", "333.33", "1000.01", "17656250"])
@pytest.mark.parametrize("capacity", [1, 2, 3, 7, 10, 100, 625, 1000])
def test_share_splits_total_exactly_with_no_rounding_drift(total, capacity):
    total = D(total)
    shares = [_share(total, k, capacity) for k in range(1, capacity + 1)]
    assert sum(shares) == total
    assert all(s >= 0 for s in shares)
    assert max(shares) - min(shares) <= D("0.01")  # spread as evenly as paise allow


@pytest.mark.parametrize("raw,expected", [("2", 2), ("3.0", 3), ("0", 1), ("-4", 1), ("abc", 10), ("", 10),
                                          ("nan", 10), ("inf", 10)])
def test_bad_width_settings_fall_back_safely(db, raw, expected):
    set_setting(db, "level_plan_width", raw)
    assert setting_int(db, "level_plan_width") == expected


# ---------------------------------------------------------------- basic pay
def test_direct_sponsor_is_paid_level_1_minus_held_reward(db, root, pkg):
    set_plan(db, "level", [(100, 100, 0)])  # 10 wide: reward 100 over 10 heads = 10 held per head
    m = make_user(db, root, pkg)
    [c] = comms(db, root)
    assert (to_dec(c.amount), to_dec(c.held_amount), c.level, c.from_user_id) == (D("90"), D("10"), 1, m.id)
    assert balance(db, root) == D("90")
    txn = db.scalar(select(WalletTransaction).where(WalletTransaction.user_id == root.id))
    assert (txn.type, txn.category, txn.reference_user_id) == ("credit", "commission", m.id)
    assert reward(db, root).accrued_amount == D("10") and reward(db, root).status == "accruing"
    assert_ledger_consistent(db)


def test_each_upline_paid_at_its_own_level(db, root, pkg):
    set_plan(db, "level", [(100, 0, 0), (50, 0, 0), (30, 0, 0)])
    a = make_user(db, root, pkg)
    b = make_user(db, a, pkg)
    c = make_user(db, b, pkg)
    assert [(x.level, to_dec(x.amount)) for x in comms(db, root)] == [(1, D("100")), (2, D("50")), (3, D("30"))]
    assert [(x.level, to_dec(x.amount)) for x in comms(db, a)] == [(1, D("100")), (2, D("50"))]
    assert [(x.level, to_dec(x.amount)) for x in comms(db, b)] == [(1, D("100"))]
    assert comms(db, c) == []
    assert_ledger_consistent(db)


def test_uplines_beyond_the_last_plan_level_get_nothing_but_still_get_group_pv(db, root, pkg):
    set_plan(db, "level", [(100, 0, 0)])
    a = make_user(db, root, pkg)
    make_user(db, a, pkg)
    assert [x.level for x in comms(db, root)] == [1]  # nothing for being two levels up
    assert root.group_pv == 2 * pkg.pv and a.group_pv == pkg.pv
    assert_ledger_consistent(db)


def test_personal_pv_comes_from_the_package_and_group_pv_reaches_every_upline(db, admin, root):
    small, big = make_package(db, pv=50), make_package(db, pv=200)
    a = make_user(db, root, small)
    make_user(db, a, big)
    nopkg = make_user(db, a, None)
    assert a.personal_pv == 50 and nopkg.personal_pv == 0
    assert a.group_pv == 200 and root.group_pv == 250 and admin.group_pv == 250 + root.personal_pv


def test_heads_beyond_level_capacity_are_not_paid(db, root, pkg):
    set_setting(db, "level_plan_width", 2)
    set_plan(db, "level", [(100, 0, 0)])
    for _ in range(3):
        make_user(db, root, pkg)
    assert len(comms(db, root)) == 2
    assert balance(db, root) == D("200")
    assert_ledger_consistent(db)


def test_zero_amount_level_still_counts_heads_but_books_no_wallet_entry(db, root, pkg):
    set_setting(db, "level_plan_width", 2)
    set_plan(db, "level", [(0, 0, 0)])
    make_user(db, root, pkg)
    [c] = comms(db, root)
    assert to_dec(c.amount) == 0
    assert db.scalar(select(WalletTransaction.id).where(WalletTransaction.user_id == root.id)) is None
    assert_ledger_consistent(db)


# ---------------------------------------------------------------- who gets paid
def test_admin_upline_is_never_paid(db, admin, pkg):
    set_plan(db, "level", [(100, 0, 0)])
    make_user(db, admin, pkg)
    assert comms(db, admin) == []


@pytest.mark.parametrize("bad_status", ["blocked", "pending", "rejected"])
def test_inactive_upline_is_skipped_and_the_next_one_keeps_its_own_level(db, root, pkg, bad_status):
    set_plan(db, "level", [(100, 0, 0), (50, 0, 0)])
    mid = make_user(db, root, pkg)
    mid.status = bad_status
    make_user(db, mid, pkg)
    assert comms(db, mid) == []
    assert [(x.level, to_dec(x.amount)) for x in comms(db, root, level=2)] == [(2, D("50"))]
    assert_ledger_consistent(db)


def test_skipped_head_does_not_use_up_the_upline_capacity(db, root, pkg):
    set_setting(db, "level_plan_width", 2)
    set_plan(db, "level", [(100, 0, 0)])
    root.status = "blocked"
    make_user(db, root, pkg)  # root blocked: not paid, not counted
    root.status = "active"
    make_user(db, root, pkg)
    make_user(db, root, pkg)
    assert len(comms(db, root)) == 2


def test_pending_member_pays_nothing_until_activated(db, root, pkg):
    set_plan(db, "level", [(100, 0, 0)])
    m = make_user(db, root, pkg, activate=False)
    assert comms(db, root) == [] and root.group_pv == 0
    activate_member(db, m)
    assert len(comms(db, root)) == 1 and m.status == "active" and m.activated_at is not None


# ---------------------------------------------------------------- idempotency
def test_activating_twice_pays_only_once(db, root, pkg):
    set_plan(db, "level", [(100, 0, 0)])
    m = make_user(db, root, pkg)
    activate_member(db, m)
    activate_member(db, m)
    assert len(comms(db, root)) == 1 and balance(db, root) == D("100")
    assert root.group_pv == pkg.pv


@pytest.mark.parametrize("status", ["blocked", "rejected", "active"])
def test_activate_does_nothing_for_a_member_that_is_not_pending(db, root, pkg, status):
    set_plan(db, "level", [(100, 0, 0)])
    m = make_user(db, root, pkg, status=status, activate=False)
    activate_member(db, m)
    assert m.status == status and comms(db, root) == []


def test_member_reset_to_pending_after_being_paid_out_is_never_paid_again(db, root, pkg):
    set_plan(db, "level", [(100, 0, 0)])
    m = make_user(db, root, pkg)
    m.status, m.activated_at = "pending", None  # e.g. edited by hand in the database
    activate_member(db, m)
    assert m.status == "active" and m.activated_at is not None
    assert len(comms(db, root)) == 1 and root.group_pv == pkg.pv


def test_sponsor_cycle_does_not_hang_and_pays_each_upline_once(db, admin, pkg):
    set_plan(db, "level", [(100, 0, 0)] * 5)
    a = make_user(db, admin, pkg)
    b = make_user(db, a, pkg)
    a.sponsor_id = b.id  # corrupt data: a <-> b
    db.flush()
    db.expire_all()
    make_user(db, b, pkg)
    assert len(comms(db, b)) == 1 and len(comms(db, a)) == 2  # a: from b (before the cycle) + level 2


# ---------------------------------------------------------------- held reward & auto pool fund
def full_tree(db, top, pkg, width, depth):
    level, out = [top], []
    for _ in range(depth):
        nxt = [make_user(db, p, pkg) for p in level for _ in range(width)]
        out.append(nxt)
        level = nxt
    return out


def test_completed_levels_match_the_plan_sheet_exactly(db, root, pkg):
    set_setting(db, "level_plan_width", 2)
    set_plan(db, "level", [(100, 50, 0), (50, 30, 70)])
    full_tree(db, root, pkg, 2, 2)

    l1, l2 = comms(db, root, level=1), comms(db, root, level=2)
    assert net_sum(l1) == D("150") and held_sum(l1) == D("50")  # 2 x 100 - 50 reward
    assert net_sum(l2) == D("100") and held_sum(l2) == D("100")  # 4 x 50 - 30 reward - 70 pool
    r1, r2 = reward(db, root, level=1), reward(db, root, level=2)
    assert (r1.accrued_amount, r1.status) == (D("50"), "achieved") and r1.achieved_at is not None
    assert (r2.accrued_amount, r2.status) == (D("30"), "achieved")
    pool = reward(db, root, level=2, kind="autopool")
    assert (pool.accrued_amount, pool.status) == (D("70"), "delivered") and pool.delivered_at is not None
    assert db.scalar(select(AutoPoolEntry).where(AutoPoolEntry.user_id == root.id)).source == "Level 2 completed"
    assert balance(db, root) == D("250")
    assert_ledger_consistent(db)


def test_reward_stays_accruing_until_the_last_head_of_the_level(db, root, pkg):
    set_setting(db, "level_plan_width", 3)
    set_plan(db, "level", [(100, 100, 0)])
    make_user(db, root, pkg)
    make_user(db, root, pkg)
    r = reward(db, root)
    assert r.status == "accruing" and r.accrued_amount == D("66.67")
    make_user(db, root, pkg)
    assert r.status == "achieved" and r.accrued_amount == D("100")
    assert [to_dec(c.held_amount) for c in comms(db, root)] == [D("33.33"), D("33.34"), D("33.33")]
    assert_ledger_consistent(db)


def test_auto_pool_entry_is_given_only_once_per_completed_level(db, root, pkg):
    set_setting(db, "level_plan_width", 1)
    set_plan(db, "level", [(100, 0, 40)])
    make_user(db, root, pkg)
    make_user(db, root, pkg)  # beyond capacity - must not create a second entry
    assert len(db.scalars(select(AutoPoolEntry).where(AutoPoolEntry.user_id == root.id)).all()) == 1


def test_plan_edited_while_level_is_filling_still_holds_exactly_the_new_reward(db, root, pkg):
    set_setting(db, "level_plan_width", 2)
    set_plan(db, "level", [(100, 50, 0)])
    make_user(db, root, pkg)  # holds 25 of 50
    db.get(PlanLevel, ("level", 1)).reward_amount = 80
    db.flush()
    make_user(db, root, pkg)  # must hold 55 so the reward ends at the new 80
    r = reward(db, root)
    assert (r.accrued_amount, r.target_amount, r.status) == (D("80"), D("80"), "achieved")
    assert [to_dec(c.held_amount) for c in comms(db, root)] == [D("25"), D("55")]
    assert net_sum(comms(db, root)) == D("120")
    assert_ledger_consistent(db)


def test_reward_lowered_mid_level_holds_nothing_more_and_never_claws_back(db, root, pkg):
    set_setting(db, "level_plan_width", 2)
    set_plan(db, "level", [(100, 80, 0)])
    make_user(db, root, pkg)  # holds 40
    db.get(PlanLevel, ("level", 1)).reward_amount = 20
    db.flush()
    make_user(db, root, pkg)
    assert [(to_dec(c.amount), to_dec(c.held_amount)) for c in comms(db, root)] == [(D("60"), D("40")), (D("100"), D("0"))]
    r = reward(db, root)
    assert (r.status, r.accrued_amount, r.target_amount) == ("achieved", D("40"), D("40"))
    assert_ledger_consistent(db)


def test_held_amount_never_exceeds_the_head_so_net_is_never_negative(db, root, pkg):
    set_setting(db, "level_plan_width", 2)
    set_plan(db, "level", [(100, 150, 150)])  # invalid plan (300 held > 200 total) forced into the database
    make_user(db, root, pkg)
    make_user(db, root, pkg)
    for c in comms(db, root):
        assert to_dec(c.amount) == 0 and to_dec(c.held_amount) == D("100")
    assert reward(db, root).accrued_amount == D("150")
    assert reward(db, root, kind="autopool").accrued_amount == D("50")
    assert_ledger_consistent(db)


# ---------------------------------------------------------------- the real plan
def test_real_plan_first_two_levels_pay_exactly_the_published_net(db, root, pkg):
    set_plan(db, "level", [(a, r, p) for _, a, r, p, _ in LEVEL_PLAN])
    full_tree(db, root, pkg, 10, 2)  # 10 directs, 100 at level 2
    assert net_sum(comms(db, root, level=1)) == D("900")  # 10 x 100 - 100 gift
    assert net_sum(comms(db, root, level=2)) == D("8500")  # 100 x 100 - 300 reward - 1200 auto pool
    assert reward(db, root, level=1).status == "achieved"
    assert reward(db, root, level=2).status == "achieved"
    assert reward(db, root, level=2, kind="autopool").status == "delivered"
    assert reward(db, root, level=3) is None
    assert root.group_pv == 110 * pkg.pv
    assert db.scalar(select(Commission.id).where(Commission.user_id == root.id).offset(110)) is None
    assert_ledger_consistent(db)
