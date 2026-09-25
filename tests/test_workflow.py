import tempfile, unittest
from pathlib import Path
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


def report_payload(severity='serious', external_ref='WF-1', start='2026-09-25T08:00:00+00:00',
                   end='2026-09-25T09:00:00+00:00'):
    return {
        "title": "workflow item", "description": "complete business flow",
        "severity": severity, "quantity": 12, "threshold": 6,
        "external_ref": external_ref, "site_ref": "SITE-1",
        "injured_person": "张三", "incident_start": start, "incident_end": end,
        "report_source": "team",
    }


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def test_complete_workflow_and_audit(self):
        item=self.service.create_item(report_payload(),"creator",'reporter')
        self.assertEqual(item["status"],STATES[0])
        self.service.add_record(item["id"],{"kind":"evidence","detail":"evidence registered","status":"closed","external_ref":"EV-1"},"recorder",'investigator')
        current=item
        for target in STATES[1:]:
            current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"],STATES[-1])
        self.assertEqual(len(self.service.list_records(current["id"],"viewer")),1)
        events=self.service.audit("viewer",current["id"]); self.assertGreaterEqual(len(events),len(STATES)+1); self.assertTrue(self.repo.verify_audit_chain())
if __name__=="__main__": unittest.main()
