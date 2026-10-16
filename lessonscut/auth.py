"""Same-origin browser sessions alongside backwards-compatible administrator API tokens."""
import hashlib
import secrets
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, SecretStr
from sqlalchemy import delete
from .store import LoginSession

COOKIE = "lessonscut_session"
SESSION_SECONDS = 8 * 3600


def session_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class LoginBody(BaseModel):
    password: SecretStr


def install_auth(app, settings, store):
    router = APIRouter(prefix="/api/v1/auth")
    attempts = defaultdict(deque)

    def same_origin(request):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "same-origin request required")

    def auth(request: Request, authorization: str | None = Header(default=None)):
        expected = settings.admin_token.get_secret_value()
        if not expected:
            raise HTTPException(503, "configure LESSONSCUT_ADMIN_TOKEN")
        if authorization:
            if secrets.compare_digest(authorization.encode(), ("Bearer " + expected).encode()):
                return
            raise HTTPException(401, "authentication required")
        token = request.cookies.get(COOKIE)
        with store.session() as s:
            row = s.get(LoginSession, session_digest(token)) if token else None
            if not row or row.expires <= time.time():
                raise HTTPException(401, "authentication required")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            same_origin(request)
            if request.headers.get("x-lessonscut-request") != "1":
                raise HTTPException(403, "request header required")

    @router.post("/login")
    def login(body: LoginBody, request: Request, response: Response):
        same_origin(request)
        client = request.client.host if request.client else "unknown"
        now = time.time()
        queue = attempts[client]
        while queue and queue[0] < now - 60:
            queue.popleft()
        if len(queue) >= 10:
            raise HTTPException(429, "too many login attempts; retry in one minute")
        queue.append(now)
        expected = settings.admin_token.get_secret_value()
        if not expected:
            raise HTTPException(503, "configure LESSONSCUT_ADMIN_TOKEN")
        if not secrets.compare_digest(body.password.get_secret_value().encode(), expected.encode()):
            raise HTTPException(401, "invalid access password")
        token = secrets.token_urlsafe(32)
        with store.session() as s:
            s.execute(delete(LoginSession).where(LoginSession.expires < now))
            previous = request.cookies.get(COOKIE)
            if previous:
                s.execute(delete(LoginSession).where(LoginSession.digest == session_digest(previous)))
            s.add(LoginSession(digest=session_digest(token), expires=now + SESSION_SECONDS))
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=SESSION_SECONDS, path="/")
        response.headers["Cache-Control"] = "no-store"
        return {"authenticated": True}

    @router.get("/session", dependencies=[Depends(auth)])
    def session(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"authenticated": True}

    @router.post("/logout", dependencies=[Depends(auth)])
    def logout(request: Request, response: Response):
        token = request.cookies.get(COOKIE)
        if token:
            with store.session() as s:
                s.execute(delete(LoginSession).where(LoginSession.digest == session_digest(token)))
        response.delete_cookie(COOKIE, path="/")
        return {"authenticated": False}

    app.include_router(router)
    return auth
