import base64
import dataclasses
import hashlib
import re
import secrets
import threading
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi.testclient import TestClient
from sqlalchemy import select, text, update

from app import auth
from app.config import CLAUDE_CALLBACK
from app.db import oauth_tokens
from app.main import create_app
from tests.conftest import BASE, CLIENT_ID, CLIENT_SECRET, OWNER

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_params(challenge, **overrides):
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": CLAUDE_CALLBACK,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "st-123",
        "scope": "health.read",
        "resource": f"{BASE}/mcp",
    }
    params.update(overrides)
    return {k: v for k, v in params.items() if v is not None}


def get_code(client, challenge, password=OWNER, **overrides):
    r = client.get("/authorize?" + urlencode(authorize_params(challenge, **overrides)), follow_redirects=False)
    assert r.status_code == 200, r.text
    assert "claude.ai" in r.text  # redirect host is shown
    txn = re.search(r'name="txn" value="([^"]+)"', r.text).group(1)
    r = client.post("/login", data={"txn": txn, "password": password}, follow_redirects=False)
    return r


def code_from(response):
    assert response.status_code == 302, response.text
    loc = urlparse(response.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == CLAUDE_CALLBACK
    q = parse_qs(loc.query)
    assert q["state"] == ["st-123"] and q["iss"] == [BASE]
    return q["code"][0]


def token_request(client, data, basic=False):
    headers = {}
    if basic:
        cred = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        headers["Authorization"] = f"Basic {cred}"
    else:
        data = data | {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    return client.post("/token", data=data, headers=headers)


def full_flow(client, basic=False):
    verifier, challenge = pkce()
    code = code_from(get_code(client, challenge))
    r = token_request(
        client,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": CLAUDE_CALLBACK, "code_verifier": verifier, "resource": f"{BASE}/mcp"},
        basic=basic,
    )
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return r.json()


def mcp_status(client, access):
    return client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | {"Authorization": f"Bearer {access}"}, follow_redirects=False).status_code


# --- discovery ----------------------------------------------------------------


def test_discovery_metadata(client):
    pr = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert pr["resource"] == f"{BASE}/mcp" and pr["authorization_servers"] == [BASE]
    assert client.get("/.well-known/oauth-protected-resource").json()["resource"] == f"{BASE}/mcp"
    md = client.get("/.well-known/oauth-authorization-server").json()
    assert md["issuer"] == BASE
    assert md["code_challenge_methods_supported"] == ["S256"]
    assert md["authorization_response_iss_parameter_supported"] is True
    assert "registration_endpoint" not in md
    assert client.post("/register", json={"redirect_uris": [CLAUDE_CALLBACK]}).status_code in (404, 405)


def test_mcp_requires_token_without_redirect(client):
    for path in ("/mcp",):
        r = client.post(path, json=LIST_TOOLS, headers=MCP_HEADERS, follow_redirects=False)
        assert r.status_code == 401
        assert 'resource_metadata="http://localhost/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]
    assert mcp_status(client, "not-a-real-token") == 401


# --- authorization code flow ---------------------------------------------------


def test_full_flow_client_secret_post_and_basic(client):
    for basic in (False, True):
        tokens = full_flow(client, basic=basic)
        assert tokens["token_type"] == "Bearer" and tokens["scope"] == "health.read"
        assert mcp_status(client, tokens["access_token"]) == 200


def test_code_is_single_use_and_reuse_revokes_issued_tokens(client):
    verifier, challenge = pkce()
    code = code_from(get_code(client, challenge))
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": CLAUDE_CALLBACK, "code_verifier": verifier}
    first = token_request(client, data)
    assert first.status_code == 200
    second = token_request(client, data)
    assert second.status_code == 400 and second.json() == {"error": "invalid_grant"}
    assert mcp_status(client, first.json()["access_token"]) == 401


def test_authorize_rejects_unknown_client_and_redirect_without_redirecting(client):
    _, challenge = pkce()
    for overrides in ({"client_id": "someone-else"}, {"redirect_uri": "https://evil.example/cb"}, {"redirect_uri": CLAUDE_CALLBACK + "/x"}):
        r = client.get("/authorize?" + urlencode(authorize_params(challenge, **overrides)), follow_redirects=False)
        assert r.status_code == 400
        assert "location" not in r.headers


def test_authorize_errors_redirect_with_state_and_iss(client):
    _, challenge = pkce()
    for overrides, error in (
        ({"resource": "https://other.example/mcp"}, "invalid_target"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge_method": None}, "invalid_request"),
        ({"response_type": "token"}, "unsupported_response_type"),
    ):
        r = client.get("/authorize?" + urlencode(authorize_params(challenge, **overrides)), follow_redirects=False)
        assert r.status_code == 302
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["error"] == [error] and q["state"] == ["st-123"] and q["iss"] == [BASE]


def test_token_rejections(client):
    verifier, challenge = pkce()
    code = code_from(get_code(client, challenge))
    good = {"grant_type": "authorization_code", "code": code, "redirect_uri": CLAUDE_CALLBACK, "code_verifier": verifier}
    bad_secret = client.post("/token", data=good | {"client_id": CLIENT_ID, "client_secret": "x" * 40})
    assert bad_secret.status_code == 401 and bad_secret.json() == {"error": "invalid_client"}
    wrong_client = client.post("/token", data=good | {"client_id": "other", "client_secret": CLIENT_SECRET})
    assert wrong_client.status_code == 401
    assert token_request(client, good | {"grant_type": "password"}).json() == {"error": "unsupported_grant_type"}
    # Each failed redemption burns the (single-use) code, so use fresh codes.
    for mutate in (
        lambda d: d | {"redirect_uri": CLAUDE_CALLBACK + "?x=1"},
        lambda d: d | {"code_verifier": secrets.token_urlsafe(48)},
        lambda d: d | {"resource": "https://other.example/mcp"},
    ):
        verifier, challenge = pkce()
        code = code_from(get_code(client, challenge))
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": CLAUDE_CALLBACK, "code_verifier": verifier}
        r = token_request(client, mutate(data))
        assert r.status_code == 400 and r.json()["error"] in ("invalid_grant", "invalid_target")


def test_bad_or_tampered_txn_rejected(client):
    r = client.post("/login", data={"txn": "abc.def", "password": OWNER}, follow_redirects=False)
    assert r.status_code == 400


# --- refresh rotation -------------------------------------------------------------


def refresh(client, rt):
    return token_request(client, {"grant_type": "refresh_token", "refresh_token": rt})


def test_refresh_rotation(client):
    t1 = full_flow(client)
    r = refresh(client, t1["refresh_token"])
    assert r.status_code == 200
    t2 = r.json()
    assert t2["refresh_token"] != t1["refresh_token"]
    assert mcp_status(client, t2["access_token"]) == 200
    assert refresh(client, t2["refresh_token"]).status_code == 200


def test_refresh_replay_within_grace_is_rejected_without_revoking(client):
    t1 = full_flow(client)
    t2 = refresh(client, t1["refresh_token"]).json()
    again = refresh(client, t1["refresh_token"])
    assert again.status_code == 400 and again.json() == {"error": "invalid_grant"}
    assert mcp_status(client, t2["access_token"]) == 200  # race tolerance: family intact


def test_refresh_replay_after_grace_revokes_family(client, engine):
    t1 = full_flow(client)
    t2 = refresh(client, t1["refresh_token"]).json()
    with engine.begin() as conn:
        conn.execute(text("UPDATE oauth_tokens SET used_at = now() - interval '1 hour' WHERE used_at IS NOT NULL"))
    assert refresh(client, t1["refresh_token"]).json() == {"error": "invalid_grant"}
    assert mcp_status(client, t2["access_token"]) == 401
    assert refresh(client, t2["refresh_token"]).status_code == 400


def test_concurrent_refresh_exactly_one_succeeds(client, engine, settings):
    t1 = full_flow(client)
    barrier = threading.Barrier(4)
    results = []

    def worker():
        barrier.wait()
        with engine.begin() as conn:
            results.append(auth.exchange_refresh(conn, settings, {"refresh_token": t1["refresh_token"]}))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [r for r in results if isinstance(r, dict)]
    assert len(winners) == 1
    assert all(r == "invalid_grant" for r in results if not isinstance(r, dict))
    assert mcp_status(client, winners[0]["access_token"]) == 200


# --- token checks ---------------------------------------------------------------------


def test_expired_revoked_and_wrong_resource_tokens_rejected(client, engine):
    tokens = full_flow(client)
    access = tokens["access_token"]
    h = hashlib.sha256(access.encode()).hexdigest()
    with engine.begin() as conn:
        conn.execute(update(oauth_tokens).where(oauth_tokens.c.token_hash == h).values(resource="https://other.example/mcp"))
    assert mcp_status(client, access) == 401
    with engine.begin() as conn:
        conn.execute(update(oauth_tokens).where(oauth_tokens.c.token_hash == h).values(resource=f"{BASE}/mcp", expires_at=text("now() - interval '1 second'")))
    assert mcp_status(client, access) == 401
    fresh = full_flow(client)
    assert client.post("/revoke", data={"token": fresh["refresh_token"], "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}).status_code == 200
    assert mcp_status(client, fresh["access_token"]) == 401


def test_tokens_stored_only_as_hashes(client, engine):
    tokens = full_flow(client)
    with engine.connect() as conn:
        stored = " ".join(r[0] for r in conn.execute(select(oauth_tokens.c.token_hash)))
    assert tokens["access_token"] not in stored and tokens["refresh_token"] not in stored


# --- login rate limit ------------------------------------------------------------------


def test_login_rate_limit(client):
    _, challenge = pkce()
    for _ in range(5):
        r = get_code(client, challenge, password="wrong-" + "x" * 30)
        assert r.status_code == 401 and "Invalid credentials" in r.text
    blocked = get_code(client, challenge)  # even the right password is refused now
    assert blocked.status_code == 429


def test_rate_limit_uses_last_forwarded_ip_behind_proxy(settings, engine, clock):
    proxied = create_app(dataclasses.replace(settings, trust_proxy=True), engine=engine, clock=clock)
    with TestClient(proxied, base_url=BASE) as client:
        _, challenge = pkce()
        attacker = {"X-Forwarded-For": "1.2.3.4"}
        r = client.get("/authorize?" + urlencode(authorize_params(challenge)))
        txn = re.search(r'name="txn" value="([^"]+)"', r.text).group(1)
        for _ in range(5):
            client.post("/login", data={"txn": txn, "password": "nope-" + "x" * 30}, headers=attacker)
        # Spoofed leading entries don't help the attacker...
        spoof = client.post("/login", data={"txn": txn, "password": OWNER}, headers={"X-Forwarded-For": "9.9.9.9, 1.2.3.4"}, follow_redirects=False)
        assert spoof.status_code == 429
        # ...and the owner on a different address is not locked out.
        owner = client.post("/login", data={"txn": txn, "password": OWNER}, headers={"X-Forwarded-For": "5.6.7.8"}, follow_redirects=False)
        assert owner.status_code == 302
