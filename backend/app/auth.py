"""Single-user OAuth 2.1 authorization server for the Claude connector.

- One pre-registered confidential client (OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET),
  no dynamic registration, exact redirect_uri matching, PKCE S256 required.
- The owner approves at /login with MCP_AUTH_SECRET (DB-backed rate limit).
- Codes and tokens are random, stored only as sha256 hashes, bound to client,
  redirect_uri, scopes, resource and issuer. Codes are single-use; refresh
  tokens rotate atomically and replaying a used one revokes its whole family.
- Every failure returns a generic error; no secret or token is ever logged.
"""

import base64
import datetime as dt
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from urllib.parse import unquote, urlencode, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from mcp.server.auth.provider import AccessToken
from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.engine import Connection
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.db import login_attempts, oauth_codes, oauth_tokens

log = logging.getLogger("app.auth")
router = APIRouter()

CODE_TTL = dt.timedelta(minutes=5)
TXN_TTL_SECONDS = 600
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = dt.timedelta(minutes=15)
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _same_resource(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


# ---------------------------------------------------------------------------
# Signed, expiring authorize transaction (carried through the login form)
# ---------------------------------------------------------------------------


def _txn_key(settings: Settings) -> bytes:
    return hmac.new(settings.mcp_auth_secret.encode(), b"oauth-txn-v1", hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign_txn(settings: Settings, payload: dict) -> str:
    body = _b64(json.dumps(payload | {"exp": int(time.time()) + TXN_TTL_SECONDS}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_txn_key(settings), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_txn(settings: Settings, token: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(_txn_key(settings), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(body))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def authorization_server_metadata(settings: Settings) -> dict:
    base = settings.issuer
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "revocation_endpoint": f"{base}/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "revocation_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "scopes_supported": list(settings.scopes),
        "authorization_response_iss_parameter_supported": True,
    }


def protected_resource_metadata(settings: Settings) -> dict:
    return {
        "resource": settings.resource_url,
        "authorization_servers": [settings.issuer],
        "scopes_supported": list(settings.scopes),
        "bearer_methods_supported": ["header"],
    }


@router.get("/.well-known/oauth-authorization-server")
async def as_metadata(request: Request):
    return JSONResponse(authorization_server_metadata(request.app.state.settings))


@router.get("/.well-known/oauth-protected-resource")
async def pr_metadata_root(request: Request):
    # The SDK serves /.well-known/oauth-protected-resource/mcp; this is the root fallback.
    return JSONResponse(protected_resource_metadata(request.app.state.settings))


# ---------------------------------------------------------------------------
# /authorize -> /login
# ---------------------------------------------------------------------------


def _plain_error(status: int) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><title>Request rejected</title><p>This authorization request is invalid.</p>",
        status_code=status,
        headers=NO_STORE,
    )


def _redirect_with(uri: str, params: dict) -> RedirectResponse:
    sep = "&" if urlparse(uri).query else "?"
    query = urlencode({k: v for k, v in params.items() if v is not None})
    return RedirectResponse(f"{uri}{sep}{query}", status_code=302, headers=NO_STORE)


def _login_page(settings: Settings, txn: str, redirect_uri: str, error: str | None = None, status: int = 200):
    host = html.escape(urlparse(redirect_uri).hostname or "")
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Approve health data access</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:26rem;margin:3rem auto;padding:0 1rem;color:#1a1a1a;background:#fafafa}}
h1{{font-size:1.3rem}} .host{{font-weight:600}} .err{{color:#b00020}}
input,button{{font:inherit;width:100%;padding:.6rem;margin-top:.5rem;box-sizing:border-box}}
button{{background:#1a1a1a;color:#fff;border:0;border-radius:6px;cursor:pointer}}
</style></head><body>
<h1>Approve read-only access to your health data</h1>
<p>After approval you will be sent to <span class="host">{host}</span>.
Only continue if you started this from your own Claude account.</p>
{err}
<form method="post" action="/login" autocomplete="off">
<input type="hidden" name="txn" value="{html.escape(txn)}">
<label>Owner secret<input type="password" name="password" required autofocus></label>
<button type="submit">Approve</button>
</form></body></html>"""
    return HTMLResponse(page, status_code=status, headers=NO_STORE)


@router.get("/authorize")
async def authorize(request: Request):
    settings: Settings = request.app.state.settings
    q = request.query_params
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    # Client and redirect URI errors must never redirect (RFC 6749 §4.1.2.1).
    if not client_id or not _eq(client_id, settings.oauth_client_id):
        return _plain_error(400)
    if redirect_uri not in settings.allowed_redirect_uris:
        return _plain_error(400)

    state = q.get("state")

    def fail(error: str) -> RedirectResponse:
        return _redirect_with(redirect_uri, {"error": error, "state": state, "iss": settings.issuer})

    if q.get("response_type") != "code":
        return fail("unsupported_response_type")
    challenge = q.get("code_challenge", "")
    if q.get("code_challenge_method") != "S256" or not 43 <= len(challenge) <= 128:
        return fail("invalid_request")
    resource = q.get("resource") or settings.resource_url
    if not _same_resource(resource, settings.resource_url):
        return fail("invalid_target")
    txn = sign_txn(
        settings,
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "state": state,
            "scopes": " ".join(settings.scopes),  # the only scope there is
            "resource": settings.resource_url,
        },
    )
    return _login_page(settings, txn, redirect_uri)


def client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy:
        # Cloud Run appends the real client address as the last X-Forwarded-For entry.
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


def _ip_hash(settings: Settings, ip: str) -> str:
    return hmac.new(_txn_key(settings), ip.encode(), hashlib.sha256).hexdigest()


def _login_blocked(conn: Connection, ip_hash: str) -> bool:
    conn.execute(delete(login_attempts).where(login_attempts.c.ts < func.now() - dt.timedelta(days=1)))
    count = conn.execute(
        select(func.count()).where(
            and_(login_attempts.c.ip_hash == ip_hash, login_attempts.c.ts > func.now() - LOGIN_WINDOW)
        )
    ).scalar_one()
    return count >= LOGIN_MAX_FAILURES


@router.post("/login")
async def login(request: Request):
    settings: Settings = request.app.state.settings
    engine = request.app.state.engine
    form = await request.form()
    txn_token = str(form.get("txn", ""))
    password = str(form.get("password", ""))
    txn = verify_txn(settings, txn_token)
    if txn is None:
        return _plain_error(400)
    ip_hash = _ip_hash(settings, client_ip(request, settings))

    def check() -> str:
        with engine.begin() as conn:
            if _login_blocked(conn, ip_hash):
                return "blocked"
            if not _eq(password, settings.mcp_auth_secret):
                conn.execute(insert(login_attempts).values(ip_hash=ip_hash))
                return "bad"
            code = secrets.token_urlsafe(32)
            conn.execute(
                insert(oauth_codes).values(
                    code_hash=sha256(code),
                    client_id=txn["client_id"],
                    redirect_uri=txn["redirect_uri"],
                    code_challenge=txn["code_challenge"],
                    scopes=txn["scopes"],
                    resource=txn["resource"],
                    family_id=secrets.token_hex(16),
                    expires_at=func.now() + CODE_TTL,
                )
            )
            return code

    result = await run_in_threadpool(check)
    if result == "blocked":
        log.warning("login rate-limited")
        return _login_page(settings, txn_token, txn["redirect_uri"], "Too many attempts. Try again later.", 429)
    if result == "bad":
        log.warning("login failed")
        return _login_page(settings, txn_token, txn["redirect_uri"], "Invalid credentials.", 401)
    log.info("login ok, code issued")
    return _redirect_with(txn["redirect_uri"], {"code": result, "state": txn.get("state"), "iss": settings.issuer})


# ---------------------------------------------------------------------------
# /token and /revoke
# ---------------------------------------------------------------------------


def _token_error(error: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status, headers=NO_STORE)


def authenticate_client(request: Request, form, settings: Settings) -> bool:
    """Accept client_secret_basic or client_secret_post for the single client."""
    form_id = str(form.get("client_id") or "")
    header = request.headers.get("authorization", "")
    if header[:6].lower() == "basic ":
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode()
            cid, csecret = decoded.split(":", 1)
            cid, csecret = unquote(cid), unquote(csecret)
        except (ValueError, UnicodeDecodeError):
            return False
        if form_id and form_id != cid:
            return False
    else:
        cid, csecret = form_id, str(form.get("client_secret") or "")
    if not cid or not csecret:
        return False
    return _eq(cid, settings.oauth_client_id) & _eq(csecret, settings.oauth_client_secret)


def _pkce_ok(verifier: str, challenge: str) -> bool:
    if not 43 <= len(verifier) <= 128:
        return False
    digest = _b64(hashlib.sha256(verifier.encode()).digest())
    return hmac.compare_digest(digest, challenge)


def _issue_tokens(conn: Connection, settings: Settings, row, family_id: str, parent_hash: str | None) -> dict:
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    common = {
        "client_id": row["client_id"],
        "scopes": row["scopes"],
        "resource": row["resource"],
        "issuer": settings.issuer,
        "family_id": family_id,
    }
    for kind, value, ttl in (
        ("access", access, settings.access_token_ttl),
        ("refresh", refresh, settings.refresh_token_ttl),
    ):
        conn.execute(
            insert(oauth_tokens).values(
                **common,
                token_hash=sha256(value),
                kind=kind,
                parent_hash=parent_hash,
                expires_at=func.now() + dt.timedelta(seconds=ttl),
            )
        )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": settings.access_token_ttl,
        "refresh_token": refresh,
        "scope": row["scopes"],
    }


def _revoke_family(conn: Connection, family_id: str) -> None:
    conn.execute(
        update(oauth_tokens)
        .where(oauth_tokens.c.family_id == family_id, oauth_tokens.c.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )


def exchange_code(conn: Connection, settings: Settings, form) -> dict | str:
    code = str(form.get("code") or "")
    if not code:
        return "invalid_request"
    t = oauth_codes
    row = conn.execute(
        update(t)
        .where(t.c.code_hash == sha256(code), t.c.used_at.is_(None), t.c.expires_at > func.now())
        .values(used_at=func.now())
        .returning(t)
    ).mappings().first()
    if row is None:
        # Reuse of an already-redeemed code: revoke whatever it produced (RFC 6749 §4.1.2).
        used = conn.execute(select(t.c.family_id).where(t.c.code_hash == sha256(code))).first()
        if used is not None:
            _revoke_family(conn, used[0])
        return "invalid_grant"
    if not _eq(row["client_id"], settings.oauth_client_id):
        return "invalid_grant"
    if str(form.get("redirect_uri") or "") != row["redirect_uri"]:
        return "invalid_grant"
    if not _pkce_ok(str(form.get("code_verifier") or ""), row["code_challenge"]):
        return "invalid_grant"
    resource = form.get("resource")
    if resource and not _same_resource(str(resource), row["resource"]):
        return "invalid_target"
    return _issue_tokens(conn, settings, row, row["family_id"], None)


def exchange_refresh(conn: Connection, settings: Settings, form) -> dict | str:
    token = str(form.get("refresh_token") or "")
    if not token:
        return "invalid_request"
    t = oauth_tokens
    h = sha256(token)
    row = conn.execute(
        update(t)
        .where(
            t.c.token_hash == h,
            t.c.kind == "refresh",
            t.c.used_at.is_(None),
            t.c.revoked_at.is_(None),
            t.c.expires_at > func.now(),
        )
        .values(used_at=func.now())
        .returning(t)
    ).mappings().first()
    if row is None:
        prior = conn.execute(
            select(t.c.family_id, t.c.used_at, (func.now() - t.c.used_at).label("age")).where(
                t.c.token_hash == h, t.c.kind == "refresh"
            )
        ).mappings().first()
        if prior is not None and prior["used_at"] is not None:
            # A refresh token used twice. Within a short grace window this is a
            # concurrent-refresh race (the client still holds the winner's tokens);
            # outside it, treat it as replay of a stolen token.
            if prior["age"] > dt.timedelta(seconds=settings.refresh_reuse_grace_seconds):
                log.warning("refresh token replay detected; family revoked")
                _revoke_family(conn, prior["family_id"])
        return "invalid_grant"
    if not _eq(row["client_id"], settings.oauth_client_id) or row["issuer"] != settings.issuer:
        return "invalid_grant"
    resource = form.get("resource")
    if resource and not _same_resource(str(resource), row["resource"]):
        return "invalid_target"
    requested = str(form.get("scope") or "").split()
    if any(s not in row["scopes"].split() for s in requested):
        return "invalid_scope"
    return _issue_tokens(conn, settings, row, row["family_id"], h)


@router.post("/token")
async def token(request: Request):
    settings: Settings = request.app.state.settings
    engine = request.app.state.engine
    form = await request.form()
    if not authenticate_client(request, form, settings):
        log.warning("token: client authentication failed")
        return _token_error("invalid_client", 401)
    grant = form.get("grant_type")
    if grant not in ("authorization_code", "refresh_token"):
        return _token_error("unsupported_grant_type")

    def run() -> dict | str:
        with engine.begin() as conn:
            if grant == "authorization_code":
                return exchange_code(conn, settings, form)
            return exchange_refresh(conn, settings, form)

    result = await run_in_threadpool(run)
    if isinstance(result, str):
        log.info("token: %s rejected (%s)", grant, result)
        return _token_error(result)
    log.info("token: %s ok", grant)
    return JSONResponse(result, headers=NO_STORE)


@router.post("/revoke")
async def revoke(request: Request):
    settings: Settings = request.app.state.settings
    engine = request.app.state.engine
    form = await request.form()
    if not authenticate_client(request, form, settings):
        return _token_error("invalid_client", 401)
    value = str(form.get("token") or "")

    def run() -> None:
        with engine.begin() as conn:
            row = conn.execute(select(oauth_tokens.c.family_id).where(oauth_tokens.c.token_hash == sha256(value))).first()
            if row is not None:
                _revoke_family(conn, row[0])

    if value:
        await run_in_threadpool(run)
    return JSONResponse({}, headers=NO_STORE)  # RFC 7009: 200 even for unknown tokens


# ---------------------------------------------------------------------------
# Resource-server side: verifies access tokens for the MCP SDK middleware
# ---------------------------------------------------------------------------


class DbTokenVerifier:
    def __init__(self, engine, settings: Settings):
        self.engine = engine
        self.settings = settings

    def _load(self, token: str) -> AccessToken | None:
        t = oauth_tokens
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.c.client_id, t.c.scopes, t.c.resource, t.c.issuer, t.c.expires_at).where(
                    t.c.token_hash == sha256(token),
                    t.c.kind == "access",
                    t.c.revoked_at.is_(None),
                    t.c.expires_at > func.now(),
                )
            ).mappings().first()
        if row is None or row["issuer"] != self.settings.issuer:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=int(row["expires_at"].timestamp()),
            resource=row["resource"],
            subject="owner",
            claims={"iss": row["issuer"]},
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or len(token) > 512:
            return None
        return await run_in_threadpool(self._load, token)
