"""Creates tables (if missing) and seeds the admin, packages, commission levels and optional demo data.

Run manually with:  uv run python -m app.seed
It also runs automatically when the API starts.
"""
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update

from .auth import hash_password
from .config import settings
from .database import Base, SessionLocal, engine
from .models import AppSetting, Commission, Mail, News, Package, Payout, PlanLevel, User, WalletTransaction
from .services import DEFAULT_SETTINGS, activate_member, add_txn, enter_autopool

# All amounts in INR.
# name, code, join amount, PV, product, product amount, commission, franchise commission, GST %
PACKAGES = [
    ("Anti Radiation Chip Pack", "GA549", 549, 549, "Anti Radiation Chip (1 chip)", 25, 322, 20, 5),
    ("Sanitary Pad Pack", "GA799", 799, 799, "Sanitary Pad (6 boxes)", 288, 322, 50, 0),
    ("The Silent Pack", "GA999", 999, 999, "The Silent 500 ml (2 bottles)", 200, 322, 100, 18),
]
LEGACY_PACKAGE_CODES = ("PACK1", "PACK2", "PACK3", "PACK4")

# Level income - 10 wide, 7 levels: level, per head amount, reward held at the level, auto pool entry fund, reward name
LEVEL_PLAN = [
    (1, 100, 100, 0, "Gift"),
    (2, 100, 300, 1200, "Reward"),
    (3, 50, 1250, 0, "Gift"),
    (4, 30, 3750, 0, "Gift"),
    (5, 30, 18750, 0, "Gift"),
    (6, 30, 93750, 0, "Gift"),
    (7, 30, 468750, 0, "Gift"),
]
# Auto pool - 5 wide, 10 levels: level, per head amount, reward & award amount held at the level
AUTOPOOL_PLAN = [
    (1, 200, 500), (2, 200, 3000), (3, 100, 4500), (4, 60, 7500), (5, 60, 87500),
    (6, 40, 225000), (7, 30, 743750), (8, 20, 1412500), (9, 20, 13462500), (10, 10, 17656250),
]

# Columns added after the first release; create_all() does not alter existing tables.
MIGRATIONS = [
    "ALTER TABLE packages ADD COLUMN IF NOT EXISTS product_name VARCHAR(150) NOT NULL DEFAULT ''",
    "ALTER TABLE packages ADD COLUMN IF NOT EXISTS product_amount NUMERIC(14, 2) NOT NULL DEFAULT 0",
    "ALTER TABLE packages ADD COLUMN IF NOT EXISTS commission_amount NUMERIC(14, 2) NOT NULL DEFAULT 0",
    "ALTER TABLE packages ADD COLUMN IF NOT EXISTS franchise_commission NUMERIC(14, 2) NOT NULL DEFAULT 0",
    "ALTER TABLE packages ADD COLUMN IF NOT EXISTS gst_percent NUMERIC(5, 2) NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_franchise BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE commissions ADD COLUMN IF NOT EXISTS held_amount NUMERIC(14, 2) NOT NULL DEFAULT 0",
    "ALTER TABLE commissions ADD COLUMN IF NOT EXISTS entry_id INTEGER REFERENCES autopool_entries (id)",
]

DEMO_MEMBERS = [
    # username, first, last, sponsor, package code, months ago
    ("GAUSER01", "John", "Smith", None, "GA999", 11),
    ("GA00002", "Emma", "Wilson", "GAUSER01", "GA799", 10),
    ("GA00003", "Liam", "Brown", "GAUSER01", "GA549", 9),
    ("GA00004", "Olivia", "Jones", "GA00002", "GA999", 8),
    ("GA00005", "Noah", "Garcia", "GA00002", "GA799", 7),
    ("GA00006", "Ava", "Miller", "GA00003", "GA549", 6),
    ("GA00007", "Ethan", "Davis", "GAUSER01", "GA999", 5),
    ("GA00008", "Sophia", "Martinez", "GA00004", "GA799", 4),
    ("GA00009", "Mason", "Lopez", "GA00007", "GA549", 3),
    ("GA00010", "Isabella", "Clark", "GA00007", "GA799", 2),
    ("GA00011", "Lucas", "Lewis", "GA00005", "GA999", 1),
    ("GA00012", "Mia", "Walker", "GA00010", "GA549", 0),
]


def migrate():
    with engine.begin() as conn:
        for stmt in MIGRATIONS:
            conn.execute(text(stmt))


