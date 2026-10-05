"""Business logic shared by the admin and member APIs."""
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .auth import hash_password, verify_password
from .models import AppSetting, AutoPoolEntry, Commission, Package, PlanLevel, Reward, User, WalletTransaction, now_utc


def money(v) -> float:
    return float(v or 0)


def to_dec(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"))


# ---------------------------------------------------------------- settings
DEFAULT_SETTINGS = {
    "company_name": "Golden Apiece",
    "currency_symbol": "₹",
    "min_payout": "200",
    "level_plan_width": "10",
    "autopool_width": "5",
    "company_email": "support@goldenapiece.com",
    "company_phone": "+91 90000 00000",
    "company_address": "India",
}


def get_setting(db: Session, key: str) -> str:
    row = db.get(AppSetting, key)
    return row.value if row else DEFAULT_SETTINGS.get(key, "")


# ---------------------------------------------------------------- wallet
def wallet_totals(db: Session, user_id: int) -> tuple[float, float]:
    rows = db.execute(
        select(WalletTransaction.type, func.coalesce(func.sum(WalletTransaction.amount), 0))
        .where(WalletTransaction.user_id == user_id)
        .group_by(WalletTransaction.type)
    ).all()
    d = {t: money(v) for t, v in rows}
    return d.get("credit", 0.0), d.get("debit", 0.0)


def wallet_balance(db: Session, user_id: int) -> float:
    c, d = wallet_totals(db, user_id)
    return round(c - d, 2)


def add_txn(db: Session, user_id: int, type_: str, category: str, amount, description: str = "", ref: int | None = None):
    t = WalletTransaction(
        user_id=user_id, type=type_, category=category, amount=to_dec(amount),
        description=description, reference_user_id=ref,
    )
    db.add(t)
    return t


def check_txn_password(user: User, raw: str):
    if not raw or not verify_password(raw, user.txn_password_hash):
        raise HTTPException(400, "Invalid transaction password")


# ---------------------------------------------------------------- users
def next_username(db: Session) -> str:
    last = db.scalar(select(func.max(User.id))) or 0
    n = last + 1
    while True:
        name = f"GA{n:05d}"
        if not db.scalar(select(User.id).where(User.username == name)):
            return name
        n += 1


def user_brief(u: User | None) -> dict | None:
    if not u:
        return None
    return {
        "id": u.id,
        "username": u.username,
        "full_name": u.full_name,
        "email": u.email,
        "status": u.status,
        "package": u.package.name if u.package else None,
        "joined_at": u.joined_at.isoformat() if u.joined_at else None,
    }


def user_detail(db: Session, u: User) -> dict:
    d = user_brief(u)
    d.update({
        "first_name": u.first_name, "last_name": u.last_name, "phone": u.phone, "gender": u.gender,
        "date_of_birth": u.date_of_birth, "country": u.country, "state": u.state, "city": u.city,
        "address": u.address, "pincode": u.pincode, "bank_name": u.bank_name,
        "account_holder": u.account_holder, "account_number": u.account_number, "ifsc": u.ifsc,
        "role": u.role, "sponsor": u.sponsor.username if u.sponsor else None,
        "sponsor_name": u.sponsor.full_name if u.sponsor else None,
        "package_id": u.package_id, "personal_pv": u.personal_pv, "group_pv": u.group_pv,
        "is_franchise": u.is_franchise,
        "activated_at": u.activated_at.isoformat() if u.activated_at else None,
        "wallet_balance": wallet_balance(db, u.id),
        "direct_count": db.scalar(select(func.count(User.id)).where(User.sponsor_id == u.id)) or 0,
    })
    return d


def downline(db: Session, root_id: int, max_depth: int = 50) -> list[tuple[User, int]]:
    """Breadth-first list of (member, level) under root."""
    result, frontier, level = [], [root_id], 0
    while frontier and level < max_depth:
        level += 1
        kids = db.scalars(select(User).where(User.sponsor_id.in_(frontier)).order_by(User.id)).all()
        result += [(k, level) for k in kids]
        frontier = [k.id for k in kids]
    return result


def is_in_downline(db: Session, root_id: int, target: User) -> bool:
    cur, hops = target, 0
    while cur and hops < 200:
        if cur.id == root_id:
            return True
        cur = cur.sponsor
        hops += 1
    return False


def build_tree(db: Session, u: User, depth: int) -> dict:
    node = {
        "id": u.id, "username": u.username, "full_name": u.full_name, "status": u.status,
        "package": u.package.name if u.package else None, "personal_pv": u.personal_pv,
        "group_pv": u.group_pv, "joined_at": u.joined_at.isoformat() if u.joined_at else None,
        "sponsor": u.sponsor.username if u.sponsor else None, "children": [], "has_more": False,
    }
    kids = db.scalars(select(User).where(User.sponsor_id == u.id).order_by(User.id)).all()
    if depth > 0:
        node["children"] = [build_tree(db, k, depth - 1) for k in kids]
    else:
        node["has_more"] = len(kids) > 0
    node["child_count"] = len(kids)
    return node


# ---------------------------------------------------------------- registration & commissions
def setting_int(db: Session, key: str) -> int:
    try:
        return max(1, int(float(get_setting(db, key))))
    except (ValueError, OverflowError):  # garbage, "nan" or "inf" typed into the settings page
        return int(DEFAULT_SETTINGS[key])


def plan_levels(db: Session, plan: str) -> dict[int, PlanLevel]:
    return {lv.level: lv for lv in db.scalars(select(PlanLevel).where(PlanLevel.plan == plan).order_by(PlanLevel.level))}


ENGINE_LOCK_KEY = 724001  # Postgres advisory lock that serialises every commission-generating event
MAX_UPLINE_DEPTH = 1000  # safety stop for a corrupted (cyclic) sponsor chain


def _is_postgres(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def engine_lock(db: Session):
    """Serialise commission work for the rest of the current transaction.

    Head counts, auto pool positions and wallet balance checks are read-then-write, so two requests
    running at once (double-clicked approve, simultaneous registrations) could pay a head twice, exceed a
    level's capacity or overdraw a wallet. A transaction-scoped advisory lock makes them run one at a
    time; it is released automatically on commit / rollback. Cached rows are expired after locking so the
    caller re-reads whatever a previous lock holder committed.
    """
    db.connection()  # make sure the transaction has begun, so it can be told apart from the next one
    if db.info.get("engine_lock") is db.get_transaction():
        return  # already held in this transaction
    db.flush()
    if _is_postgres(db):
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ENGINE_LOCK_KEY})
    db.expire_all()
    db.info["engine_lock"] = db.get_transaction()


