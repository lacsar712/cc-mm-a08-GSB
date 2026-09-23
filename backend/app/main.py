from datetime import date, datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Certificate(Base):
    __tablename__ = "certificates"
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_no: Mapped[str] = mapped_column(String(80))
    valid_until: Mapped[date] = mapped_column(Date)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    certificate_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("certificates.id"), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float
    certificate_id: int | None = None


class CertificateIn(BaseModel):
    instrument_no: str = Field(min_length=1, max_length=80)
    valid_until: date


class CertificatePatch(BaseModel):
    instrument_no: str | None = Field(default=None, min_length=1, max_length=80)
    valid_until: date | None = None


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": username, "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可操作")
    return user


def cert_status(row: Certificate, today: date) -> str:
    if row.revoked:
        return "已作废"
    if row.valid_until < today:
        return "已过期"
    return "有效"


def cert_to_dict(row: Certificate, today: date) -> dict:
    return {
        "id": row.id,
        "instrument_no": row.instrument_no,
        "valid_until": row.valid_until.isoformat(),
        "revoked": row.revoked,
        "status": cert_status(row, today),
        "created_by": row.created_by,
    }


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    # 旧库的 readings 表可能没有 certificate_id 列，启动时幂等补上
    with engine.begin() as conn:
        try:
            conn.exec_driver_sql("ALTER TABLE readings ADD COLUMN certificate_id INTEGER")
        except Exception:
            pass
    db = SessionLocal()
    try:
        if db.query(Reading).count() == 0:
            now = datetime.now(timezone.utc)
            for site, ch4 in (("东翼-12", 0.35), ("回风巷", 1.4)):
                level, note = classify(ch4)
                db.add(
                    Reading(
                        site=site,
                        ch4_pct=ch4,
                        level=level,
                        note=note,
                        created_by="gasman",
                        created_at=now,
                    )
                )
            db.commit()
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "mine-methane-shift"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": body.username.strip(), "role": user["role"], "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/certificates")
def list_certificates(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        today = datetime.now(timezone.utc).date()
        rows = db.query(Certificate).order_by(Certificate.id.desc()).all()
        return [cert_to_dict(r, today) for r in rows]
    finally:
        db.close()


@app.post("/api/certificates", status_code=201)
def create_certificate(body: CertificateIn, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = Certificate(
            instrument_no=body.instrument_no.strip(),
            valid_until=body.valid_until,
            revoked=False,
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return cert_to_dict(row, datetime.now(timezone.utc).date())
    finally:
        db.close()


@app.patch("/api/certificates/{cert_id}")
def patch_certificate(cert_id: int, body: CertificatePatch, _user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = db.get(Certificate, cert_id)
        if row is None:
            raise HTTPException(status_code=404, detail="校准证不存在")
        if row.revoked:
            raise HTTPException(status_code=400, detail="校准证已作废，不能修改")
        if body.instrument_no is not None:
            row.instrument_no = body.instrument_no.strip()
        if body.valid_until is not None:
            row.valid_until = body.valid_until
        db.commit()
        db.refresh(row)
        return cert_to_dict(row, datetime.now(timezone.utc).date())
    finally:
        db.close()


@app.post("/api/certificates/{cert_id}/revoke")
def revoke_certificate(cert_id: int, _user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = db.get(Certificate, cert_id)
        if row is None:
            raise HTTPException(status_code=404, detail="校准证不存在")
        if not row.revoked:
            row.revoked = True
            db.commit()
            db.refresh(row)
        return cert_to_dict(row, datetime.now(timezone.utc).date())
    finally:
        db.close()


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Reading).order_by(Reading.id.desc()).all()
        return [
            {
                "id": r.id,
                "site": r.site,
                "ch4_pct": r.ch4_pct,
                "level": r.level,
                "note": r.note,
                "certificate_id": r.certificate_id,
                "created_by": r.created_by,
            }
            for r in rows
        ]
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    today = datetime.now(timezone.utc).date()
    if body.certificate_id is None:
        raise HTTPException(status_code=400, detail="缺少仪器校准证：上报前必须绑定有效的校准证")
    db = SessionLocal()
    try:
        cert = db.get(Certificate, body.certificate_id)
        if cert is None:
            raise HTTPException(status_code=400, detail="校准证不存在：请选择有效的仪器校准证")
        if cert.revoked:
            raise HTTPException(
                status_code=400,
                detail=f"校准证已作废：仪器 {cert.instrument_no} 的校准证不能用于上报",
            )
        if cert.valid_until < today:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"校准证已过期：仪器 {cert.instrument_no} 的校准证有效期至 {cert.valid_until.isoformat()}，"
                    f"今天 {today.isoformat()} 已超过截止日"
                ),
            )
        level, note = classify(body.ch4_pct)
        row = Reading(
            site=body.site.strip(),
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            certificate_id=cert.id,
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        payload = {
            "id": row.id,
            "site": row.site,
            "ch4_pct": row.ch4_pct,
            "level": row.level,
            "note": row.note,
            "certificate_id": row.certificate_id,
        }
    finally:
        db.close()
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)
    return payload


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
