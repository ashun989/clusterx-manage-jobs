"""Private persistence for mailbox sessions, notification rules and outbox."""

from contextlib import closing, contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

from ..collector import RESOURCE_ID_PATTERN
from .rules import LIFECYCLE, QUEUE_MINUTES, TERMINAL, TRAINING, epoch, identity, key_for, snapshot_signals

API = "/api/v1/subscriptions"
COOKIE = "pt_monitor_email_session"
CODE_TTL = 600
SESSION_TTL = 30 * 86400
DAY = 86400
ADDRESS = re.compile(r"[a-z0-9](?:[a-z0-9_.+-]{0,62}[a-z0-9])?@pjlab\.org\.cn")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


def email_address(value):
    value = value.strip().lower()
    if not ADDRESS.fullmatch(value) or ".." in value:
        raise ValueError("请输入 @pjlab.org.cn 邮箱。")
    return value


def private_file(folder, name):
    path = folder / name
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError(f"{name} must be a private regular file")
    return path


class RateLimit(Exception):
    pass


class Store:
    def __init__(self, folder, clock=time.time):
        self.folder = Path(folder).expanduser()
        if self.folder.is_symlink() or not self.folder.is_dir() or self.folder.stat().st_mode & 0o077:
            raise ValueError("notifications directory must be private")
        self.key = private_file(self.folder, "notification-key").read_bytes().strip()
        if len(self.key) < 32:
            raise ValueError("notification key must have at least 32 bytes")
        self.origin = private_file(self.folder, "origin").read_text().strip()
        parsed = urlsplit(self.origin)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or self.origin != f"{parsed.scheme}://{parsed.netloc}" or parsed.username is not None:
            raise ValueError("notifications origin must be an HTTP(S) origin")
        self.clock = clock
        self.path = self.folder / "subscriptions.sqlite3"
        self.path.touch(mode=0o600, exist_ok=True)
        private_file(self.folder, self.path.name)
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS challenge (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL, source TEXT NOT NULL,
                    created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, used INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS challenge_time ON challenge(created);
                CREATE TABLE IF NOT EXISTS session (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS target (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, cluster TEXT NOT NULL, queue TEXT NOT NULL, kind TEXT NOT NULL,
                    uid TEXT NOT NULL, name TEXT NOT NULL, resource TEXT NOT NULL, lookup REAL NOT NULL DEFAULT 0,
                    state TEXT, state_at REAL, last_seen REAL, pending_since REAL, pending_open INTEGER,
                    UNIQUE(cluster,queue,kind,uid));
                CREATE TABLE IF NOT EXISTS subscription (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL, target INTEGER NOT NULL REFERENCES target(id),
                    created REAL NOT NULL, UNIQUE(email,target));
                CREATE INDEX IF NOT EXISTS followers ON subscription(target);
                CREATE TABLE IF NOT EXISTS preference (
                    subscription TEXT NOT NULL REFERENCES subscription(id) ON DELETE CASCADE,
                    rule TEXT NOT NULL, enabled REAL NOT NULL, PRIMARY KEY(subscription,rule));
                CREATE TABLE IF NOT EXISTS observation (
                    target INTEGER NOT NULL REFERENCES target(id) ON DELETE CASCADE,
                    at REAL NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(target,at));
                CREATE INDEX IF NOT EXISTS observation_time ON observation(at);
                CREATE TABLE IF NOT EXISTS signal_state (
                    target INTEGER NOT NULL REFERENCES target(id) ON DELETE CASCADE, rule TEXT NOT NULL,
                    level INTEGER NOT NULL, observed REAL NOT NULL, PRIMARY KEY(target,rule));
                CREATE TABLE IF NOT EXISTS delivery (
                    id TEXT PRIMARY KEY, category TEXT NOT NULL, ref TEXT NOT NULL, rule TEXT,
                    payload TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    due REAL NOT NULL, expires REAL NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS delivery_due ON delivery(state,due);
                CREATE INDEX IF NOT EXISTS delivery_ref ON delivery(ref,state);
            """)

    @property
    def advertised(self):
        return (self.folder / "enabled").is_file()

    @property
    def paused(self):
        return (self.folder / "paused").is_file()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.8)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self, *, write=True):
        with closing(self.connect()) as db, db:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def digest(self, purpose, value):
        return hmac.new(self.key, f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()

    def code(self, challenge):
        return f"{int(self.digest('pt-code', challenge), 16) % 1000000:06d}"

    def challenge(self, email, source):
        now, identifier = self.clock(), secrets.token_urlsafe(24)
        with self.transaction() as db:
            limits = db.execute("""SELECT count(*), sum(email=?), sum(source=?),
                sum(email=? AND created>?) FROM challenge WHERE created>?""",
                (email, self.digest("pt-source", source), email, now - 60, now - 3600)).fetchone()
            if any((count or 0) >= cap for count, cap in zip(limits, (100, 5, 20, 1))):
                raise RateLimit
            db.execute("UPDATE challenge SET used=1 WHERE email=? AND used=0", (email,))
            db.execute("INSERT INTO challenge(id,email,source,created) VALUES (?,?,?,?)",
                       (identifier, email, self.digest("pt-source", source), now))
            self.enqueue(db, f"verify:{identifier}", "verify", identifier, None, {}, now, now + CODE_TTL)
        return identifier

    def verify(self, identifier, code, old_token, follow=None):
        now = self.clock()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM challenge WHERE id=?", (identifier,)).fetchone()
            if not row or row["used"] or row["attempts"] >= 5 or now >= row["created"] + CODE_TTL:
                return None
            db.execute("UPDATE challenge SET attempts=attempts+1 WHERE id=?", (identifier,))
            if not hmac.compare_digest(self.code(identifier), code):
                return None
            db.execute("UPDATE challenge SET used=1 WHERE id=?", (identifier,))
            if old_token:
                db.execute("DELETE FROM session WHERE id=?", (self.digest("pt-session", old_token),))
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO session VALUES (?,?,?)",
                       (self.digest("pt-session", token), row["email"], now + SESSION_TTL))
            limited = False
            if follow:
                try:
                    self.save_follow(db, row["email"], *follow, replace=False)
                except RateLimit:
                    limited = True
            return token, row["email"], limited

    def session(self, token):
        if token and TOKEN.fullmatch(token):
            with self.transaction(write=False) as db:
                row = db.execute("SELECT email FROM session WHERE id=? AND expires>?",
                                 (self.digest("pt-session", token), self.clock())).fetchone()
                if row:
                    return row["email"]
        return None

    def logout(self, token):
        with self.transaction() as db:
            db.execute("DELETE FROM session WHERE id=?", (self.digest("pt-session", token),))

    def save_follow(self, db, email, scope, task, rules, *, replace):
        kind, uid = identity(task)
        now = self.clock()
        target = db.execute("""SELECT id FROM target WHERE cluster=? AND queue=? AND kind=? AND uid=?""",
                            (*scope, kind, uid)).fetchone()
        existing = target and db.execute("SELECT id FROM subscription WHERE email=? AND target=?",
                                         (email, target["id"])).fetchone()
        if existing:
            if replace:
                self.preferences(db, existing["id"], rules, now)
            return
        if db.execute("SELECT count(*) FROM subscription WHERE email=?", (email,)).fetchone()[0] >= 100:
            raise RateLimit
        if not target:
            resource = task.get("resource_name") or ""
            rid = parse_qs(urlsplit(task.get("console_url") or "").query).get("rid", [""])[0]
            match = RESOURCE_ID_PATTERN.fullmatch(rid)
            if match and match.group(5) == "trainingJobs":
                resource = rid
            target_id = db.execute("""INSERT INTO target(cluster,queue,kind,uid,name,resource)
                VALUES (?,?,?,?,?,?)""", (*scope, kind, uid, task["workload_name"], resource)).lastrowid
        else:
            target_id = target["id"]
        identifier = secrets.token_urlsafe(24)
        db.execute("INSERT INTO subscription VALUES (?,?,?,?)", (identifier, email, target_id, now))
        self.preferences(db, identifier, rules, now)

    @staticmethod
    def preferences(db, identifier, rules, now):
        existing = {row[0] for row in db.execute("SELECT rule FROM preference WHERE subscription=?", (identifier,))}
        for rule in existing.difference(rules):
            db.execute("DELETE FROM preference WHERE subscription=? AND rule=?", (identifier, rule))
            db.execute("UPDATE delivery SET state='cancelled',updated=? WHERE ref=? AND rule=? AND state='pending'",
                       (now, identifier, rule))
        for rule in set(rules).difference(existing):
            db.execute("INSERT INTO preference VALUES (?,?,?)", (identifier, rule, now))

    def follows(self, email, scope):
        now = self.clock()
        with self.transaction(write=False) as db:
            rows = db.execute("""SELECT s.id,t.kind,t.uid,t.name,t.state,t.state_at,t.last_seen
                FROM subscription s JOIN target t ON s.target=t.id
                WHERE s.email=? AND t.cluster=? AND t.queue=? ORDER BY s.created DESC,s.id""",
                              (email, *scope)).fetchall()
            result = []
            for row in rows:
                rules = [r[0] for r in db.execute("SELECT rule FROM preference WHERE subscription=? ORDER BY rule", (row["id"],))]
                terminal = row["state"] in TERMINAL
                state = row["state"] if terminal or now - (row["last_seen"] or 0) <= 180 else None
                result.append({"uid": row["uid"], "kind": row["kind"], "name": row["name"],
                               "state": state, "occurred_at": row["state_at"] if state else None,
                               "events": ["queue.wait" if r.startswith("queue.wait.") else r for r in rules],
                               "queue_minutes": next((int(r.rsplit(".", 1)[1]) for r in rules if r.startswith("queue.wait.")), 60)})
            return result

    def remove(self, db, identifier):
        db.execute("DELETE FROM subscription WHERE id=?", (identifier,))
        db.execute("UPDATE delivery SET state='cancelled',updated=? WHERE ref=? AND state='pending'",
                   (self.clock(), identifier))

    def unfollow(self, email, scope, kind, uid):
        with self.transaction() as db:
            row = db.execute("""SELECT s.id FROM subscription s JOIN target t ON s.target=t.id
                WHERE s.email=? AND t.cluster=? AND t.queue=? AND t.kind=? AND t.uid=?""",
                             (email, *scope, kind, uid)).fetchone()
            if row:
                self.remove(db, row["id"])

    def unsubscribe(self, token):
        identifier, _, signature = token.partition(".")
        if not identifier or not hmac.compare_digest(self.digest("pt-unsubscribe", identifier), signature):
            return False
        with self.transaction() as db:
            self.remove(db, identifier)
        return True

    def observe(self, snapshot, policy):
        at = epoch(snapshot.get("generated_at"))
        now = self.clock()
        allowance = 2 * policy.refresh_seconds
        if at is None or not 0 <= now - at <= allowance:
            return
        with self.transaction(write=False) as db:
            targets = {(r["kind"], r["uid"]): r["id"] for r in db.execute("""SELECT t.* FROM target t
                WHERE t.cluster=? AND t.queue=? AND (t.state IS NULL OR t.state NOT IN ('COMPLETED','FAILED','TERMINATED'))
                AND EXISTS (SELECT 1 FROM subscription WHERE target=t.id)""", (snapshot["cluster"], snapshot["queue"]))}
        batch = {}
        # Running placement takes precedence during a Pending inventory transition.
        for pending in (True, False):
            for task in snapshot.get("pending_workloads" if pending else "workloads", []):
                target = targets.get(identity(task))
                if target is None:
                    continue
                signals = snapshot_signals(task, snapshot, policy, at, pending)
                phase = None
                if task["type"] == TRAINING:
                    if not pending:
                        phase = "RUNNING"
                    elif snapshot.get("pending_complete"):
                        phase = "PENDING"
                payload = {"signals": signals, "phase": phase,
                           "queue_time": epoch(task.get("queue_transition_time"))}
                batch[target] = (target, at, at + allowance, json.dumps(payload))
        if batch:
            with self.transaction() as db:
                db.executemany("INSERT OR IGNORE INTO observation VALUES (?,?,?,?)", batch.values())

    def consume(self):
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM observation ORDER BY at LIMIT 200").fetchall()
            for row in rows:
                db.execute("DELETE FROM observation WHERE target=? AND at=?", (row["target"], row["at"]))
                if row["expires"] < self.clock():
                    continue
                task = db.execute("SELECT * FROM target WHERE id=?", (row["target"],)).fetchone()
                if task["state"] in TERMINAL or row["at"] <= (task["last_seen"] or 0):
                    continue
                at = row["at"]
                payload = json.loads(row["payload"])
                phase, signals = payload["phase"], payload["signals"]
                pending_since, pending_open = task["pending_since"], task["pending_open"]
                if phase == "RUNNING":
                    pending_open = 0
                elif phase == "PENDING" and pending_open != 1:
                    changed_at = payload["queue_time"]
                    pending_since = None if pending_open is None else (
                        changed_at if changed_at and (task["last_seen"] or 0) <= changed_at <= at else at
                    )
                    pending_open = 1
                if phase == "PENDING" and pending_since is not None:
                    for minutes in QUEUE_MINUTES:
                        key = key_for("queue.wait", minutes)
                        if key in signals:
                            threshold = pending_since + minutes * 60
                            signals[key].update(level=int(at >= threshold), at=threshold)
                db.execute("UPDATE target SET last_seen=?,pending_since=?,pending_open=? WHERE id=?",
                           (at, pending_since, pending_open, task["id"]))
                for rule, value in signals.items():
                    previous = db.execute("SELECT level,observed FROM signal_state WHERE target=? AND rule=?",
                                          (task["id"], rule)).fetchone()
                    level = value["level"]
                    db.execute("""INSERT INTO signal_state VALUES (?,?,?,?)
                        ON CONFLICT(target,rule) DO UPDATE SET level=excluded.level,observed=excluded.observed""",
                               (task["id"], rule, level, at))
                    if level and ((previous is not None and level > previous["level"])
                                  or (previous is None and value["at"] is not None)):
                        self.emit(db, task, rule, value["at"] or at, value["details"])
            return len(rows)

    def lookup_batch(self, scope):
        now = self.clock()
        with self.transaction() as db:
            rows = db.execute("""SELECT t.* FROM target t
                WHERE t.cluster=? AND t.queue=? AND t.kind=? AND (t.state IS NULL OR t.state NOT IN ('COMPLETED','FAILED','TERMINATED'))
                AND t.lookup<=? AND EXISTS (SELECT 1 FROM subscription s JOIN preference p ON p.subscription=s.id
                    WHERE s.target=t.id AND p.rule IN (?,?,?,?))
                ORDER BY t.lookup,t.id LIMIT 4""", (*scope, TRAINING, now - 60, *LIFECYCLE)).fetchall()
            db.executemany("UPDATE target SET lookup=? WHERE id=?", ((now, r["id"]) for r in rows))
            return [dict(r) for r in rows]

    def lifecycle(self, target, row):
        if row.get("uid") != target["uid"]:
            return
        status = row.get("status") or {}
        state = status.get("state")
        if state not in ("RUNNING", "PENDING", *TERMINAL):
            return
        now = self.clock()
        at = epoch(status.get("end_time" if state in TERMINAL else "start_time" if state == "RUNNING" else "create_time"))
        if not at or at > now + 1:
            return
        with self.transaction() as db:
            fresh = db.execute("SELECT * FROM target WHERE id=?", (target["id"],)).fetchone()
            if not fresh or fresh["state"] in TERMINAL:
                return
            db.execute("UPDATE target SET state=?,state_at=? WHERE id=?", (state, at, target["id"]))
            if state in TERMINAL or state == "RUNNING":
                started = epoch(status.get("start_time"))
                if started and started <= at:
                    self.emit(db, fresh, "job.started", started, {})
            if state in TERMINAL:
                self.emit(db, fresh, TERMINAL[state], at, {})

    def emit(self, db, task, rule, happened, details):
        now = self.clock()
        expires = happened + DAY
        if expires <= now:
            return
        rows = db.execute("""SELECT s.id FROM subscription s JOIN preference p ON p.subscription=s.id
            WHERE s.target=? AND p.rule=? AND p.enabled<?""", (task["id"], rule, happened)).fetchall()
        payload = {"kind": task["kind"], "uid": task["uid"], "name": task["name"],
                   "at": happened, "details": details}
        for row in rows:
            key = f"{row['id']}:{rule}:{happened:.6f}"
            self.enqueue(db, key, "event", row["id"], rule, payload, now, expires)

    @staticmethod
    def enqueue(db, identifier, category, ref, rule, payload, now, expires):
        db.execute("""INSERT OR IGNORE INTO delivery(id,category,ref,rule,payload,state,due,expires,updated)
            VALUES (?,?,?,?,?,'pending',?,?,?)""",
            (identifier, category, ref, rule, json.dumps(payload, ensure_ascii=False), now, expires, now))

    @staticmethod
    def recipient(db, item):
        if item["category"] == "verify":
            return db.execute("SELECT email FROM challenge WHERE id=? AND used=0 AND attempts<5",
                              (item["ref"],)).fetchone()
        return db.execute("""SELECT s.email FROM subscription s JOIN preference p ON p.subscription=s.id
            WHERE s.id=? AND p.rule=? AND p.enabled<?""",
                          (item["ref"], item["rule"], json.loads(item["payload"])["at"])).fetchone()

    def recover_send(self):
        with self.transaction() as db:
            db.execute("UPDATE delivery SET state='unknown',updated=? WHERE state='sending'", (self.clock(),))

    def claim(self):
        now = self.clock()
        with self.transaction() as db:
            db.execute("UPDATE delivery SET state='expired',updated=? WHERE state='pending' AND expires<=?", (now, now))
            while row := db.execute("""SELECT * FROM delivery WHERE state='pending' AND due<=?
                    ORDER BY category='verify' DESC,due,id LIMIT 1""", (now,)).fetchone():
                recipient = self.recipient(db, row)
                if recipient:
                    db.execute("UPDATE delivery SET state='sending',attempts=attempts+1,updated=? WHERE id=?", (now, row["id"]))
                    return {**dict(row), "email": recipient["email"], "attempts": row["attempts"] + 1}
                db.execute("UPDATE delivery SET state='cancelled',updated=? WHERE id=?", (now, row["id"]))
        return None

    def may_send(self, item):
        with self.transaction(write=False) as db:
            return not self.paused and self.clock() < item["expires"] and self.recipient(db, item) is not None

    def finish(self, item, result):
        now = due = self.clock()
        if result == "retry":
            backoff = (60, 300, 900)
            index = item["attempts"] - 1
            due = now + backoff[index] if index < len(backoff) else item["expires"]
            result = "pending" if due < item["expires"] else "failed"
        with self.transaction() as db:
            db.execute("UPDATE delivery SET state=?,due=?,updated=? WHERE id=? AND state='sending'",
                       (result, due, now, item["id"]))

    def status(self):
        with self.transaction(write=False) as db:
            return {"paused": self.paused, "advertised": self.advertised,
                    "queue": dict(db.execute("SELECT state,count(*) FROM delivery GROUP BY state").fetchall()),
                    "last_sent": db.execute("SELECT max(updated) FROM delivery WHERE state='sent'").fetchone()[0]}

    def prune(self):
        now = self.clock()
        with self.transaction() as db:
            db.execute("DELETE FROM challenge WHERE created<?", (now - 3600,))
            db.execute("DELETE FROM session WHERE expires<=?", (now,))
            db.execute("DELETE FROM observation WHERE expires<?", (now,))
            db.execute("DELETE FROM target WHERE NOT EXISTS (SELECT 1 FROM subscription WHERE target=target.id)")
            db.execute("DELETE FROM delivery WHERE updated<? AND state NOT IN ('pending','sending')", (now - 7 * DAY,))
