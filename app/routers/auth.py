from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import create_token, get_current_user, verify_password
from ..database import get_db
from ..models import Package, User
from ..schemas import LoginIn, RegisterIn
from ..services import DEFAULT_SETTINGS, get_setting, package_out, register_member, user_detail

router = APIRouter(prefix="/api", tags=["auth & public"])


@router.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(func.upper(User.username) == body.username.strip().upper()))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(400, "Invalid username or password")
    if body.portal == "admin" and user.role != "admin":
        raise HTTPException(403, "This login is for administrators only")
    if body.portal == "user" and user.role != "user":
        raise HTTPException(403, "Administrators please use the Admin login")
    if user.status in ("blocked", "rejected"):
        raise HTTPException(403, f"Your account is {user.status}. Please contact support")
    return {"token": create_token(user), "user": user_detail(db, user)}


@router.get("/auth/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return user_detail(db, user)


@router.post("/auth/signup")
def signup(body: RegisterIn, db: Session = Depends(get_db)):
    u = register_member(db, body.model_dump(), None, payment="pending")
    db.commit()
    return {"message": "Registration successful. You can login now and activate your package with a pin or "
                       "your e-wallet.", "username": u.username, "status": u.status}


@router.get("/public/packages")
def public_packages(db: Session = Depends(get_db)):
    rows = db.scalars(select(Package).where(Package.is_active.is_(True)).order_by(Package.price)).all()
    return [package_out(p) for p in rows]


@router.get("/public/settings")
def public_settings(db: Session = Depends(get_db)):
    return {k: get_setting(db, k) for k in DEFAULT_SETTINGS}