def _cum(total: Decimal, n: int, capacity: int) -> Decimal:
    """Part of `total` due after `n` of `capacity` heads, rounded to paise (cum(capacity) == total exactly)."""
    return (total * n / capacity).quantize(Decimal("0.01"))


def _share(total: Decimal, k: int, capacity: int) -> Decimal:
    """Part of `total` that belongs to the k-th head of a level holding `capacity` heads (no rounding drift)."""
    return _cum(total, k, capacity) - _cum(total, k - 1, capacity)


def _reward_row(db: Session, earner: User, plan: str, level: int, kind: str, entry: AutoPoolEntry | None,
                name: str, target: Decimal) -> Reward:
    entry_filter = Reward.entry_id == entry.id if entry else Reward.entry_id.is_(None)
    r = db.scalar(select(Reward).where(Reward.user_id == earner.id, Reward.plan == plan, Reward.level == level,
                                       Reward.kind == kind, entry_filter))
    if not r:
        r = Reward(user_id=earner.id, plan=plan, level=level, kind=kind, entry_id=entry.id if entry else None,
                   reward_name=name, target_amount=target, accrued_amount=Decimal("0"), status="accruing")
        db.add(r)
    return r


def _accrue(r: Reward | None, total: Decimal, k: int, capacity: int, limit: Decimal, complete: bool) -> Decimal:
    """Hold back what is still due on reward `r` after head k, never more than `limit` (what is left of the head).

    The amount is the gap between where the reward should stand after k heads and what it already holds,
    so the reward self-corrects when the plan is edited while a level is filling.
    """
    if r is None:
        return Decimal("0")
    accrued = to_dec(r.accrued_amount or 0)
    due = min(max(_cum(total, k, capacity) - accrued, Decimal("0")), limit)
    r.accrued_amount = accrued + due
    r.target_amount = max(total, r.accrued_amount)  # never below what is already held for the member
    if complete and r.status == "accruing":
        r.status = "achieved"
        r.achieved_at = now_utc()
    return due


