"""Server-side $ARC token whitelist: anti-sybil layer.

Why server-side: localStorage lives in one browser, so anyone can join 100x
by opening 100 browsers/profiles. This table lives on the server:

- one row per wallet AND per username (UNIQUE both)
- 1 join per IP per 24h (kills mass scripts from one machine)
- optional Cloudflare Turnstile check (kills bots outright)
- every row stores ip + user-agent for the pre-mint audit

Endpoints:
    POST /api/whitelist/join  {name, wallet, turnstile?} -> {ok, error}
    GET  /api/whitelist/count -> {ok, count}
    GET  /api/whitelist/list?limit=8 -> {ok, entries:[{name, wallet, login, at}]}
    GET  /api/whitelist/export?admin=TOKEN -> CSV download (admin only)

Config via env:
    TURNSTILE_SECRET ... secret key; empty = skip verification (dev mode)
    WL_ADMIN_TOKEN ..... token guarding /export; empty = export disabled
"""
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request

import guard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("LK_CHAT_DB") or os.path.join(ROOT, "var", "chat.db")

WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
IP_WINDOW = 24 * 3600  # one join per IP per day
TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET", "")
ADMIN_TOKEN = os.environ.get("WL_ADMIN_TOKEN", "")

SCHEMA = """
CREATE TABLE IF NOT EXISTS whitelist (
  wallet     TEXT PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
  login      TEXT NOT NULL DEFAULT 'privy',
  ip         TEXT NOT NULL DEFAULT '',
  user_agent TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wl_created ON whitelist(created_at);
CREATE INDEX IF NOT EXISTS idx_wl_ip ON whitelist(ip, created_at);
"""


def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


class Whitelist:
    def __init__(self):
        self._local = threading.local()

    def db(self):
        db = getattr(self._local, "db", None)
        if db is None:
            db = _db()
            self._local.db = db
        return db

    def count(self):
        row = self.db().execute("SELECT COUNT(*) c FROM whitelist").fetchone()
        return row["c"]

    def recent(self, limit=8):
        rows = self.db().execute(
            "SELECT name, wallet, login, created_at FROM whitelist ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 50)),)).fetchall()
        return [dict(r) for r in rows]

    def join(self, name, wallet, login, ip, ua):
        from players import validate
        err = validate(name)
        if err:
            return {"ok": False, "error": err}
        if not isinstance(wallet, str) or not WALLET_RE.match(wallet.strip()):
            return {"ok": False, "error": "Wallet address invalid."}
        wallet = wallet.strip()
        name = name.strip()
        # 1 join per IP per 24h (checked before insert so the error is clear)
        row = self.db().execute(
            "SELECT COUNT(*) c FROM whitelist WHERE ip = ? AND created_at > ?",
            (ip, time.time() - IP_WINDOW)).fetchone()
        if row["c"] > 0:
            return {"ok": False, "error": "This network already joined in the last 24h. One spot per network per day."}
        try:
            self.db().execute(
                "INSERT INTO whitelist (wallet, name, login, ip, user_agent, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (wallet, name, (login or "privy")[:16], ip[:64], (ua or "")[:160], time.time()))
        except sqlite3.IntegrityError as e:
            msg = str(e)
            if "whitelist.name" in msg:
                return {"ok": False, "error": "This username already joined."}
            return {"ok": False, "error": "This wallet already joined."}
        return {"ok": True}

    def export_csv(self):
        rows = self.db().execute(
            "SELECT name, wallet, login, ip, user_agent, created_at FROM whitelist ORDER BY created_at").fetchall()
        lines = ["username,wallet,login,ip,user_agent,date"]
        for r in rows:
            at = time.strftime("%Y-%m-%d", time.localtime(r["created_at"]))
            ua = '"' + (r["user_agent"] or "").replace('"', "'") + '"'
            lines.append(f"{r['name']},{r['wallet']},{r['login']},{r['ip']},{ua},{at}")
        return "\n".join(lines) + "\n"


wl = Whitelist()
_send = guard.send


def verify_turnstile(token, ip):
    """True if human (or if no secret configured = dev bypass)."""
    if not TURNSTILE_SECRET:
        return True, False  # ok, not_checked
    if not token:
        return False, True
    try:
        req = urllib.request.Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=f"secret={TURNSTILE_SECRET}&response={token}&remoteip={ip}".encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as res:
            return bool(json.loads(res.read()).get("success")), True
    except Exception:
        return False, True


def handle(handler, method, path, query, body):
    if not path.startswith("/api/whitelist/"):
        return False
    action = path[len("/api/whitelist/"):]
    ip = guard.client_ip(handler)
    ua = handler.headers.get("User-Agent", "")

    if action == "count" and method == "GET":
        _send(handler, {"ok": True, "count": wl.count()})
        return True

    if action == "list" and method == "GET":
        try:
            limit = max(1, min(int((query.get("limit") or ["8"])[0]), 50))
        except ValueError:
            limit = 8
        out = []
        for e in wl.recent(limit):
            w = e["wallet"]
            out.append({"name": e["name"], "wallet": f"{w[:6]}...{w[-4:]}",
                        "login": e["login"],
                        "at": time.strftime("%Y-%m-%d", time.localtime(e["created_at"]))})
        _send(handler, {"ok": True, "count": wl.count(), "entries": out})
        return True

    if action == "export" and method == "GET":
        token = (query.get("admin") or [""])[0]
        if not ADMIN_TOKEN or token != ADMIN_TOKEN:
            _send(handler, {"ok": False, "error": "Forbidden."}, 403)
            return True
        csv = wl.export_csv().encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/csv; charset=utf-8")
        handler.send_header("Content-Length", str(len(csv)))
        handler.send_header("Content-Disposition", "attachment; filename=arc-farm-whitelist.csv")
        handler.end_headers()
        handler.wfile.write(csv)
        return True

    if action == "join" and method == "POST":
        data, err = guard.parse_json(body)
        if err:
            _send(handler, {"ok": False, "error": err}, 400)
            return True
        blocked = guard.limiter.check("wl_join", ip)
        if blocked:
            _send(handler, {"ok": False, "error": blocked}, 429)
            return True
        ok, _checked = verify_turnstile(data.get("turnstile", ""), ip)
        if not ok:
            _send(handler, {"ok": False, "error": "Human check failed. Reload and try again."}, 403)
            return True
        r = wl.join(guard.text_field(data, "name", 32),
                    guard.text_field(data, "wallet", 64),
                    guard.text_field(data, "login", 16), ip, ua)
        _send(handler, r, 200 if r["ok"] else 409)
        return True

    _send(handler, {"ok": False, "error": "Unknown."}, 404)
    return True
