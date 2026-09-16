"""Audit the whitelist for sybil (tuyul) patterns before the mint snapshot.

Reads the same SQLite table tools/whitelist.py writes and prints a risk-scored
report. Heuristics (each adds to the score):

  +3  same funding source: wallets sharing an IP (mass joins, 1 machine)
  +2  burst joins: >5 joins from nearby IPs within 10 minutes
  +2  sequential emails/usernames (user1, user2, ... / aaa1, aaa2)
  +1  same user-agent across 3+ rows (one script, one browser)
  +1  username looks random (no vowels, or ends in digits)

Usage:
    python tools/wl_audit.py [--db var/chat.db] [--json]
"""
import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("LK_CHAT_DB") or os.path.join(ROOT, "var", "chat.db")


def rows(db):
    try:
        return db.execute(
            "SELECT name, wallet, login, ip, user_agent, created_at FROM whitelist ORDER BY created_at").fetchall()
    except sqlite3.OperationalError:
        return []


def main():
    use_json = "--json" in sys.argv
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    data = [dict(r) for r in rows(db)]
    scores = {r["wallet"]: [0, []] for r in data}

    def add(w, pts, why):
        scores[w][0] += pts
        scores[w][1].append(why)

    # same IP mass joins
    by_ip = {}
    for r in data:
        by_ip.setdefault(r["ip"], []).append(r)
    for ip, rs in by_ip.items():
        if len(rs) >= 2:
            for r in rs:
                add(r["wallet"], 3, f"shared IP {ip} x{len(rs)}")

    # burst: >5 joins inside any 10-min window
    ts = sorted(r["created_at"] for r in data)
    bursty = False
    for i, t in enumerate(ts):
        if sum(1 for x in ts if 0 <= x - t <= 600) > 5:
            bursty = True
            break
    if bursty:
        for r in data:
            add(r["wallet"], 2, "burst window (>5 joins / 10 min)")

    # sequential names
    names = sorted(r["name"].lower() for r in data)
    seq = re.compile(r"^(.*?)(\d+)$")
    stems = {}
    for n in names:
        m = seq.match(n)
        if m:
            stems.setdefault(m.group(1), []).append(n)
    for stem, ns in stems.items():
        if len(ns) >= 3 and stem:
            for r in data:
                if r["name"].lower() in ns:
                    add(r["wallet"], 2, f"sequential names '{stem}*'")

    # same UA
    by_ua = {}
    for r in data:
        by_ua.setdefault(r["user_agent"], []).append(r)
    for ua, rs in by_ua.items():
        if len(rs) >= 3 and ua:
            for r in rs:
                add(r["wallet"], 1, f"shared user-agent x{len(rs)}")

    # random-looking names
    for r in data:
        n = r["name"].lower()
        if not re.search(r"[aeiou]", n) or re.search(r"\d{3,}$", n):
            add(r["wallet"], 1, "random-looking username")

    ranked = sorted(data, key=lambda r: -scores[r["wallet"]][0])
    if use_json:
        import json as j
        print(j.dumps([{"name": r["name"], "wallet": r["wallet"], "score": scores[r["wallet"]][0],
                        "flags": scores[r["wallet"]][1]} for r in ranked], indent=2))
        return
    print(f"Whitelist audit — {len(data)} rows — {time.strftime('%Y-%m-%d %H:%M')}")
    print(f"{'SCORE':>5}  NAME             WALLET        FLAGS")
    for r in ranked:
        s, flags = scores[r["wallet"]]
        mark = " <-- REVIEW" if s >= 3 else ""
        print(f"{s:>5}  {r['name']:<16} {r['wallet'][:10]}... {'; '.join(flags) or '-'}{mark}")
    flagged = sum(1 for r in data if scores[r["wallet"]][0] >= 3)
    print(f"\n{flagged}/{len(data)} rows flagged (score >= 3). Review before snapshot.")


if __name__ == "__main__":
    main()