def pay_head(db: Session, earner: User, from_user: User, plan: str, level: int, cfg: PlanLevel, capacity: int,
             entry: AutoPoolEntry | None = None):
    """Pay `earner` the per-head income for one new member at `level` of `plan` ('level' or 'autopool').

    Heads beyond the level capacity (width ** level) are not paid. The reward (and, for the level plan,
    the auto pool entry fund) is held back proportionally from every head, so when the level is complete
    the member has received exactly: total commission - reward - auto pool = net commission (plan sheet).
    Every head satisfies net + held == per-head amount, so no money is created or lost.
    """
    stmt = select(func.count(Commission.id)).where(Commission.user_id == earner.id, Commission.commission_type == plan,
                                                   Commission.level == level)
    stmt = stmt.where(Commission.entry_id == entry.id) if entry else stmt.where(Commission.entry_id.is_(None))
    k = (db.scalar(stmt) or 0) + 1
    if k > capacity:
        return
    per_head = max(to_dec(cfg.amount), Decimal("0"))
    reward_total = max(to_dec(cfg.reward_amount or 0), Decimal("0"))
    # the auto pool plan never funds further auto pool entries (that would recurse without end)
    pool_total = max(to_dec(cfg.autopool_amount or 0), Decimal("0")) if plan == "level" else Decimal("0")
    complete = k == capacity

    reward = (_reward_row(db, earner, plan, level, "reward", entry, cfg.reward_name or "Reward / Award", reward_total)
              if reward_total > 0 else None)
    pool = _reward_row(db, earner, plan, level, "autopool", None, "Auto pool entry", pool_total) if pool_total > 0 else None
    reward_share = _accrue(reward, reward_total, k, capacity, per_head, complete)
    pool_share = _accrue(pool, pool_total, k, capacity, per_head - reward_share, complete)
    net = per_head - reward_share - pool_share

    db.add(Commission(user_id=earner.id, from_user_id=from_user.id, commission_type=plan, level=level,
                      amount=net, held_amount=reward_share + pool_share, entry_id=entry.id if entry else None))
    if net > 0:
        label = f"Level {level} income" if plan == "level" else f"Auto pool level {level} income"
        add_txn(db, earner.id, "credit", "commission", net, f"{label} from {from_user.username}", from_user.id)
    db.flush()
    if pool is not None and complete and pool.status == "achieved":
        pool.status = "delivered"
        pool.delivered_at = now_utc()
        enter_autopool(db, earner, f"Level {level} completed")
    db.flush()


