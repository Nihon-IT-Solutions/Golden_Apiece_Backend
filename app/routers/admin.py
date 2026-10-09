"""Admin (back office) endpoints."""
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..auth import hash_password, require_admin
from ..database import get_db
from ..models import (AppSetting, AutoPoolEntry, Commission, News, Package, Payout, Pin, PlanLevel, Reward, User,
                      WalletTransaction, now_utc)
from ..schemas import ActionIn, AdminPasswordResetIn, FundIn, NewsIn, PackageIn, PlanLevelIn, ProfileIn, SettingsIn
from ..services import (DEFAULT_SETTINGS, activate_member, add_txn, check_txn_password, enter_autopool, get_setting,
                        level_plan_budget_warning, money, month_series, package_breakup, package_out, pin_out,
                        plan_levels, setting_int, user_brief, user_detail, wallet_balance)
from .member import autopool_entry_out, payout_summary, reward_out, txn_out

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def balances_map(db: Session) -> dict[int, float]:
    signed = case((WalletTransaction.type == "credit", WalletTransaction.amount), else_=-WalletTransaction.amount)
    rows = db.execute(select(WalletTransaction.user_id, func.sum(signed)).group_by(WalletTransaction.user_id)).all()
    return {uid: money(v) for uid, v in rows}


def activated_members(db: Session, start=None, end=None):
    stmt = select(User).where(User.role == "user", User.activated_at.is_not(None))
    if start:
        stmt = stmt.where(User.activated_at >= start)
    if end:
        stmt = stmt.where(User.activated_at < end)
    return db.scalars(stmt.order_by(User.activated_at.desc())).all()


def date_range(date_from: str | None, date_to: str | None):
    start = datetime.combine(date.fromisoformat(date_from), datetime.min.time(), tzinfo=timezone.utc) if date_from else None
    end = (datetime.combine(date.fromisoformat(date_to), datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)) if date_to else None
    return start, end


# ---------------------------------------------------------------- dashboard
@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    all_active = activated_members(db)
    user_ids = {u.id for u in db.scalars(select(User).where(User.role == "user")).all()}
    bal = balances_map(db)
    commissions = db.scalars(select(Commission).order_by(Commission.created_at.desc())).all()
    ps = payout_summary(db)

    income_rows = [(u.activated_at, u.package.price if u.package else 0) for u in all_active]
    comm_rows = [(c.created_at, c.amount) for c in commissions]
    income = month_series(income_rows)
    comm = month_series(comm_rows)

    return {
        "cards": {
            "month_income": money(sum(u.package.price for u in all_active if u.package and u.activated_at >= month_start)),
            "week_income": money(sum(u.package.price for u in all_active if u.package and u.activated_at >= now - timedelta(days=7))),
            "ewallet": round(sum(v for k, v in bal.items() if k in user_ids), 2),
            "bonus": money(sum(c.amount for c in commissions)),
            "paid": ps["paid"],
            "pending": ps["requested"],
            "members": len(user_ids),
            "active": db.scalar(select(func.count(User.id)).where(User.role == "user", User.status == "active")) or 0,
            "pending_approvals": db.scalar(select(func.count(User.id)).where(User.status == "pending")) or 0,
        },
        "joinings": month_series([(u.joined_at, 1) for u in db.scalars(select(User).where(User.role == "user")).all()]),
        "income_vs_commission": [{"label": i["label"], "income": i["value"], "commission": c["value"]} for i, c in zip(income, comm)],
        "recent_income": [{"username": u.username, "full_name": u.full_name, "package": u.package.name if u.package else "",
                           "amount": money(u.package.price if u.package else 0), "date": u.activated_at.isoformat()} for u in all_active[:6]],
        "recent_commission": [{"username": c.user.username, "full_name": c.user.full_name, "type": c.commission_type,
                               "level": c.level, "amount": money(c.amount), "date": c.created_at.isoformat()} for c in commissions[:6]],
        "payout": ps,
        "new_members": [user_brief(u) for u in db.scalars(select(User).where(User.role == "user").order_by(User.joined_at.desc()).limit(6)).all()],
        "currency": get_setting(db, "currency_symbol"),
    }


