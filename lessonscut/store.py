"""Persistent jobs, immutable revisions and fenced worker claims."""
import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from sqlalchemy import create_engine, String, Text, Float, Integer, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session


def uid():
    return str(uuid.uuid4())


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Base(DeclarativeBase):
    pass


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    batch_id: Mapped[str] = mapped_column(String)
    asset_id: Mapped[str] = mapped_column(String)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String, default="QUEUED")
    stage: Mapped[str] = mapped_column(String, default="analyze")
    reason: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[str] = mapped_column(Text, default="{}")
    token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class Revision(Base):
    __tablename__ = "revisions"
    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan: Mapped[str] = mapped_column(Text)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class Call(Base):
    __tablename__ = "model_calls"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(String)
    revision: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    details: Mapped[str] = mapped_column(Text)


class RequestKey(Base):
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String)
    response: Mapped[str] = mapped_column(Text)


class Audit(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(String)
    revision: Mapped[int] = mapped_column(Integer)
    event: Mapped[str] = mapped_column(Text)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class Store:
    def __init__(self, url):
        self.engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {})
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self):
        with Session(self.engine, expire_on_commit=False) as s, s.begin():
            yield s

    def claim(self):
        with self.session() as s:
            # One GPU-owning worker process in v0.1; no service lifecycle changes.
            running = s.scalar(select(Job.id).where(Job.token.is_not(None)).limit(1))
            if running:
                return None
            job = s.scalar(select(Job).where(Job.state == "QUEUED").order_by(Job.created).with_for_update(skip_locked=True).limit(1))
            if not job:
                return None
            token = uid()
            changed = s.execute(update(Job).where(Job.id == job.id, Job.state == "QUEUED", Job.token.is_(None)).values(state="RUNNING", token=token, lease_until=time.time()+60)).rowcount
            if not changed:
                return None
            return job.id, job.revision, token

    def transition(self, job_id, revision, token, state, result=None, reason=""):
        with self.session() as s:
            values = {"state": state, "reason": reason, "lease_until": time.time()+60}
            if result is not None:
                values["result"] = encode(result)
            if state != "RUNNING":
                values.update(token=None, lease_until=0)
            n=s.execute(update(Job).where(Job.id==job_id, Job.revision==revision, Job.token==token).values(**values)).rowcount
            if n != 1:
                raise RuntimeError("stale worker claim; cannot commit result")

    def heartbeat(self, job_id, revision, token):
        with self.session() as s:
            return s.execute(update(Job).where(Job.id==job_id, Job.revision==revision, Job.token==token).values(lease_until=time.time()+60)).rowcount == 1

    def stale_claims(self):
        with self.session() as s:
            return [j.id for j in s.scalars(select(Job).where(Job.token.is_not(None), Job.lease_until < time.time()))]

    @staticmethod
    def fingerprint(value):
        return hashlib.sha256(encode(value).encode()).hexdigest()
