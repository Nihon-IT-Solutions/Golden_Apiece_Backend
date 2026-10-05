"""Endpoints used by both portals (network, profile, mailbox, news, registration)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..auth import get_current_user, hash_password, verify_password
from ..database import get_db
from ..models import Mail, News, Package, User
from ..schemas import ChangePasswordIn, MailIn, ProfileIn, RegisterIn
from ..services import build_tree, downline, is_in_downline, package_out, register_member, user_brief, user_detail

router = APIRouter(prefix="/api", tags=["common"])


def resolve_root(db: Session, me: User, username: str | None) -> User:
    if not username:
        return me
    target = db.scalar(select(User).where(func.upper(User.username) == username.strip().upper()))
    if not target:
        raise HTTPException(404, "Member not found")
    if me.role != "admin" and not is_in_downline(db, me.id, target):
        raise HTTPException(403, "This member is not in your downline")
    return target


# ---------------------------------------------------------------- profile
@router.get("/profile")
def get_profile(me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return user_detail(db, me)


@router.put("/profile")
def update_profile(body: ProfileIn, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(me, k, v)
    db.commit()
    return user_detail(db, me)


@router.post("/profile/password")
def change_password(body: ChangePasswordIn, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(body.current_password, me.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    me.password_hash = hash_password(body.new_password)
    db.commit()
    return {"message": "Password updated"}


@router.post("/profile/transaction-password")
def change_txn_password(body: ChangePasswordIn, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(body.current_password, me.txn_password_hash):
        raise HTTPException(400, "Current transaction password is incorrect")
    me.txn_password_hash = hash_password(body.new_password)
    db.commit()
    return {"message": "Transaction password updated"}


# ---------------------------------------------------------------- network
@router.get("/network/tree")
def tree(username: str | None = None, depth: int = 3, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    root = resolve_root(db, me, username)
    return build_tree(db, root, max(0, min(depth, 6)))


@router.get("/network/downline")
def downline_members(username: str | None = None, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    root = resolve_root(db, me, username)
    rows = downline(db, root.id)
    return [{**user_brief(u), "level": lvl, "sponsor": u.sponsor.username if u.sponsor else None,
             "personal_pv": u.personal_pv, "group_pv": u.group_pv} for u, lvl in rows]


@router.get("/network/referrals")
def referral_members(username: str | None = None, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    root = resolve_root(db, me, username)
    kids = db.scalars(select(User).where(User.sponsor_id == root.id).order_by(User.joined_at.desc())).all()
    return [{**user_brief(u), "personal_pv": u.personal_pv, "group_pv": u.group_pv, "phone": u.phone} for u in kids]


# ---------------------------------------------------------------- registration
@router.post("/register")
def register(body: RegisterIn, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = body.model_dump()
    if me.role == "admin":
        payment = "admin"
    else:
        payment = "ewallet" if body.payment_method == "ewallet" else "pending"
        sponsor = resolve_root(db, me, body.sponsor_username)  # sponsor must be me or my downline
        data["sponsor_username"] = sponsor.username
    u = register_member(db, data, me, payment)
    db.commit()
    msg = "Member registered and activated" if u.status == "active" else "Member registered. Waiting for admin approval"
    return {"message": msg, "username": u.username, "status": u.status}


@router.get("/packages")
def packages(me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Package).order_by(Package.price)).all()
    if me.role != "admin":
        rows = [p for p in rows if p.is_active]
    return [package_out(p) for p in rows]


@router.get("/users/lookup")
def lookup(username: str, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(func.upper(User.username) == username.strip().upper()))
    if not u:
        raise HTTPException(404, "Username not found")
    return {"username": u.username, "full_name": u.full_name, "status": u.status}


# ---------------------------------------------------------------- mailbox
def mail_out(m: Mail) -> dict:
    return {"id": m.id, "subject": m.subject, "body": m.body, "is_read": m.is_read,
            "created_at": m.created_at.isoformat(),
            "from": {"username": m.sender.username, "full_name": m.sender.full_name},
            "to": {"username": m.recipient.username, "full_name": m.recipient.full_name}}


@router.get("/mail")
def mail_list(box: str = "inbox", me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    col = Mail.recipient_id if box == "inbox" else Mail.sender_id
    rows = db.scalars(select(Mail).where(col == me.id).order_by(Mail.created_at.desc())).all()
    unread = db.scalar(select(func.count(Mail.id)).where(Mail.recipient_id == me.id, Mail.is_read.is_(False))) or 0
    return {"items": [mail_out(m) for m in rows], "unread": unread}


@router.post("/mail")
def mail_send(body: MailIn, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    to = body.to_username.strip().upper()
    if to == "ADMIN" and me.role != "admin":
        rcpt = db.scalar(select(User).where(User.role == "admin").order_by(User.id))
    else:
        rcpt = db.scalar(select(User).where(func.upper(User.username) == to))
    if not rcpt:
        raise HTTPException(404, "Recipient not found")
    db.add(Mail(sender_id=me.id, recipient_id=rcpt.id, subject=body.subject, body=body.body))
    db.commit()
    return {"message": "Mail sent"}


@router.post("/mail/{mail_id}/read")
def mail_read(mail_id: int, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    m = db.get(Mail, mail_id)
    if not m or m.recipient_id != me.id:
        raise HTTPException(404, "Mail not found")
    m.is_read = True
    db.commit()
    return {"ok": True}


@router.delete("/mail/{mail_id}")
def mail_delete(mail_id: int, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    m = db.get(Mail, mail_id)
    if not m or me.id not in (m.recipient_id, m.sender_id):
        raise HTTPException(404, "Mail not found")
    db.delete(m)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- news
@router.get("/news")
def news(me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(News).order_by(News.created_at.desc())).all()
    return [{"id": n.id, "title": n.title, "body": n.body, "created_at": n.created_at.isoformat()} for n in rows]


@router.get("/search")
def search(q: str, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    like = f"%{q.strip()}%"
    stmt = select(User).where(User.role == "user", or_(User.username.ilike(like), User.first_name.ilike(like),
                                                      User.last_name.ilike(like), User.email.ilike(like))).limit(10)
    rows = db.scalars(stmt).all()
    if me.role != "admin":
        rows = [u for u in rows if is_in_downline(db, me.id, u)]
    return [user_brief(u) for u in rows]