# ---------------------------------------------------------------- members / profile management
@router.get("/members")
def members(q: str = "", status: str = "", db: Session = Depends(get_db)):
    stmt = select(User).where(User.role == "user")
    if status:
        stmt = stmt.where(User.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where((User.username.ilike(like)) | (User.first_name.ilike(like)) | (User.last_name.ilike(like)) | (User.email.ilike(like)))
    rows = db.scalars(stmt.order_by(User.joined_at.desc())).all()
    bal = balances_map(db)
    return [{**user_brief(u), "phone": u.phone, "sponsor": u.sponsor.username if u.sponsor else None,
             "personal_pv": u.personal_pv, "group_pv": u.group_pv, "wallet_balance": bal.get(u.id, 0.0)} for u in rows]


@router.get("/members/{user_id}")
def member_detail(user_id: int, db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Member not found")
    txns = db.scalars(select(WalletTransaction).where(WalletTransaction.user_id == u.id)
                      .order_by(WalletTransaction.created_at.desc()).limit(50)).all()
    return {**user_detail(db, u), "transactions": [txn_out(t) for t in txns]}


@router.put("/members/{user_id}")
def member_update(user_id: int, body: ProfileIn, db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Member not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(u, k, v)
    db.commit()
    return user_detail(db, u)


@router.post("/members/{user_id}/status")
def member_status(user_id: int, body: ActionIn, db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u or u.role != "user":
        raise HTTPException(404, "Member not found")
    if body.action == "block":
        u.status = "blocked"
    elif body.action == "unblock":
        u.status = "active" if u.activated_at else "pending"
    else:
        raise HTTPException(400, "Unknown action")
    db.commit()
    return {"message": f"Member {u.username} is now {u.status}"}


@router.post("/members/{user_id}/franchise")
def member_franchise(user_id: int, body: ActionIn, db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u or u.role != "user":
        raise HTTPException(404, "Member not found")
    if body.action not in ("enable", "disable"):
        raise HTTPException(400, "Unknown action")
    u.is_franchise = body.action == "enable"
    db.commit()
    return {"message": f"{u.username} is {'now a franchise' if u.is_franchise else 'no longer a franchise'}"}


@router.post("/members/{user_id}/autopool")
def member_autopool(user_id: int, db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u or u.role != "user" or u.status != "active":
        raise HTTPException(400, "Only active members can enter the auto pool")
    e = enter_autopool(db, u, "Added by admin")
    db.commit()
    return {"message": f"{u.username} placed in the auto pool at position {e.position + 1}"}


@router.post("/members/{user_id}/password")
def member_password(user_id: int, body: AdminPasswordResetIn, db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Member not found")
    if body.kind == "transaction":
        u.txn_password_hash = hash_password(body.new_password)
    else:
        u.password_hash = hash_password(body.new_password)
    db.commit()
    return {"message": "Password updated"}


# ---------------------------------------------------------------- pending activations
# Members join without approval; these list the ones whose package is not paid yet so the admin can
# activate them once payment is received outside the system.
@router.get("/approvals")
def approvals(db: Session = Depends(get_db)):
    rows = db.scalars(select(User).where(User.status == "pending").order_by(User.joined_at.desc())).all()
    return [{**user_brief(u), "sponsor": u.sponsor.username if u.sponsor else None, "phone": u.phone,
             "amount": money(u.package.price if u.package else 0)} for u in rows]


@router.post("/approvals/{user_id}")
def approve(user_id: int, body: ActionIn, db: Session = Depends(get_db)):
    if body.action not in ("activate", "approve"):  # 'approve' kept for older admin panels
        raise HTTPException(400, "Unknown action")
    u = db.get(User, user_id)
    if not u or u.status != "pending":
        raise HTTPException(404, "Pending member not found")
    activate_member(db, u)
    db.commit()
    return {"message": f"{u.username} {u.status}"}


# ---------------------------------------------------------------- e-wallet
@router.get("/ewallet")
def ewallet(db: Session = Depends(get_db)):
    users = db.scalars(select(User).where(User.role == "user").order_by(User.username)).all()
    bal = balances_map(db)
    ids = [u.id for u in users]
    totals = dict(db.execute(select(WalletTransaction.type, func.sum(WalletTransaction.amount))
                             .where(WalletTransaction.user_id.in_(ids) if ids else False)
                             .group_by(WalletTransaction.type)).all())
    txns = db.scalars(select(WalletTransaction).order_by(WalletTransaction.created_at.desc()).limit(500)).all()
    return {
        "total_credit": money(totals.get("credit")), "total_debit": money(totals.get("debit")),
        "balance": round(sum(bal.get(i, 0.0) for i in ids), 2),
        "commission": money(db.scalar(select(func.coalesce(func.sum(Commission.amount), 0)))),
        "balances": [{"id": u.id, "username": u.username, "full_name": u.full_name, "balance": bal.get(u.id, 0.0)} for u in users],
        "transactions": [txn_out(t) for t in txns],
    }


@router.post("/ewallet/fund")
def fund(body: FundIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    check_txn_password(admin, body.txn_password)
    u = db.scalar(select(User).where(func.upper(User.username) == body.username.strip().upper(), User.role == "user"))
    if not u:
        raise HTTPException(404, "Member not found")
    if body.type == "debit" and wallet_balance(db, u.id) < body.amount:
        raise HTTPException(400, "Member does not have enough balance")
    if body.type not in ("credit", "debit"):
        raise HTTPException(400, "Type must be credit or debit")
    add_txn(db, u.id, body.type, f"fund_{body.type}", body.amount, body.note or f"Admin fund {body.type}", admin.id)
    db.commit()
    return {"message": f"{body.amount:.2f} {body.type}ed to {u.username}"}


# ---------------------------------------------------------------- payouts
@router.get("/payouts")
def payouts(status: str = "", db: Session = Depends(get_db)):
    stmt = select(Payout).order_by(Payout.requested_at.desc())
    if status:
        stmt = stmt.where(Payout.status == status)
    rows = db.scalars(stmt).all()
    return {"summary": payout_summary(db), "items": [
        {"id": p.id, "username": p.user.username, "full_name": p.user.full_name, "amount": money(p.amount),
         "status": p.status, "method": p.method, "note": p.note, "bank_name": p.user.bank_name,
         "account_number": p.user.account_number, "ifsc": p.user.ifsc, "requested_at": p.requested_at.isoformat(),
         "processed_at": p.processed_at.isoformat() if p.processed_at else None} for p in rows]}


@router.post("/payouts/{payout_id}")
def payout_action(payout_id: int, body: ActionIn, db: Session = Depends(get_db)):
    p = db.get(Payout, payout_id)
    if not p:
        raise HTTPException(404, "Payout not found")
    allowed = {"approve": ("requested",), "pay": ("requested", "approved"), "reject": ("requested", "approved")}
    if body.action not in allowed or p.status not in allowed[body.action]:
        raise HTTPException(400, f"Cannot {body.action} a payout that is {p.status}")
    p.status = {"approve": "approved", "pay": "paid", "reject": "rejected"}[body.action]
    p.processed_at = now_utc()
    p.note = body.note or p.note
    if body.action == "reject":
        add_txn(db, p.user_id, "credit", "payout_refund", p.amount, "Payout request rejected - refund")
    db.commit()
    return {"message": f"Payout #{p.id} {p.status}"}


# ---------------------------------------------------------------- packages
@router.post("/packages")
def package_create(body: PackageIn, db: Session = Depends(get_db)):
    if db.scalar(select(Package.id).where(Package.code == body.code)):
        raise HTTPException(400, "Package code already exists")
    p = Package(**body.model_dump())
    db.add(p)
    db.commit()
    return package_out(p)


@router.put("/packages/{pid}")
def package_update(pid: int, body: PackageIn, db: Session = Depends(get_db)):
    p = db.get(Package, pid)
    if not p:
        raise HTTPException(404, "Package not found")
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    db.commit()
    return package_out(p)


@router.delete("/packages/{pid}")
def package_delete(pid: int, db: Session = Depends(get_db)):
    p = db.get(Package, pid)
    if not p:
        raise HTTPException(404, "Package not found")
    if db.scalar(select(func.count(User.id)).where(User.package_id == pid)):
        p.is_active = False
        db.commit()
        return {"message": "Package is in use, so it was disabled instead of deleted"}
    db.delete(p)
    db.commit()
    return {"message": "Package deleted"}


# ---------------------------------------------------------------- joining pins
@router.get("/pins")
def pins(status: str = "", q: str = "", db: Session = Depends(get_db)):
    stmt = select(Pin).order_by(Pin.id.desc())
    if status:
        stmt = stmt.where(Pin.status == status)
    if q:
        owners = select(User.id).where(User.username.ilike(f"%{q.strip()}%"))
        stmt = stmt.where(Pin.code.ilike(f"%{q.strip()}%") | Pin.owner_id.in_(owners))
    rows = db.scalars(stmt.limit(1000)).all()
    totals = dict(db.execute(select(Pin.status, func.count(Pin.id)).group_by(Pin.status)).all())
    sold = db.execute(select(func.coalesce(func.sum(Pin.price), 0), func.coalesce(func.sum(Pin.commission), 0))).one()
    return {"summary": {"unused": totals.get("unused", 0), "used": totals.get("used", 0), "amount": money(sold[0]),
                        "commission": money(sold[1]), "net": money(sold[0] - sold[1])},
            "items": [pin_out(p) for p in rows]}


# ---------------------------------------------------------------- business
@router.get("/business")
def business(db: Session = Depends(get_db)):
    active = activated_members(db)
    sales = sum(money(u.package.price) for u in active if u.package)
    comm = money(db.scalar(select(func.coalesce(func.sum(Commission.amount), 0))))
    ps = payout_summary(db)
    per_pkg: dict[str, dict] = {}
    for u in active:
        if not u.package:
            continue
        b = package_breakup(u.package)
        row = per_pkg.setdefault(u.package.name, {"package": u.package.name, "count": 0, "amount": 0.0, "product": 0.0,
                                                  "gst": 0.0})
        row["count"] += 1
        row["amount"] += money(u.package.price)
        row["product"] += b["product_amount"]
        row["gst"] += b["gst_amount"]
    product = sum(r["product"] for r in per_pkg.values())
    gst = sum(r["gst"] for r in per_pkg.values())
    sales_series = month_series([(u.activated_at, u.package.price if u.package else 0) for u in active])
    return {
        "total_sales": round(sales, 2), "total_commission": comm, "paid": ps["paid"],
        "pending": ps["requested"] + ps["approved"], "product_cost": round(product, 2), "gst": round(gst, 2),
        "profit": round(sales - comm - product - gst, 2),
        "by_package": list(per_pkg.values()), "sales_series": sales_series,
        "recent": [{"username": u.username, "full_name": u.full_name, "package": u.package.name if u.package else "",
                    "amount": money(u.package.price if u.package else 0), "date": u.activated_at.isoformat()} for u in active[:50]],
    }


# ---------------------------------------------------------------- reports
@router.get("/reports/{kind}")
def reports(kind: str, date_from: str | None = None, date_to: str | None = None, db: Session = Depends(get_db)):
    start, end = date_range(date_from, date_to)

    def within(col, stmt):
        if start:
            stmt = stmt.where(col >= start)
        if end:
            stmt = stmt.where(col < end)
        return stmt

    if kind == "joining":
        rows = db.scalars(within(User.joined_at, select(User).where(User.role == "user")).order_by(User.joined_at.desc())).all()
        return [{"username": u.username, "full_name": u.full_name, "sponsor": u.sponsor.username if u.sponsor else "",
                 "package": u.package.name if u.package else "", "status": u.status, "email": u.email,
                 "date": u.joined_at.isoformat()} for u in rows]
    if kind == "commission":
        rows = db.scalars(within(Commission.created_at, select(Commission)).order_by(Commission.created_at.desc())).all()
        return [{"username": c.user.username, "full_name": c.user.full_name, "from": c.from_user.username,
                 "type": c.commission_type, "level": c.level, "held": money(c.held_amount),
                 "amount": money(c.amount), "date": c.created_at.isoformat()} for c in rows]
    if kind == "payout":
        rows = db.scalars(within(Payout.requested_at, select(Payout)).order_by(Payout.requested_at.desc())).all()
        return [{"username": p.user.username, "full_name": p.user.full_name, "amount": money(p.amount),
                 "status": p.status, "method": p.method, "date": p.requested_at.isoformat()} for p in rows]
    if kind == "wallet":
        rows = db.scalars(within(WalletTransaction.created_at, select(WalletTransaction))
                          .order_by(WalletTransaction.created_at.desc())).all()
        return [{"username": t.user.username, "full_name": t.user.full_name, "type": t.type, "category": t.category,
                 "amount": money(t.amount), "description": t.description, "date": t.created_at.isoformat()} for t in rows]
    if kind == "top_earners":
        stmt = within(Commission.created_at, select(Commission.user_id, func.sum(Commission.amount), func.count(Commission.id))
                      .group_by(Commission.user_id).order_by(func.sum(Commission.amount).desc()))
        out = []
        for uid, s, c in db.execute(stmt).all():
            u = db.get(User, uid)
            out.append({"username": u.username, "full_name": u.full_name, "commissions": c, "amount": money(s)})
        return out
    if kind == "package":
        rows = db.scalars(within(User.activated_at, select(User).where(User.role == "user", User.activated_at.is_not(None)))).all()
        agg: dict[str, dict] = {}
        for u in rows:
            name = u.package.name if u.package else "-"
            a = agg.setdefault(name, {"package": name, "members": 0, "amount": 0.0, "pv": 0})
            a["members"] += 1
            a["amount"] += money(u.package.price if u.package else 0)
            a["pv"] += u.personal_pv
        return list(agg.values())
    raise HTTPException(404, "Unknown report")


# ---------------------------------------------------------------- tools: news
@router.post("/news")
def news_create(body: NewsIn, db: Session = Depends(get_db)):
    n = News(title=body.title, body=body.body)
    db.add(n)
    db.commit()
    return {"id": n.id}


@router.put("/news/{nid}")
def news_update(nid: int, body: NewsIn, db: Session = Depends(get_db)):
    n = db.get(News, nid)
    if not n:
        raise HTTPException(404, "News not found")
    n.title, n.body = body.title, body.body
    db.commit()
    return {"id": n.id}


@router.delete("/news/{nid}")
def news_delete(nid: int, db: Session = Depends(get_db)):
    n = db.get(News, nid)
    if n:
        db.delete(n)
        db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- settings & compensation plan
def plan_out(db: Session, plan: str) -> list[dict]:
    width = setting_int(db, "level_plan_width" if plan == "level" else "autopool_width")
    out = []
    for lv in plan_levels(db, plan).values():
        heads = width ** lv.level
        total = money(lv.amount) * heads
        held = money(lv.reward_amount) + money(lv.autopool_amount)
        out.append({"level": lv.level, "amount": money(lv.amount), "reward_amount": money(lv.reward_amount),
                    "autopool_amount": money(lv.autopool_amount), "reward_name": lv.reward_name, "heads": heads,
                    "total": round(total, 2), "net": round(total - held, 2)})
    return out


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    return {"values": {k: get_setting(db, k) for k in DEFAULT_SETTINGS},
            "level_plan": plan_out(db, "level"), "autopool_plan": plan_out(db, "autopool"),
            "level_plan_warning": level_plan_budget_warning(db)}


@router.put("/settings")
def put_settings(body: SettingsIn, db: Session = Depends(get_db)):
    for k, v in body.values.items():
        if k not in DEFAULT_SETTINGS:
            continue
        row = db.get(AppSetting, k)
        if row:
            row.value = v
        else:
            db.add(AppSetting(key=k, value=v))
    db.commit()
    return {"message": "Settings saved"}


@router.put("/settings/plan/{plan}")
def put_plan(plan: str, body: list[PlanLevelIn], db: Session = Depends(get_db)):
    if plan not in ("level", "autopool"):
        raise HTTPException(404, "Unknown plan")
    width = setting_int(db, "level_plan_width" if plan == "level" else "autopool_width")
    rows = sorted(body, key=lambda x: x.level)
    for i, lv in enumerate(rows, start=1):
        if lv.reward_amount + lv.autopool_amount > lv.amount * width ** i:
            raise HTTPException(400, f"Level {i}: reward + auto pool cannot exceed the level's total commission")
    for old in plan_levels(db, plan).values():
        db.delete(old)
    db.flush()
    for i, lv in enumerate(rows, start=1):
        db.add(PlanLevel(plan=plan, level=i, amount=lv.amount, reward_amount=lv.reward_amount,
                         autopool_amount=lv.autopool_amount if plan == "level" else 0, reward_name=lv.reward_name))
    db.commit()
    return {"message": "Compensation plan saved", "warning": level_plan_budget_warning(db)}


# ---------------------------------------------------------------- auto pool & rewards
@router.get("/autopool")
def autopool(db: Session = Depends(get_db)):
    entries = db.scalars(select(AutoPoolEntry).order_by(AutoPoolEntry.position)).all()
    total, width, depth = len(entries), setting_int(db, "autopool_width"), len(plan_levels(db, "autopool"))
    return {"width": width, "total": total,
            "entries": [autopool_entry_out(db, e, total, width, depth) for e in entries]}


@router.get("/rewards")
def rewards(status: str = "", db: Session = Depends(get_db)):
    stmt = select(Reward).where(Reward.kind == "reward").order_by(Reward.created_at.desc())
    if status:
        stmt = stmt.where(Reward.status == status)
    return [{**reward_out(r), "username": r.user.username, "full_name": r.user.full_name}
            for r in db.scalars(stmt).all()]


@router.post("/rewards/{reward_id}")
def reward_action(reward_id: int, body: ActionIn, db: Session = Depends(get_db)):
    r = db.get(Reward, reward_id)
    if not r or r.kind != "reward":
        raise HTTPException(404, "Reward not found")
    if body.action != "deliver" or r.status != "achieved":
        raise HTTPException(400, "Only achieved rewards can be marked as delivered")
    r.status = "delivered"
    r.delivered_at = now_utc()
    db.commit()
    return {"message": f"Reward delivered to {r.user.username}"}
