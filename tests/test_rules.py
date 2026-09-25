import unittest
from src import rules
from src.domain import ConflictError, ValidationError
class RulesTest(unittest.TestCase):
    def test_priority_deadline_and_escalation(self):
        low=rules.priority_score(rules.SEVERITIES[0],1,10,0); high=rules.priority_score(rules.SEVERITIES[-1],30,10,3)
        self.assertGreater(high,low); self.assertLessEqual(rules.response_deadline_hours(rules.SEVERITIES[-1],30,10),rules.response_deadline_hours(rules.SEVERITIES[0],1,10))
        self.assertTrue(rules.escalation_required(rules.SEVERITIES[-1],1,10)); self.assertTrue(rules.escalation_required(rules.SEVERITIES[0],10,10))
    def test_transition_guards(self):
        self.assertTrue(rules.can_transition(rules.STATES[0],rules.STATES[1]))
        with self.assertRaises(ConflictError): rules.validate_transition(rules.STATES[0],rules.STATES[-1])
        with self.assertRaises(ValidationError): rules.priority_score("not-a-severity",1,1)
    def test_duplicate_match_material_recalculation_and_conflict(self):
        first={'item_id':1,'site_ref':'SITE-1','injured_person':'张三','incident_start':'2026-09-25T08:00:00+00:00','incident_end':'2026-09-25T09:00:00+00:00','status':'reported','severity':'minor','quantity':1,'threshold':10}
        second={'item_id':2,'site_ref':'SITE-1','injured_person':'张三','incident_start':'2026-09-25T08:30:00Z','incident_end':'2026-09-25T09:30:00Z','status':'investigating','severity':'fatal','quantity':3,'threshold':5}
        third=dict(second, item_id=3, site_ref='SITE-2')
        closed=dict(second, item_id=4, status='closed')
        self.assertTrue(rules.same_accident(first,second))
        self.assertFalse(rules.same_accident(first,third))
        self.assertFalse(rules.same_accident(first,closed))
        self.assertEqual(rules.recalculate_material([first,second]),
                         {'severity':'fatal','quantity':3.0,'threshold':5.0})
        reason,items=rules.conclusion_conflict([
            {'item_id':1,'report_source':'team','conclusion_code':'work_related'},
            {'item_id':2,'report_source':'safety','conclusion_code':'not_work_related'},
        ])
        self.assertIn('冲突',reason); self.assertEqual(len(items),2)
if __name__=="__main__": unittest.main()
