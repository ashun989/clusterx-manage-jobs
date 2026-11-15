from datetime import datetime, timezone
from types import SimpleNamespace
import json
import smtplib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_monitor_api import TestClient as ApiClient, raw_snapshot
from clusterx_monitor.models import PolicyConfig
from clusterx_monitor.notifications.rules import snapshot_signals
from clusterx_monitor.notifications.store import Store
from clusterx_monitor.notifications.worker import message, send_mail
from clusterx_monitor.service import MonitorRuntime, create_app


SCOPE = ("c", "q")
EMAIL = "alice@pjlab.org.cn"
ORIGIN = {"Origin": "http://testserver"}
API = "/api/v1/subscriptions"


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def fixture(tmp_path):
    (tmp_path / "notification-key").write_bytes(bytes(range(32)))
    (tmp_path / "origin").write_text("http://testserver")
    for path in tmp_path.iterdir():
        path.chmod(0o600)
    (tmp_path / "enabled").touch()
    now = [1_800_000_000.0]
    return Store(tmp_path, clock=lambda: now[0]), now


def workload(uid="first", kind="trainingJob"):
    return {"workload_uid": uid, "workload_id": "queue-name", "workload_name": "test job",
            "resource_name": "test-job", "type": kind, "total_gpu": 4}


def follow(store, task, *events, email=EMAIL):
    with store.transaction() as db:
        store.save_follow(db, email, SCOPE, task, events, replace=True)


def snapshot(at, *, running=(), pending=(), complete=True):
    return {"generated_at": iso(at), "cluster": "c", "queue": "q", "workloads": list(running),
            "pending_workloads": list(pending), "pending_complete": complete}


def policy():
    return PolicyConfig(groups={"default": {}})


class NotificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    @unittest.skipIf(ApiClient is None, "httpx2 test client is unavailable")
    def test_email_routes_origin_session_isolation_and_unsubscribe(self):
        store, _ = fixture(self.folder)
        runtime = MonitorRuntime(SimpleNamespace(cluster_name="c", queue="q"), SimpleNamespace(configured=True, policy=policy()))
        runtime.notifications = store
        sample = raw_snapshot()
        sample["workloads"] = [{**sample["workloads"][0], **workload()}]
        runtime.snapshots.publish(sample)
        try:
            app = create_app(runtime)
            client = ApiClient(app)
            other = ApiClient(app)
            assert client.get(API).status_code == 401
            assert client.post(API + "/challenge", json={"email": EMAIL}).status_code == 403
            c = client.post(API + "/challenge", headers=ORIGIN, json={"email": EMAIL})
            assert c.status_code == 202
            assert c.headers["Cache-Control"] == "no-store"
            challenge = c.json()["challenge_id"]
            assert client.post(API + "/verify", headers=ORIGIN, json={"challenge_id": challenge, "code": "12345"}).status_code == 422
            payload = {"challenge_id": challenge, "code": store.code(challenge),
                       "target": {"kind": "trainingJob", "uid": "first"}}
            verified = client.post(API + "/verify", headers=ORIGIN, json=payload)
            assert verified.status_code == 200
            cookie = verified.headers["Set-Cookie"]
            assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=2592000" in cookie
            assert client.post(API + "/verify", headers=ORIGIN, json=payload).status_code == 422
            item = client.get(API).json()[0]
            assert item["events"] == ["job.failed"] and item["uid"] == "first"
            second = other.post(API + "/challenge", headers=ORIGIN, json={"email": "bob@pjlab.org.cn"}).json()["challenge_id"]
            assert other.post(API + "/verify", headers=ORIGIN, json={"challenge_id": second, "code": store.code(second)}).status_code == 200
            assert other.get(API).json() == []
            assert other.get(API + "/trainingJob/first").status_code == 404
            assert client.put(API + "/trainingJob/first", headers=ORIGIN, json={"events": []}).status_code == 403
            headers = {**ORIGIN, "X-CSRF-Token": verified.json()["csrf"]}
            assert client.put(API + "/trainingJob/first", headers=headers, json={"events": ["queue.wait"], "queue_minutes": 30}).json()["queue_minutes"] == 30
            assert client.put(API + "/trainingJob/first", headers=headers, json={"events": ["gpu.typo"]}).status_code == 422
            with store.transaction(write=False) as db:
                sub = db.execute("SELECT id FROM subscription").fetchone()[0]
            token = f"{sub}.{store.digest('pt-unsubscribe', sub)}"
            assert client.get(API + "/unsubscribe").status_code == 405
            assert client.get(API).json()[0]["uid"] == "first"
            assert other.post(API + "/unsubscribe", headers=ORIGIN, json={"token": token}).status_code == 200
            assert client.get(API).json() == []
            assert client.post(API + "/logout", headers=headers).status_code == 200
            assert client.get(API).status_code == 401
        finally:
            runtime.executor.shutdown(wait=False, cancel_futures=True)


    def test_expired_code_attempts_and_persistent_session(self):
        store, now = fixture(self.folder)
        challenge = store.challenge(EMAIL, "127.0.0.1")
        now[0] += 600
        assert store.verify(challenge, store.code(challenge), None) is None
        challenge = store.challenge(EMAIL, "127.0.0.1")
        wrong = "000000" if store.code(challenge) != "000000" else "111111"
        for _ in range(5):
            assert store.verify(challenge, wrong, None) is None
        assert store.verify(challenge, store.code(challenge), None) is None
        now[0] += 60
        challenge = store.challenge(EMAIL, "127.0.0.1")
        token, _, _ = store.verify(challenge, store.code(challenge), None)
        restarted = Store(tmp_path, clock=lambda: now[0])
        assert restarted.session(token) == EMAIL
        now[0] += 30 * 86400
        assert restarted.session(token) is None


    def test_verified_lifecycle_terminal_states_and_dedup(self):
        store, now = fixture(self.folder)
        for uid in ("failed", "completed", "stopped"):
            follow(store, workload(uid), "job.started", "job.failed", "job.completed", "job.stopped")
        now[0] += 120
        targets = store.lookup_batch(SCOPE)
        assert {row["uid"] for row in targets} == {"failed", "completed", "stopped"}
        for target, state in zip(targets, ("FAILED", "COMPLETED", "TERMINATED")):
            status = {"state": state, "start_time": iso(now[0] - 80), "end_time": iso(now[0] - 1)}
            store.lifecycle(target, {"uid": "replacement", "status": status})
            store.lifecycle(target, {"uid": target["uid"], "status": {"state": "RESTARTING"}})
            store.lifecycle(target, {"uid": target["uid"], "status": status})
            store.lifecycle(target, {"uid": target["uid"], "status": status})
        assert store.status()["queue"] == {"pending": 6}
        assert {item["state"] for item in store.follows(EMAIL, SCOPE)} == {"FAILED", "COMPLETED", "TERMINATED"}
        item = store.claim()
        mail = message(store, item, "monitor@pjlab.org.cn")
        assert "followTask=trainingJob%3A" in mail.get_content()
        assert "#unsubscribe=" in mail.get_content()
        assert store.may_send(item)
        store.unfollow(EMAIL, SCOPE, "trainingJob", json.loads(item["payload"])["uid"])
        assert not store.may_send(item)
        store.finish(item, "cancelled")
        assert store.status()["queue"] == {"cancelled": 2, "pending": 4}
        store.recover_send()
        next_item = store.claim()
        assert next_item
        store.recover_send()
        assert store.status()["queue"]["unknown"] == 1


    def test_pending_uid_transition_gap_and_requeue(self):
        store, now = fixture(self.folder)
        task = workload()
        created = now[0] - 10
        follow(store, task, "queue.wait.30")
        for advance, complete, running in ((60, True, False), (1800, False, False), (60, True, False), (30, True, False), (30, True, True)):
            now[0] += advance
            queued = {**task, "resource_create_time": iso(created)}
            assert task["workload_uid"] == queued["workload_uid"]
            store.observe(snapshot(now[0], running=[{**task, "workload_id": "first"}] if running else (),
                                   pending=() if running else [queued], complete=complete), policy())
            store.consume()
            if not complete:
                assert store.status()["queue"] == {}
        assert store.status()["queue"] == {"pending": 1}
        now[0] += 30
        queued["queue_transition_time"] = iso(now[0])
        store.observe(snapshot(now[0], pending=[queued]), policy())
        store.consume()
        assert store.status()["queue"] == {"pending": 1}
        now[0] += 1800
        store.observe(snapshot(now[0], pending=[queued]), policy())
        store.consume()
        assert store.status()["queue"] == {"pending": 2}


    def test_rule_coverage_baseline_recovery_and_escalation(self):
        store, now = fixture(self.folder)
        task = workload()
        follow(store, task, "gpu.compute", "placement.outside_owned_pool")
        configured = policy().model_copy(update={"node_allocation": SimpleNamespace(enabled=True)})
        gpu = {"code": "utilization.low_gpu_activity", "observed": {"triggered_metrics": ["compute"]}}
        placement = {"code": "placement.outside_owned_pool", "status": "warning"}
        task.update(placement_context={"mode": "managed"})
        for index, (compute, finding, coverage) in enumerate((
            (True, "warning", 4), (False, "warning", 1), (True, "warning", 4),
            (True, "violation", 4), (False, "violation", 4), (True, "violation", 4),
        )):
            now[0] += 30
            task.update(telemetry={"compute_reported_gpu_count": coverage},
                        historical_telemetry={"fetched_at": iso(now[0]), "evaluation_status": "partial",
                                              "gpu_compute_util_avg_pct": 10 if compute else 80, "compute_sample_count": 12},
                        policy_findings=[{**placement, "status": finding}] + ([gpu] if compute else []))
            sample = {**snapshot(now[0], running=[task]), "telemetry_available": True, "node_allocation": {"enabled": True}}
            store.observe(sample, configured)
            store.consume()
            if index < 3:
                assert store.status()["queue"] == {}
        assert store.status()["queue"] == {"pending": 2}
        assert "gpu.compute" in snapshot_signals(task, sample, configured, now[0], False)


    def test_smtp_retries_only_known_temporary_errors(self):
        store, now = fixture(self.folder)
        c = store.challenge(EMAIL, "127.0.0.1")
        item = store.claim()
        msg = message(store, item, "monitor@pjlab.org.cn")
        with patch("clusterx_monitor.notifications.worker.smtplib.SMTP_SSL") as sender:
            sender.return_value.send_message.side_effect = smtplib.SMTPDataError(451, b"temporary")
            assert send_mail(msg, "monitor@pjlab.org.cn", "test", lambda: True) == "retry"
            sender.return_value.send_message.side_effect = ConnectionError()
            assert send_mail(msg, "monitor@pjlab.org.cn", "test", lambda: True) == "unknown"
        store.finish(item, "retry")
        assert store.claim() is None
        now[0] += 60
        assert store.claim()["ref"] == c
