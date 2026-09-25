import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.accident_http import AccidentRouter
from src.accident_repository import AccidentRepository
from src.accident_service import AccidentService
from src.http_api import make_handler
from src.repository import Repository
from src.service import Service


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class AccidentHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = Repository(str(base / "i.db"))
        self.acc_repo = AccidentRepository(str(base / "a.db"))
        router = AccidentRouter(AccidentService(self.acc_repo))
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", free_port()),
            make_handler(Service(self.repo), str(base), router))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.repo.close()
        self.acc_repo.close()
        self.tmp.cleanup()

    def _request(self, method, path, body=None, role="reporter",
                 actor="u1"):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-Actor": actor, "X-Role": role}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        return resp.status, json.loads(raw) if raw else None

    def _report(self, **over):
        payload = {
            "source": "crew", "scene_no": "WEB-1", "victim_name": "钱七",
            "victim_id": "ID-Q7",
            "occurred_start": "2026-09-20T08:00:00+08:00",
            "occurred_end": "2026-09-20T09:00:00+08:00",
            "severity": "minor", "verdict": "pending",
        }
        payload.update(over)
        return self._request("POST", "/api/accidents", payload)

    def test_http_report_conflict_merge_revoke(self):
        status, first = self._report(verdict="liable", conclusion="班组违章")
        self.assertEqual(status, 201)
        status, second = self._report(
            source="safety", severity="serious",
            verdict="not_liable", conclusion="设备问题")
        self.assertEqual(status, 201)
        gid = second["merge"]["id"]

        status, body = self._request(
            "POST", f"/api/merge-groups/{gid}/designate-master",
            {"master_case_id": second["id"]}, role="safety_manager")
        self.assertEqual(status, 409)
        self.assertIn("conflicts", body)

        status, _ = self._request(
            "POST", f"/api/accidents/{second['id']}/amend-conclusion",
            {"verdict": "pending", "reason": "重新调查"},
            role="safety_manager")
        self.assertEqual(status, 200)

        status, group = self._request(
            "POST", f"/api/merge-groups/{gid}/designate-master",
            {"master_case_id": second["id"]}, role="safety_manager")
        self.assertEqual(status, 200)
        self.assertEqual(group["status"], "merged")

        status, master = self._request(
            "GET", f"/api/accidents/{second['id']}", role="viewer")
        self.assertEqual(master["severity"], "serious")
        self.assertIn(first["case_no"],
                      [a["alias_no"] for a in master["aliases"]])

        status, revoked = self._request(
            "POST", f"/api/merge-groups/{gid}/revoke",
            {"reason": "误并"}, role="safety_manager")
        self.assertEqual(status, 200)
        self.assertEqual(revoked["status"], "revoked")

        status, groups = self._request(
            "GET", "/api/merge-groups?status=revoked", role="viewer")
        self.assertEqual(status, 200)
        self.assertEqual(groups["groups"][0]["status"], "revoked")

        # 原有调查流程接口仍然可用，分层互不影响
        status, item = self._request("POST", "/api/items", {
            "title": "t", "description": "d", "severity": "minor"},
            role="reporter")
        self.assertEqual(status, 201)
        self.assertIn("id", item)


if __name__ == "__main__":
    unittest.main()
