from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, String, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify, today_cn

CN_TZ = timezone(timedelta(hours=8))


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
    expires_on: Mapped[date] = mapped_column(Date)
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
    certificate_id: Mapped[int | None] = mapped_column(ForeignKey("certificates.id"), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float
    certificate_id: int


class CertificateIn(BaseModel):
    instrument_no: str = Field(min_length=1, max_length=80)
    expires_on: date


class CertificatePatch(BaseModel):
    expires_on: date


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


def cert_state(c: Certificate, today: date) -> Literal["有效", "已作废", "已过期"]:
    if c.revoked:
        return "已作废"
    if c.expires_on < today:
        return "已过期"
    return "有效"


def cert_dict(c: Certificate, today: date | None = None) -> dict:
    today = today or today_cn()
    return {
        "id": c.id,
        "instrument_no": c.instrument_no,
        "expires_on": c.expires_on.isoformat(),
        "revoked": c.revoked,
        "state": cert_state(c, today),
        "created_by": c.created_by,
    }


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    # 兼容旧库：给已有 readings 表补绑定校准证的列（幂等）
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(readings)"))} if engine.dialect.name == "sqlite" else None
        if engine.dialect.name == "sqlite":
            if "certificate_id" not in cols:
                conn.execute(text("ALTER TABLE readings ADD COLUMN certificate_id INTEGER"))
        else:
            exists = conn.execute(
                text("SELECT 1 FROM information_schema.columns "
                     "WHERE table_name='readings' AND column_name='certificate_id'")
            ).scalar()
            if not exists:
                conn.execute(text("ALTER TABLE readings ADD COLUMN certificate_id INTEGER "
                                  "REFERENCES certificates(id)"))
    db = SessionLocal()
    try:
        if db.query(Reading).count() == 0:
            now = datetime.now(CN_TZ)
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
    exp = datetime.now(CN_TZ) + timedelta(hours=8)
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
        rows = db.query(Certificate).order_by(Certificate.id.desc()).all()
        today = today_cn()
        return [cert_dict(c, today) for c in rows]
    finally:
        db.close()


@app.post("/api/certificates", status_code=201)
def create_certificate(body: CertificateIn, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = Certificate(
            instrument_no=body.instrument_no.strip(),
            expires_on=body.expires_on,
            revoked=False,
            created_by=user["username"],
            created_at=datetime.now(CN_TZ),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return cert_dict(row)
    finally:
        db.close()


@app.post("/api/certificates/{cert_id}/revoke", status_code=200)
def revoke_certificate(cert_id: int, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = db.get(Certificate, cert_id)
        if row is None:
            raise HTTPException(status_code=404, detail="校准证不存在")
        row.revoked = True
        db.commit()
        db.refresh(row)
        return cert_dict(row)
    finally:
        db.close()


@app.patch("/api/certificates/{cert_id}", status_code=200)
def update_certificate(cert_id: int, body: CertificatePatch, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = db.get(Certificate, cert_id)
        if row is None:
            raise HTTPException(status_code=404, detail="校准证不存在")
        row.expires_on = body.expires_on
        db.commit()
        db.refresh(row)
        return cert_dict(row)
    finally:
        db.close()


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Reading).order_by(Reading.id.desc()).all()
        certs = {c.id: c for c in db.query(Certificate).all()}
        today = today_cn()
        result = []
        for r in rows:
            item = {
                "id": r.id,
                "site": r.site,
                "ch4_pct": r.ch4_pct,
                "level": r.level,
                "note": r.note,
                "created_by": r.created_by,
                "certificate_id": r.certificate_id,
                "instrument_no": certs[r.certificate_id].instrument_no
                if r.certificate_id and r.certificate_id in certs
                else None,
                "cert_state": cert_state(certs[r.certificate_id], today)
                if r.certificate_id and r.certificate_id in certs
                else None,
            }
            result.append(item)
        return result
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    level, note = classify(body.ch4_pct)
    db = SessionLocal()
    try:
        cert = db.get(Certificate, body.certificate_id)
        if cert is None:
            raise HTTPException(status_code=400, detail="上报被拒绝：缺少有效的仪器校准证（请选择校准证）")
        if cert.revoked:
            raise HTTPException(status_code=400, detail=f"上报被拒绝：校准证（仪器 {cert.instrument_no}）已作废，不能用来上报")
        today = today_cn()
        if cert.expires_on < today:
            raise HTTPException(
                status_code=400,
                detail=f"上报被拒绝：校准证（仪器 {cert.instrument_no}）已过期，截止日 {cert.expires_on.isoformat()}，请更换有效证件",
            )
        row = Reading(
            site=body.site.strip(),
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            certificate_id=cert.id,
            created_by=user["username"],
            created_at=datetime.now(CN_TZ),
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
            "certificate_id": cert.id,
            "instrument_no": cert.instrument_no,
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
