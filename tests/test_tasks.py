import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES, priority_score


class TaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "task item", "description": "task tracking",
             "severity": "major", "quantity": 5, "threshold": 10,
             "external_ref": "TASK-1"}, "creator", "observer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _task(self, **overrides):
        payload = {"category": "recovery", "responsible_unit": "清污一队",
                   "deadline": "2099-01-01T00:00:00Z", "external_ref": "T-1"}
        payload.update(overrides)
        return payload

    def _advance_to_monitoring(self):
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def test_task_create_validate_duplicate_and_audit(self):
        task = self.service.create_task(
            self.item["id"], self._task(), "ops", "operations")
        self.assertEqual(task["status"], "open")
        self.assertFalse(task["overdue"])
        self.assertEqual(task["responsible_unit"], "清污一队")
        with self.assertRaises(ConflictError):
            self.service.create_task(
                self.item["id"], self._task(), "ops", "operations")
        with self.assertRaises(PermissionDenied):
            self.service.create_task(
                self.item["id"], self._task(external_ref="T-2"),
                "attacker", "viewer")
        with self.assertRaises(ValidationError):
            self.service.create_task(
                self.item["id"], self._task(category="unknown", external_ref="T-3"),
                "ops", "operations")
        with self.assertRaises(ValidationError):
            self.service.create_task(
                self.item["id"], self._task(deadline="not-a-date", external_ref="T-4"),
                "ops", "operations")
        done = self.service.update_task_status(
            self.item["id"], task["id"], {"status": "done"}, "ops", "operations")
        self.assertEqual(done["status"], "done")
        self.assertIsNotNone(done["completed_at"])
        with self.assertRaises(ConflictError):
            self.service.update_task_status(
                self.item["id"], task["id"], {"status": "done"},
                "ops", "operations")
        events = self.service.audit("viewer", self.item["id"])
        actions = [e["action"] for e in events]
        self.assertIn("task_create", actions)
        self.assertIn("task_status", actions)
        status_event = [e for e in events if e["action"] == "task_status"][0]
        self.assertEqual(status_event["detail"]["from"], "open")
        self.assertEqual(status_event["detail"]["to"], "done")
        self.assertEqual(status_event["detail"]["task_id"], task["id"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_close_requires_recovery_done_and_shoreline_closed(self):
        self.service.create_task(
            self.item["id"], self._task(), "ops", "operations")
        record = self.service.add_record(
            self.item["id"],
            {"kind": "shoreline_monitoring", "detail": "岸线监测",
             "status": "open", "external_ref": "SM-1"},
            "recorder", "response_commander")
        current = self._advance_to_monitoring()
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], "closed", current["version"],
                                    "reviewer", "response_commander")
        self.assertIn("回收任务未完成", str(ctx.exception))
        self.assertIn("岸线监测记录未关闭", str(ctx.exception))
        task = self.service.list_tasks(self.item["id"], "viewer")[0]
        self.service.update_task_status(
            self.item["id"], task["id"], {"status": "done"}, "ops", "operations")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], "closed", current["version"],
                                    "reviewer", "response_commander")
        self.assertNotIn("回收任务未完成", str(ctx.exception))
        self.assertIn("岸线监测记录未关闭", str(ctx.exception))
        self.service.update_record_status(
            self.item["id"], record["id"], {"status": "closed"},
            "recorder", "response_commander")
        closed = self.service.transition(
            current["id"], "closed", current["version"], "reviewer",
            "response_commander")
        self.assertEqual(closed["status"], "closed")
        with self.assertRaises(ConflictError):
            self.service.create_task(
                self.item["id"], self._task(external_ref="T-9"),
                "ops", "operations")

    def test_overdue_task_raises_priority_and_shows_in_list(self):
        base = priority_score("major", 5, 10, 0, 0)
        self.service.create_task(
            self.item["id"], self._task(deadline="2000-01-01T00:00:00Z"),
            "ops", "operations")
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["overdue_tasks"], 1)
        self.assertEqual(item["open_tasks"], 1)
        self.assertGreater(item["priority"], base)
        listed = [i for i in self.service.list_items("viewer")
                  if i["id"] == self.item["id"]][0]
        self.assertEqual(listed["overdue_tasks"], 1)
        self.assertEqual(listed["priority"], item["priority"])
        task = self.service.list_tasks(self.item["id"], "viewer")[0]
        self.assertTrue(task["overdue"])
        self.service.update_task_status(
            self.item["id"], task["id"], {"status": "done"}, "ops", "operations")
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["overdue_tasks"], 0)
        self.assertEqual(item["priority"], base)
        task = self.service.list_tasks(self.item["id"], "viewer")[0]
        self.assertFalse(task["overdue"])


if __name__ == "__main__":
    unittest.main()
