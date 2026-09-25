import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


def payload(external_ref='FAIL-1'):
    return {
        "title": "failure item", "description": "failure scenarios",
        "severity": 'serious', "quantity": 5, "threshold": 10,
        "external_ref": external_ref, "site_ref": "FAIL-SITE",
        "injured_person": "李四",
        "incident_start": "2026-09-25T08:00:00+00:00",
        "incident_end": "2026-09-25T09:00:00+00:00",
    }


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item(payload(),"creator",'reporter')
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def test_permission_version_duplicate_and_invariant(self):
        with self.assertRaises(PermissionDenied): self.service.transition(self.item["id"],STATES[1],1,"attacker","viewer")
        with self.assertRaises(ConflictError): self.service.transition(self.item["id"],STATES[1],99,"reviewer",TRANSITION_ROLES[STATES[1]][0])
        payload_record={"kind":"action","detail":"same reference","status":"open","external_ref":"DUP-1"}
        self.service.add_record(self.item["id"],payload_record,"recorder",'investigator')
        with self.assertRaises(ConflictError): self.service.add_record(self.item["id"],payload_record,"recorder",'investigator')
        current=self.service.get_item(self.item["id"],"viewer")
        for target in STATES[1:-1]: current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError): self.service.transition(current["id"],STATES[-1],current["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
if __name__=="__main__": unittest.main()
