from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def now_utc():
    return datetime.now(timezone.utc)


Money = Numeric(14, 2)


class Package(Base):
    __tablename__ = "packages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    code: Mapped[str] = mapped_column(String(30), unique=True)
    price: Mapped[Decimal] = mapped_column(Money, default=0)
    pv: Mapped[int] = mapped_column(Integer, default=0)
    validity_days: Mapped[int] = mapped_column(Integer, default=365)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # price break-up (all amounts in INR): price - commission - product - franchise - GST = company balance
    product_name: Mapped[str] = mapped_column(String(150), default="")
    product_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    commission_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    franchise_commission: Mapped[Decimal] = mapped_column(Money, default=0)
    gst_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    txn_password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(10), default="user")  # admin | user
    status: Mapped[str] = mapped_column(String(15), default="active")  # pending | active | blocked | rejected
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80), default="")
    email: Mapped[str] = mapped_column(String(150), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    gender: Mapped[str] = mapped_column(String(10), default="")
    date_of_birth: Mapped[str] = mapped_column(String(20), default="")
    country: Mapped[str] = mapped_column(String(60), default="India")
    state: Mapped[str] = mapped_column(String(60), default="")
    city: Mapped[str] = mapped_column(String(60), default="")
    address: Mapped[str] = mapped_column(String(255), default="")
    pincode: Mapped[str] = mapped_column(String(15), default="")
    bank_name: Mapped[str] = mapped_column(String(100), default="")
    account_holder: Mapped[str] = mapped_column(String(100), default="")
    account_number: Mapped[str] = mapped_column(String(40), default="")
    ifsc: Mapped[str] = mapped_column(String(20), default="")
    sponsor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    package_id: Mapped[int | None] = mapped_column(ForeignKey("packages.id"), nullable=True)
    personal_pv: Mapped[int] = mapped_column(Integer, default=0)
    group_pv: Mapped[int] = mapped_column(Integer, default=0)
    is_franchise: Mapped[bool] = mapped_column(Boolean, default=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sponsor: Mapped["User | None"] = relationship(remote_side=[id], foreign_keys=[sponsor_id])
    package: Mapped["Package | None"] = relationship()

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(10))  # credit | debit
    category: Mapped[str] = mapped_column(String(30))  # commission, fund_credit, fund_debit, fund_transfer, payout, payout_refund, registration
    amount: Mapped[Decimal] = mapped_column(Money)
    description: Mapped[str] = mapped_column(String(255), default="")
    reference_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    reference_user: Mapped["User | None"] = relationship(foreign_keys=[reference_user_id])


class Commission(Base):
    __tablename__ = "commissions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    commission_type: Mapped[str] = mapped_column(String(20))  # level | autopool | franchise
    level: Mapped[int] = mapped_column(Integer, default=1)
    percentage: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=0)  # legacy, unused by the INR plan
    amount: Mapped[Decimal] = mapped_column(Money)  # net amount credited to the e-wallet
    held_amount: Mapped[Decimal] = mapped_column(Money, default=0)  # part kept for reward / auto pool entry
    entry_id: Mapped[int | None] = mapped_column(ForeignKey("autopool_entries.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    from_user: Mapped[User] = relationship(foreign_keys=[from_user_id])


class CommissionLevel(Base):
    """Legacy percentage plan - kept so old databases still load; replaced by PlanLevel."""
    __tablename__ = "commission_levels"
    level: Mapped[int] = mapped_column(Integer, primary_key=True)
    percentage: Mapped[Decimal] = mapped_column(Numeric(6, 2))


class PlanLevel(Base):
    """One row per level of a plan. plan = 'level' (sponsor level income) or 'autopool' (global auto pool).

    Every member counted at a level pays `amount` (per head). Of the level's total, `reward_amount`
    (+ `autopool_amount` for the level plan) is held back proportionally and given as reward / auto pool entry.
    """
    __tablename__ = "plan_levels"
    plan: Mapped[str] = mapped_column(String(15), primary_key=True)
    level: Mapped[int] = mapped_column(Integer, primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    reward_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    autopool_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    reward_name: Mapped[str] = mapped_column(String(150), default="")


class AutoPoolEntry(Base):
    """Global auto pool matrix. Entries fill left to right; the parent of position n is (n - 1) // width."""
    __tablename__ = "autopool_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, unique=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("autopool_entries.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    user: Mapped[User] = relationship()
    parent: Mapped["AutoPoolEntry | None"] = relationship(remote_side=[id])


class Reward(Base):
    """Amount held from a member's level / auto pool income until the level is complete."""
    __tablename__ = "rewards"
    __table_args__ = (UniqueConstraint("user_id", "plan", "level", "kind", "entry_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan: Mapped[str] = mapped_column(String(15))  # level | autopool
    level: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(15), default="reward")  # reward | autopool (auto pool entry fund)
    entry_id: Mapped[int | None] = mapped_column(ForeignKey("autopool_entries.id"), nullable=True)
    reward_name: Mapped[str] = mapped_column(String(150), default="")
    target_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    accrued_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    status: Mapped[str] = mapped_column(String(15), default="accruing")  # accruing | achieved | delivered
    achieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    user: Mapped[User] = relationship()


class Payout(Base):
    __tablename__ = "payouts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(15), default="requested")  # requested | approved | paid | rejected
    method: Mapped[str] = mapped_column(String(30), default="Bank Transfer")
    note: Mapped[str] = mapped_column(String(255), default="")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship()


class Mail(Base):
    __tablename__ = "mails"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    recipient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    sender: Mapped[User] = relationship(foreign_keys=[sender_id])
    recipient: Mapped[User] = relationship(foreign_keys=[recipient_id])


class News(Base):
    __tablename__ = "news"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), default="")
