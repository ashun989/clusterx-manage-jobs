"""Run PT notification evaluation and delivery on the Monitor development host."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate
import fcntl
import json
import logging
import os
import smtplib
import ssl
import threading
import time
from urllib.parse import quote, urlencode, urlsplit

from ..collector import RESOURCE_ID_PATTERN
from .rules import RULES
from .store import Store, email_address, private_file

LOG = logging.getLogger("clusterx_monitor.notifications")
BEIJING = timezone(timedelta(hours=8))


def timestamp(value):
    return datetime.fromtimestamp(value, BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def message(store, item, username):
    msg = EmailMessage()
    msg["From"], msg["To"] = username, item["email"]
    msg["Date"] = formatdate(store.clock())
    msg["Message-ID"] = f"<{store.digest('pt-message', item['id'])}@pjlab.org.cn>"
    if item["category"] == "verify":
        msg["Subject"] = "PT Monitor 邮箱验证码"
        msg.set_content(f"你的验证码是 {store.code(item['ref'])}，10 分钟内有效。")
        return msg
    payload = json.loads(item["payload"])
    code = "queue.wait" if item["rule"].startswith("queue.wait.") else item["rule"]
    label = RULES[code].label
    detail = store.origin + "/?" + urlencode({
        "view": "workloads", "followTask": f"{payload['kind']}:{payload['uid']}",
    })
    token = item["ref"] + "." + store.digest("pt-unsubscribe", item["ref"])
    values = payload["details"]
    extra = ""
    if code.startswith("gpu."):
        extra = (f"观测值 {values['value_pct']:.2f}% · 阈值 {values['threshold_pct']:g}% · "
                 f"近 {values['hours']} 小时 · {values['covered']}/{values['expected']} 张卡\n")
    elif code == "queue.wait":
        extra = f"排队达到 {values['minutes']} 分钟\n"
    elif code.startswith("placement."):
        extra = f"级别 {values['severity']}\n"
    elif code == "runtime.near_limit":
        extra = f"运行时限 {values['limit_hours']:g} 小时 · 提前 {values['lead_hours']:g} 小时\n"
    elif values.get("codes"):
        extra = "规则 " + "、".join(values["codes"]) + "\n"
    msg["Subject"] = f"PT Monitor · {label}"
    msg.set_content(f"{payload['name']}\n{label}\n发生时间 {timestamp(payload['at'])}（北京时间）\n"
                    f"{extra}\n详情 {detail}\n退订此任务 {store.origin}/#unsubscribe={token}\n")
    return msg


def send_mail(msg, username, password, allow):
    client = None
    sending = False
    try:
        client = smtplib.SMTP_SSL("smtp.pjlab.org.cn", 465, timeout=10, context=ssl.create_default_context())
        client.login(username, password)
        if not allow():
            return "cancelled"
        sending = True
        client.send_message(msg)
        return "sent"
    except smtplib.SMTPRecipientsRefused as error:
        return "retry" if any(400 <= value[0] < 500 for value in error.recipients.values()) else "failed"
    except smtplib.SMTPResponseException as error:
        return "retry" if 400 <= error.smtp_code < 500 else "failed"
    except (ssl.SSLCertVerificationError, smtplib.SMTPNotSupportedError):
        return "failed"
    except (OSError, smtplib.SMTPException):
        return "unknown" if sending else "retry"
    finally:
        if client is not None:
            client.close()


def smtp_config(store):
    data = json.loads(private_file(store.folder, "smtp.json").read_text())
    username, password = email_address(data["username"]), data["password"]
    if not isinstance(password, str) or not password:
        raise ValueError("invalid client password")
    return username, password


def training_detail(client, target):
    # This is the SDK's signed read-only detail request with a bounded timeout
    # and without its printing of upstream error bodies.
    import requests
    from clusterx.launcher.ssp.api import get_auth_headers

    resource = target["resource"]
    match = RESOURCE_ID_PATTERN.fullmatch(resource)
    if match and match.group(5) == "trainingJobs":
        path = quote(resource, safe="/")
    elif resource and not any(char in resource for char in "/?#"):
        path = client._get_base_path() + "trainingJobs/" + quote(resource, safe="")
    else:
        return {}
    url = client.compute_endpoint + path
    parsed = urlsplit(url)
    headers = get_auth_headers(ak_id=client.ak_id, ak_secret=client.ak_secret, method="GET",
                               path=parsed.path, host=parsed.netloc)
    response = requests.get(url, headers=headers, timeout=(3, 8), allow_redirects=False)
    if response.status_code != 200:
        return {}
    return response.json()


def evaluate(store):
    last = 0.0
    while True:
        try:
            if store.advertised:
                store.consume()
            if time.monotonic() - last >= 60:
                store.prune()
                last = time.monotonic()
        except Exception as error:
            LOG.warning("evaluation cycle failed: %s", type(error).__name__)
        time.sleep(2)


def probe(store, cluster):
    scope = (str(cluster.cfg.get("cluster")), str(cluster.cfg.get("queue") or cluster.cfg.get("partition")))

    def lookup(target):
        try:
            store.lifecycle(target, training_detail(cluster.client, target))
        except Exception as error:
            LOG.warning("training detail failed: %s", type(error).__name__)

    with ThreadPoolExecutor(max_workers=4) as pool:
        while True:
            try:
                if store.advertised:
                    list(pool.map(lookup, store.lookup_batch(scope)))
            except Exception as error:
                LOG.warning("lifecycle cycle failed: %s", type(error).__name__)
            time.sleep(10)


def main():
    from ..cli import _protected_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "status"))
    parser.add_argument("--notifications-dir", required=True)
    parser.add_argument("--clusterx-config")
    parser.add_argument("--simulate", action="store_true", help="Record simulated delivery without contacting SMTP")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = Store(args.notifications_dir)
    if args.action == "status":
        print(json.dumps(store.status()))
        return
    os.umask(0o077)
    if not args.simulate:
        smtp_config(store)
    cluster = None
    if args.clusterx_config:
        os.environ["CLUSTERX_CFG_PATH"] = str(_protected_config(args.clusterx_config))
        from clusterx.launcher.ssp.ssp import SSPCluster
        cluster = SSPCluster()
    elif not args.simulate:
        parser.error("--clusterx-config is required to run with real delivery")
    with (store.folder / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store.recover_send()
        threading.Thread(target=evaluate, args=(store,), daemon=True).start()
        if cluster:
            threading.Thread(target=probe, args=(store, cluster), daemon=True).start()
        while True:
            item = None
            try:
                if store.advertised and not store.paused:
                    if not args.simulate:
                        sender, password = smtp_config(store)
                    item = store.claim()
                    if item:
                        result = "simulated" if args.simulate else send_mail(
                            message(store, item, sender), sender, password, lambda: store.may_send(item),
                        )
                        store.finish(item, result)
                        continue
            except Exception as error:
                LOG.warning("delivery cycle failed: %s", type(error).__name__)
                if item:
                    try:
                        store.finish(item, "unknown")
                    except Exception:
                        LOG.warning("delivery disposition unavailable")
            time.sleep(2)


if __name__ == "__main__":
    main()
