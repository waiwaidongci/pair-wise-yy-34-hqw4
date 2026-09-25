import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlrequest

from src.http_api import make_handler
from src.repository import Repository
from src.service import Service


def http(method, url, payload=None, role="viewer", actor="tester"):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"X-Actor": actor, "X-Role": role}
    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        body = json.loads(exc.read().decode("utf-8"))
        return exc.code, body


class HttpMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            self.service, str(Path(__file__).resolve().parent.parent / "static")))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.repo.close()
        self.tmp.cleanup()

    def accident(self, ref, source="team", **extra):
        payload = {
            "title": ref, "description": "http duplicate", "severity": "minor",
            "quantity": 1, "threshold": 10, "external_ref": ref,
            "site_ref": "HTTP-SITE", "injured_person": "周九",
            "incident_start": "2026-09-25T08:00:00+00:00",
            "incident_end": "2026-09-25T09:00:00+00:00",
            "report_source": source,
        }
        payload.update(extra)
        return http("POST", f"{self.base}/api/items", payload,
                    role="safety_manager" if source == "safety" else "reporter")[1]

    def test_merge_api_lifecycle(self):
        team = self.accident("HTTP-TEAM")
        safety = self.accident(
            "HTTP-SAFE", source="safety", severity="serious",
            start="2026-09-25T08:30:00+00:00",
            end="2026-09-25T09:30:00+00:00")
        request_id = safety["merge_request_id"]

        status, body = http("GET", f"{self.base}/api/merge-requests/{request_id}")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["members"]), 2)

        status, body = http("POST", f"{self.base}/api/merge-requests/{request_id}/merge", {
            "master_item_id": safety["id"], "expected_version": safety["version"],
        }, role="safety_manager")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "merged")

        status, team_body = http("GET", f"{self.base}/api/items/{team['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(team_body["status"], "merged")

        status, body = http("POST", f"{self.base}/api/merge-requests/{request_id}/undo",
                            role="safety_manager")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "undone")


if __name__ == "__main__":
    unittest.main()
