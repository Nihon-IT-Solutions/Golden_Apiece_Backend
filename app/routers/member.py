"""Member (normal user) portal endpoints."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import require_user
from ..database import get_db
from ..models import AutoPoolEntry, Commission, Payout, Reward, User, WalletTransaction
from ..schemas import PayoutRequestIn, TransferIn
from ..services import (add_txn, autopool_level_counts, check_txn_password, downline, get_setting, money, month_series,
                        plan_levels, setting_int, user_brief, user_detail, wallet_balance, wallet_totals)

router = APIRouter(prefix="/api/member", tags=["member"])


def month_bounds():
    now = datetime.now(timezone.utc)
    this_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    prev_start = (this_start - timedelta(days=1)).replace(day=1)
    return this_start, prev_start


def pct_change(cur: float, prev: float) -> float:
    if prev == 0:
        return 100.0 if cur > 0 else 0.0
    return round((cur - prev) / prev * 100, 2)


def payout_summary(db: Session, user_id: int | None = None) -> dict:
    stmt = select(Payout.status, func.coalesce(func.sum(Payout.amount), 0)).group_by(Payout.status)
    if user_id:
        stmt = stmt.where(Payout.user_id == user_id)
    d = {s: money(v) for s, v in db.execute(stmt).all()}
    return {k: d.get(k, 0.0) for k in ("requested", "approved", "paid", "rejected")}


def txn_out(t: WalletTransaction) -> dict:
    return {"id": t.id, "type": t.type, "category": t.category, "amount": money(t.amount),
            "description": t.description, "created_at": t.created_at.isoformat(),
            "username": t.user.username, "full_name": t.user.full_name}


@router.get("/dashboard")
def dashboard(me: User = Depends(require_user), db: Session = Depends(get_db)):
    this_start, prev_start = month_bounds()

    def sum_txn(type_, start=None, end=None, category=None):
        stmt = select(func.coalesce(func.sum(WalletTransaction.amount), 0)).where(
            WalletTransaction.user_id == me.id, WalletTransaction.type == type_)
        if category:
            stmt = stmt.where(WalletTransaction.category == category)
        if start:
            stmt = stmt.where(WalletTransaction.created_at >= start)
        if end:
            stmt = stmt.where(WalletTransaction.created_at < end)
        return money(db.scalar(stmt))

    credit, debit = wallet_totals(db, me.id)
    comm_total = sum_txn("credit", category="commission")
    cards = {
        "ewallet": wallet_balance(db, me.id),
        "commission": comm_total,
        "commission_change": pct_change(sum_txn("credit", this_start, category="commission"),
                                        sum_txn("credit", prev_start, this_start, category="commission")),
        "total_credit": credit,
        "credit_change": pct_change(sum_txn("credit", this_start), sum_txn("credit", prev_start, this_start)),
        "total_debit": debit,
        "debit_change": pct_change(sum_txn("debit", this_start), sum_txn("debit", prev_start, this_start)),
    }

    team = downline(db, me.id)
    team_ids = [u.id for u, _ in team]
    joins = [(u.joined_at, 1) for u, _ in team]
    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=i)).date() for i in range(13, -1, -1)]
    by_day = {d: 0 for d in days}
    by_year = {y: 0 for y in range(now.year - 4, now.year + 1)}
    for u, _ in team:
        if u.joined_at.date() in by_day:
            by_day[u.joined_at.date()] += 1
        if u.joined_at.year in by_year:
            by_year[u.joined_at.year] += 1
    joinings = {
        "month": month_series(joins),
        "day": [{"label": d.strftime("%d %b"), "value": v} for d, v in by_day.items()],
        "year": [{"label": str(y), "value": v} for y, v in by_year.items()],
    }

    new_members = [user_brief(u) for u, _ in sorted(team, key=lambda x: x[0].joined_at, reverse=True)[:6]]

    top_earners, top_recruiters, package_overview = [], [], []
    if team_ids:
        rows = db.execute(select(Commission.user_id, func.sum(Commission.amount).label("s"))
                          .where(Commission.user_id.in_(team_ids)).group_by(Commission.user_id)
                          .order_by(func.sum(Commission.amount).desc()).limit(5)).all()
        top_earners = [{**user_brief(db.get(User, uid)), "amount": money(s)} for uid, s in rows]
        rows = db.execute(select(User.sponsor_id, func.count(User.id)).where(User.sponsor_id.in_(team_ids))
                          .group_by(User.sponsor_id).order_by(func.count(User.id).desc()).limit(5)).all()
        top_recruiters = [{**user_brief(db.get(User, uid)), "count": c} for uid, c in rows]
        counts: dict[str, int] = {}
        for u, _ in team:
            name = u.package.name if u.package else "No package"
            counts[name] = counts.get(name, 0) + 1
        package_overview = [{"package": k, "count": v} for k, v in counts.items()]

    def grouped(type_):
        rows = db.execute(select(WalletTransaction.category, func.sum(WalletTransaction.amount))
                          .where(WalletTransaction.user_id == me.id, WalletTransaction.type == type_)
                          .group_by(WalletTransaction.category)).all()
        return [{"category": c, "amount": money(a)} for c, a in rows]

    return {
        "profile": user_detail(db, me),
        "cards": cards,
        "joinings": joinings,
        "new_members": new_members,
        "top_earners": top_earners,
        "top_recruiters": top_recruiters,
        "package_overview": package_overview,
        "earnings": grouped("credit"),
        "expenses": grouped("debit"),
        "payout": payout_summary(db, me.id),
        "currency": get_setting(db, "currency_symbol"),
    }


@router.get("/ewallet")
def ewallet(me: User = Depends(require_user), db: Session = Depends(get_db)):
    credit, debit = wallet_totals(db, me.id)
    txns = db.scalars(select(WalletTransaction).where(WalletTransaction.user_id == me.id)
                      .order_by(WalletTransaction.created_at.desc())).all()
    comms = db.scalars(select(Commission).where(Commission.user_id == me.id).order_by(Commission.created_at.desc())).all()
    return {
        "balance": round(credit - debit, 2), "total_credit": credit, "total_debit": debit,
        "commission": money(sum(c.amount for c in comms)),
        "transactions": [txn_out(t) for t in txns],
        "commissions": [{"id": c.id, "type": c.commission_type, "level": c.level, "held": money(c.held_amount),
                         "amount": money(c.amount), "from": c.from_user.username, "from_name": c.from_user.full_name,
                         "created_at": c.created_at.isoformat()} for c in comms],
    }


def reward_out(r: Reward) -> dict:
    return {"id": r.id, "plan": r.plan, "level": r.level, "kind": r.kind, "entry_id": r.entry_id,
            "reward_name": r.reward_name, "target_amount": money(r.target_amount),
            "accrued_amount": money(r.accrued_amount), "status": r.status,
            "achieved_at": r.achieved_at.isoformat() if r.achieved_at else None,
            "delivered_at": r.delivered_at.isoformat() if r.delivered_at else None}


def autopool_entry_out(db: Session, e: AutoPoolEntry, total: int, width: int, depth: int) -> dict:
    filled = autopool_level_counts(e.position, total, width, depth)
    earned = money(db.scalar(select(func.coalesce(func.sum(Commission.amount), 0)).where(Commission.entry_id == e.id)))
    return {"id": e.id, "position": e.position + 1, "username": e.user.username, "full_name": e.user.full_name,
            "parent": e.parent.user.username if e.parent else None, "source": e.source,
            "created_at": e.created_at.isoformat(), "earned": earned,
            "levels": [{"level": i + 1, "filled": f, "capacity": width ** (i + 1)} for i, f in enumerate(filled)]}


@router.get("/income")
def income(me: User = Depends(require_user), db: Session = Depends(get_db)):
    """Level plan progress, auto pool entries and rewards of the logged-in member."""
    width = setting_int(db, "level_plan_width")
    paid = dict(db.execute(select(Commission.level, func.count(Commission.id))
                           .where(Commission.user_id == me.id, Commission.commission_type == "level")
                           .group_by(Commission.level)).all())
    earned = dict(db.execute(select(Commission.level, func.sum(Commission.amount))
                             .where(Commission.user_id == me.id, Commission.commission_type == "level")
                             .group_by(Commission.level)).all())
    level_plan = [{"level": lv.level, "per_head": money(lv.amount), "heads": paid.get(lv.level, 0),
                   "capacity": width ** lv.level, "earned": money(earned.get(lv.level)),
                   "reward_name": lv.reward_name, "reward_amount": money(lv.reward_amount),
                   "autopool_amount": money(lv.autopool_amount)} for lv in plan_levels(db, "level").values()]
    total = db.scalar(select(func.count(AutoPoolEntry.id))) or 0
    pool_width, depth = setting_int(db, "autopool_width"), len(plan_levels(db, "autopool"))
    entries = db.scalars(select(AutoPoolEntry).where(AutoPoolEntry.user_id == me.id).order_by(AutoPoolEntry.position)).all()
    rewards = db.scalars(select(Reward).where(Reward.user_id == me.id).order_by(Reward.plan, Reward.level)).all()
    by_type = dict(db.execute(select(Commission.commission_type, func.sum(Commission.amount))
                              .where(Commission.user_id == me.id).group_by(Commission.commission_type)).all())
    return {
        "totals": {k: money(by_type.get(k)) for k in ("level", "autopool", "franchise")},
        "is_franchise": me.is_franchise,
        "level_plan": level_plan,
        "autopool_entries": [autopool_entry_out(db, e, total, pool_width, depth) for e in entries],
        "autopool_total": total,
        "rewards": [reward_out(r) for r in rewards],
    }


@router.post("/ewallet/transfer")
def transfer(body: TransferIn, me: User = Depends(require_user), db: Session = Depends(get_db)):
    check_txn_password(me, body.txn_password)
    to = db.scalar(select(User).where(func.upper(User.username) == body.to_username.strip().upper(), User.role == "user"))
    if not to or to.id == me.id:
        raise HTTPException(400, "Enter a valid member username")
    if wallet_balance(db, me.id) < body.amount:
        raise HTTPException(400, "Insufficient e-wallet balance")
    note = body.note or "Fund transfer"
    add_txn(db, me.id, "debit", "fund_transfer", body.amount, f"{note} to {to.username}", to.id)
    add_txn(db, to.id, "credit", "fund_transfer", body.amount, f"{note} from {me.username}", me.id)
    db.commit()
    return {"message": f"Transferred {body.amount:.2f} to {to.username}"}


@router.get("/payouts")
def payouts(me: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Payout).where(Payout.user_id == me.id).order_by(Payout.requested_at.desc())).all()
    return {
        "balance": wallet_balance(db, me.id),
        "min_payout": float(get_setting(db, "min_payout") or 0),
        "summary": payout_summary(db, me.id),
        "items": [{"id": p.id, "amount": money(p.amount), "status": p.status, "method": p.method, "note": p.note,
                   "requested_at": p.requested_at.isoformat(),
                   "processed_at": p.processed_at.isoformat() if p.processed_at else None} for p in rows],
    }


@router.post("/payouts")
def request_payout(body: PayoutRequestIn, me: User = Depends(require_user), db: Session = Depends(get_db)):
    check_txn_password(me, body.txn_password)
    minimum = float(get_setting(db, "min_payout") or 0)
    if body.amount < minimum:
        raise HTTPException(400, f"Minimum payout amount is {minimum:.2f}")
    if wallet_balance(db, me.id) < body.amount:
        raise HTTPException(400, "Insufficient e-wallet balance")
    p = Payout(user_id=me.id, amount=body.amount, method=body.method)
    db.add(p)
    add_txn(db, me.id, "debit", "payout", body.amount, "Payout request")
    db.commit()
    return {"message": "Payout request submitted"}