def seed():
    Base.metadata.create_all(engine)
    migrate()
    db = SessionLocal()
    try:
        for k, v in DEFAULT_SETTINGS.items():
            if not db.get(AppSetting, k):
                db.add(AppSetting(key=k, value=v))
        # switch an existing dollar setup to INR
        cur = db.get(AppSetting, "currency_symbol")
        if cur and cur.value.strip() in ("$", "USD"):
            cur.value = DEFAULT_SETTINGS["currency_symbol"]
            mp = db.get(AppSetting, "min_payout")
            if mp and mp.value in ("10", "10.00"):
                mp.value = DEFAULT_SETTINGS["min_payout"]
        existing = set(db.scalars(select(Package.code)).all())
        if not any(p[1] in existing for p in PACKAGES):
            for p in db.scalars(select(Package).where(Package.code.in_(LEGACY_PACKAGE_CODES))).all():
                p.is_active = False
            for name, code, price, pv, product, product_amt, comm, franchise, gst in PACKAGES:
                db.add(Package(name=name, code=code, price=price, pv=pv, product_name=product, product_amount=product_amt,
                               commission_amount=comm, franchise_commission=franchise, gst_percent=gst,
                               description=f"Includes {product}"))
        if not db.scalar(select(PlanLevel.level).where(PlanLevel.plan == "level")):
            for lv, amount, reward, pool, name in LEVEL_PLAN:
                db.add(PlanLevel(plan="level", level=lv, amount=amount, reward_amount=reward, autopool_amount=pool,
                                 reward_name=name))
        if not db.scalar(select(PlanLevel.level).where(PlanLevel.plan == "autopool")):
            for lv, amount, reward in AUTOPOOL_PLAN:
                db.add(PlanLevel(plan="autopool", level=lv, amount=amount, reward_amount=reward,
                                 reward_name="Reward & Award"))
        db.commit()

        admin = db.scalar(select(User).where(User.role == "admin"))
        if not admin:
            admin = User(username=settings.ADMIN_USERNAME.upper(), password_hash=hash_password(settings.ADMIN_PASSWORD),
                         txn_password_hash=hash_password(settings.ADMIN_TXN_PASSWORD), role="admin", status="active",
                         first_name="Admin", last_name="", email="admin@goldenapiece.com",
                         activated_at=datetime.now(timezone.utc))
            db.add(admin)
            db.commit()
            print(f"Created admin {admin.username}")

        if settings.SEED_DEMO_DATA and not db.scalar(select(User.id).where(User.role == "user")):
            seed_demo(db, admin)
            print("Demo data created (member login: GAUSER01 / 12345678)")
    finally:
        db.close()


def seed_demo(db, admin):
    rnd = random.Random(7)
    pk = {p.code: p for p in db.scalars(select(Package)).all()}
    now = datetime.now(timezone.utc)
    by_name = {admin.username: admin}
    for uname, first, last, sponsor, code, months in DEMO_MEMBERS:
        when = now - timedelta(days=30 * months + rnd.randint(0, 20))
        u = User(username=uname, password_hash=hash_password("12345678"), txn_password_hash=hash_password("12345678"),
                 role="user", status="pending", first_name=first, last_name=last,
                 email=f"{first.lower()}.{last.lower()}@example.com", phone=f"+91 98{rnd.randint(10000000, 99999999)}",
                 country="India", city=rnd.choice(["Mumbai", "Pune", "Delhi", "Bengaluru", "Chennai"]),
                 sponsor_id=by_name[sponsor or admin.username].id, package_id=pk[code].id, joined_at=when,
                 bank_name="HDFC Bank", account_holder=f"{first} {last}", account_number=str(rnd.randint(10**11, 10**12)),
                 ifsc="HDFC0001234")
        db.add(u)
        db.flush()
        db.refresh(u)
        activate_member(db, u)
        db.flush()
        u.activated_at = when
        db.execute(update(Commission).where(Commission.from_user_id == u.id).values(created_at=when))
        db.execute(update(WalletTransaction).where(WalletTransaction.reference_user_id == u.id).values(created_at=when))
        by_name[uname] = u

    john = by_name["GAUSER01"]
    john.is_franchise = True
    add_txn(db, john.id, "credit", "fund_credit", 1000, "Welcome fund from admin", admin.id)
    for uname in ("GAUSER01", "GA00002", "GA00003", "GA00004", "GA00005", "GA00006", "GA00007"):
        enter_autopool(db, by_name[uname], "Demo entry")
    for uname, amount, status, days in [("GAUSER01", 500, "paid", 60), ("GAUSER01", 300, "approved", 20),
                                        ("GA00002", 250, "requested", 5), ("GA00007", 200, "requested", 2)]:
        u = by_name[uname]
        when = now - timedelta(days=days)
        db.add(Payout(user_id=u.id, amount=amount, status=status, requested_at=when,
                      processed_at=when + timedelta(days=1) if status != "requested" else None))
        t = add_txn(db, u.id, "debit", "payout", amount, "Payout request")
        t.created_at = when
    # one member waiting for approval
    db.add(User(username="GA00013", password_hash=hash_password("12345678"), txn_password_hash=hash_password("12345678"),
                role="user", status="pending", first_name="Harper", last_name="Young", email="harper.young@example.com",
                sponsor_id=john.id, package_id=pk["GA799"].id))
    db.add(News(title="Welcome to Golden Apiece", body="Our new back office is live. Explore your dashboard, network tree and e-wallet."))
    db.add(News(title="Monthly payout schedule", body="Payout requests are processed every Monday. Keep your bank details updated in your profile."))
    db.add(Mail(sender_id=admin.id, recipient_id=john.id, subject="Welcome aboard!",
                body="Hi John, welcome to Golden Apiece. Reach out anytime through the mail box."))
    db.commit()


if __name__ == "__main__":
    seed()
