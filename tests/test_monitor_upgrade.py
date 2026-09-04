from __future__ import annotations

import time
import unittest

from clusterx_monitor.collector import (
    _list_queue_nodes,
    _list_node_pods,
    _list_training_jobs_page,
    _response_total,
    _training_job_page,
    resource_number,
)
from clusterx_monitor.gateway import ClusterxGateway, GatewayError, GatewayTimeouts


class MonitorUpgradeTests(unittest.TestCase):
    def test_gateway_bounds_and_classifies_stuck_sdk_call(self):
        gateway = ClusterxGateway(
            object(), timeouts=GatewayTimeouts(connect=0.01, read=0.01, retries=0),
        )
        with self.assertRaisesRegex(GatewayError, "timed out") as context:
            gateway._call("fake", lambda: (time.sleep(0.1), None)[1])
        self.assertEqual(context.exception.category, "timeout")

    def test_pod_pagination_rejects_malformed_duplicate_payload(self):
        class Client:
            subscription = "s"
            resource_group = "g"
            region = "r"

            def _make_management_request(self, *_args, **_kwargs):
                return {"pods": [{"uid": "same"}], "total_size": 2}

        class Cluster:
            client = Client()

        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            _list_node_pods(Cluster(), "cluster", "queue", "node")

    def test_resource_units_are_normalized(self):
        self.assertEqual(resource_number("500m"), 0.5)
        self.assertEqual(resource_number("240GiB", memory=True), 240)
        self.assertAlmostEqual(resource_number("1024MiB", memory=True), 1)

    def test_removed_legacy_response_aliases_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "unsupported total field"):
            _response_total({"total": 1}, "queue node")
        with self.assertRaisesRegex(RuntimeError, "unsupported trainingJobs field"):
            _training_job_page({"trainingJobs": []}, "pending workload")

        client = type("Client", (), {
            "list_queue_nodes": lambda _self, **_kwargs: {"nodes": [], "total": 0},
        })()
        with self.assertRaisesRegex(RuntimeError, "unsupported total field"):
            _list_queue_nodes(type("Cluster", (), {"client": client})(), "cluster", "queue")

    def test_training_job_page_uses_signed_token_request_after_first_page(self):
        class Client:
            def __init__(self):
                self.calls = []

            def list_training_jobs(self, **kwargs):
                self.calls.append(("list", kwargs))
                return {"training_jobs": [{"uid": "first"}], "total_size": 2,
                        "next_page_token": "cursor-1"}

            def _get_base_path(self):
                return "/subscriptions/s/workspaces/w/"

            def _make_request(self, *args, **kwargs):
                self.calls.append(("raw", args, kwargs))
                return {"training_jobs": [{"uid": "second"}], "total_size": 2}

        cluster = type("Cluster", (), {"client": Client()})()
        first = _list_training_jobs_page(cluster, "queue-id", "RUNNING", None)
        second = _list_training_jobs_page(cluster, "queue-id", "RUNNING", "cursor-1")
        self.assertEqual([item["uid"] for item in first["training_jobs"] + second["training_jobs"]], ["first", "second"])
        self.assertEqual(cluster.client.calls[0][0], "list")
        self.assertEqual(cluster.client.calls[1][0], "raw")
        self.assertEqual(cluster.client.calls[1][2]["params"]["page_token"], "cursor-1")


if __name__ == "__main__":
    unittest.main()