def enter_autopool(db: Session, user: User, source: str = "") -> AutoPoolEntry:
    """Place `user` in the next free position of the global auto pool and pay the entries above it."""
    engine_lock(db)
    width = setting_int(db, "autopool_width")
    # next position after the highest one, so a deleted entry can never cause a duplicate position
    n = db.scalar(select(func.coalesce(func.max(AutoPoolEntry.position), -1))) + 1
    parent = db.scalar(select(AutoPoolEntry).where(AutoPoolEntry.position == (n - 1) // width)) if n else None
    entry = AutoPoolEntry(user_id=user.id, position=n, parent_id=parent.id if parent else None, source=source)
    db.add(entry)
    db.flush()
    levels = plan_levels(db, "autopool")
    anc, level = parent, 1
    while anc is not None and level <= max(levels, default=0):
        cfg = levels.get(level)
        if cfg and anc.user.status == "active":
            pay_head(db, anc.user, user, "autopool", level, cfg, width ** level, anc)
        anc, level = anc.parent, level + 1
    return entry


def autopool_level_counts(position: int, total: int, width: int, depth: int) -> list[int]:
    """Filled positions at each depth below `position` (the positions under a node are contiguous per depth)."""
    out, lo, hi = [], position, position
    for _ in range(depth):
        lo, hi = lo * width + 1, hi * width + width
        out.append(max(0, min(hi, total - 1) - lo + 1))
    return out


def activate_member(db: Session, u: User):
    """Activate a member: set PV, add group PV to uplines and pay the per-head level income.

    Only a pending member is activated, and the uplines are paid for a member at most once, so calling
    this twice (double-clicked approve, two admins at once) never pays twice.
    """
    engine_lock(db)  # re-reads `u`, so a concurrent activation of the same member is seen here
    if u.status != "pending":
        return  # already active, or blocked / rejected in the meantime
    already_paid = db.scalar(select(Commission.id).where(Commission.from_user_id == u.id,
                                                         Commission.commission_type == "level").limit(1))
    u.status = "active"
    u.activated_at = u.activated_at or now_utc()
    if already_paid is not None:
        return  # was activated before (e.g. status reset by hand) - never pay the uplines twice
    pkg = u.package
    pv = pkg.pv if pkg else 0
    u.personal_pv = pv

    width = setting_int(db, "level_plan_width")
    levels = plan_levels(db, "level")
    upline, level, seen = u.sponsor, 1, {u.id}
    while upline is not None and upline.id not in seen and level <= MAX_UPLINE_DEPTH:
        seen.add(upline.id)
        upline.group_pv = (upline.group_pv or 0) + pv
        cfg = levels.get(level)
        if cfg and upline.status == "active" and upline.role == "user":
            pay_head(db, upline, u, "level", level, cfg, width ** level)
        upline = upline.sponsor
        level += 1


def pay_franchise_commission(db: Session, franchise: User, u: User, pkg: Package):
    amount = to_dec(pkg.franchise_commission or 0)
    if amount <= 0 or not franchise.is_franchise or franchise.role != "user":
        return
    db.add(Commission(user_id=franchise.id, from_user_id=u.id, commission_type="franchise", level=0, amount=amount))
    add_txn(db, franchise.id, "credit", "commission", amount, f"Franchise commission for {u.username} ({pkg.name})", u.id)


def package_breakup(p: Package) -> dict:
    """Join amount - commission - product - franchise commission - GST (on the join amount) = balance."""
    price = to_dec(p.price)
    gst = (price * to_dec(p.gst_percent or 0) / Decimal(100)).quantize(Decimal("0.01"))
    commission, product = to_dec(p.commission_amount or 0), to_dec(p.product_amount or 0)
    franchise = to_dec(p.franchise_commission or 0)
    return {"product_name": p.product_name, "commission_amount": money(commission), "product_amount": money(product),
            "franchise_commission": money(franchise), "gst_percent": money(p.gst_percent), "gst_amount": money(gst),
            "balance": money(price - commission - product - franchise - gst)}


def package_out(p: Package) -> dict:
    return {"id": p.id, "name": p.name, "code": p.code, "price": money(p.price), "pv": p.pv,
            "validity_days": p.validity_days, "description": p.description, "is_active": p.is_active,
            **package_breakup(p)}


def register_member(db: Session, data: dict, registrar: User | None, payment: str = "pending") -> User:
    """payment: 'admin' (activate), 'ewallet' (registrar pays, activate), 'pending' (wait for approval)."""
    if payment not in ("admin", "ewallet", "pending"):
        raise HTTPException(400, "Unknown payment type")
    if payment != "pending":
        engine_lock(db)  # wallet check + activation must not interleave with another registration
    sponsor =db.scalar(select(User).where(func.upper(User.username) == data["sponsor_username"].strip().upper()))
    if not sponsor:
        raise HTTPException(400, "Sponsor username not found")
    if sponsor.status != "active":
        raise HTTPException(400, "Sponsor account is not active")
    pkg = db.get(Package, int(data["package_id"]))
    if not pkg or not pkg.is_active:
        raise HTTPException(400, "Please choose a valid package")
    username = (data.get("username") or "").strip().upper() or next_username(db)
    if db.scalar(select(User.id).where(func.upper(User.username) == username)):
        raise HTTPException(400, "Username already taken")
    if len(data.get("password") or "") < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    if payment == "ewallet":
        if not registrar:
            raise HTTPException(400, "E-wallet payment needs a logged-in member")
        check_txn_password(registrar, data.get("txn_password_confirm", ""))
        if to_dec(wallet_balance(db, registrar.id)) < to_dec(pkg.price):
            raise HTTPException(400, "Insufficient e-wallet balance for this package")

    u = User(
        username=username,
        password_hash=hash_password(data["password"]),
        txn_password_hash=hash_password(data.get("txn_password") or data["password"]),
        role="user", status="pending",
        first_name=data["first_name"].strip(), last_name=(data.get("last_name") or "").strip(),
        email=data.get("email") or "", phone=data.get("phone") or "", gender=data.get("gender") or "",
        date_of_birth=data.get("date_of_birth") or "", country=data.get("country") or "India",
        state=data.get("state") or "", city=data.get("city") or "", address=data.get("address") or "",
        pincode=data.get("pincode") or "", sponsor_id=sponsor.id, package_id=pkg.id,
    )
    db.add(u)
    db.flush()
    db.refresh(u)
    if payment == "ewallet":
        add_txn(db, registrar.id, "debit", "registration", pkg.price, f"Registration of {u.username} ({pkg.name})", u.id)
    if payment in ("admin", "ewallet"):
        activate_member(db, u)
    if payment == "ewallet":
        pay_franchise_commission(db, registrar, u, pkg)
    return u


# ---------------------------------------------------------------- charts
def last_12_months() -> list[tuple[int, int, str]]:
    today = datetime.now(timezone.utc)
    y, m = today.year, today.month
    out = []
    for _ in range(12):
        out.append((y, m, datetime(y, m, 1).strftime("%b %y")))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


def month_series(rows: list[tuple[datetime, float]]) -> list[dict]:
    buckets = {(y, m): 0.0 for y, m, _ in last_12_months()}
    for dt, val in rows:
        if dt is None:
            continue
        k = (dt.year, dt.month)
        if k in buckets:
            buckets[k] += float(val or 0)
    return [{"label": lbl, "value": round(buckets[(y, m)], 2)} for y, m, lbl in last_12_months()]
