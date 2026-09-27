import tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


def iso(dt):
    return dt.replace(microsecond=0).isoformat()


class TaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "task item", "description": "disposal tasks",
             "severity": "major", "quantity": 12, "threshold": 6,
             "external_ref": "TASK-1"}, "creator", "observer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _payload(self, category="recovery", ref="T-1", hours=24):
        return {"category": category, "responsible_unit": "海事局清污队",
                "due_at": iso(datetime.now(timezone.utc) + timedelta(hours=hours)),
                "external_ref": ref}

    def _walk_to_monitoring(self):
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def test_create_and_list_tasks(self):
        categories = ["containment", "recovery", "shoreline_protection", "waste_disposal"]
        for i, category in enumerate(categories):
            self.service.add_task(self.item["id"], self._payload(category, f"T-{i}"),
                                  "planner", "operations")
        tasks = self.service.list_tasks(self.item["id"], "viewer")
        self.assertEqual(len(tasks), 4)
        self.assertEqual({t["category"] for t in tasks}, set(categories))
        self.assertTrue(all(t["responsible_unit"] == "海事局清污队" for t in tasks))
        self.assertTrue(all(t["status"] == "open" for t in tasks))
        self.assertTrue(all(t["due_at"] for t in tasks))

    def test_duplicate_external_ref_rejected(self):
        with self.assertRaises(ConflictError):
            self.service.create_item(
                {"title": "dup", "description": "dup", "severity": "minor",
                 "quantity": 1, "threshold": 1, "external_ref": "TASK-1"},
                "creator", "observer")
        self.service.add_task(self.item["id"], self._payload(), "planner", "operations")
        with self.assertRaises(ConflictError):
            self.service.add_task(self.item["id"], self._payload(), "planner", "operations")

    def test_invalid_task_payload(self):
        with self.assertRaises(PermissionDenied):
            self.service.add_task(self.item["id"], self._payload(), "planner", "viewer")
        bad = self._payload(); bad["category"] = "unknown"
        with self.assertRaises(ValidationError):
            self.service.add_task(self.item["id"], bad, "planner", "operations")
        bad = self._payload(); bad["due_at"] = "not-a-date"
        with self.assertRaises(ValidationError):
            self.service.add_task(self.item["id"], bad, "planner", "operations")
        bad = self._payload(); bad["responsible_unit"] = "  "
        with self.assertRaises(ValidationError):
            self.service.add_task(self.item["id"], bad, "planner", "operations")

    def test_status_flow_and_audit(self):
        task = self.service.add_task(self.item["id"], self._payload(),
                                     "planner", "operations")
        task = self.service.update_task_status(
            self.item["id"], task["id"], {"status": "in_progress"}, "ops", "operations")
        self.assertEqual(task["status"], "in_progress")
        task = self.service.update_task_status(
            self.item["id"], task["id"], {"status": "completed"}, "ops",
            "response_commander")
        self.assertEqual(task["status"], "completed")
        with self.assertRaises(ConflictError):
            self.service.update_task_status(
                self.item["id"], task["id"], {"status": "open"}, "ops", "operations")
        actions = [e["action"] for e in self.service.audit("viewer", self.item["id"])]
        self.assertIn("task_create", actions)
        self.assertEqual(actions.count("task_status"), 2)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_close_blocked_until_recovery_done_and_shoreline_closed(self):
        task = self.service.add_task(self.item["id"], self._payload("recovery", "T-1"),
                                     "planner", "operations")
        self.service.add_record(
            self.item["id"],
            {"kind": "shoreline_monitoring", "detail": "岸线监测中", "status": "open"},
            "recorder", "response_commander")
        current = self._walk_to_monitoring()
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], "closed", current["version"],
                                  "reviewer", TRANSITION_ROLES["closed"][0])
        self.assertIn("回收任务未完成", str(ctx.exception))
        self.assertIn("岸线监测记录未关闭", str(ctx.exception))
        self.service.update_task_status(
            self.item["id"], task["id"], {"status": "completed"}, "ops", "operations")
        current = self.service.get_item(self.item["id"], "viewer")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], "closed", current["version"],
                                  "reviewer", TRANSITION_ROLES["closed"][0])
        self.assertNotIn("回收任务未完成", str(ctx.exception))
        self.assertIn("岸线监测记录未关闭", str(ctx.exception))

    def test_close_succeeds_when_recovery_done_and_shoreline_closed(self):
        task = self.service.add_task(self.item["id"], self._payload("recovery", "T-1"),
                                     "planner", "operations")
        self.service.add_record(
            self.item["id"],
            {"kind": "shoreline_monitoring", "detail": "岸线监测完成", "status": "closed"},
            "recorder", "response_commander")
        self.service.update_task_status(
            self.item["id"], task["id"], {"status": "completed"}, "ops", "operations")
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"], "closed")

    def test_overdue_tasks_raise_priority_and_show_in_list(self):
        def make_item(ref):
            return self.service.create_item(
                {"title": ref, "description": "overdue compare", "severity": "minor",
                 "quantity": 1, "threshold": 10, "external_ref": ref},
                "creator", "observer")
        overdue_item = make_item("OD-1")
        normal_item = make_item("OD-2")
        self.service.add_task(overdue_item["id"], self._payload("containment", "T-1", -1),
                              "planner", "operations")
        self.service.add_task(normal_item["id"], self._payload("containment", "T-2", 48),
                              "planner", "operations")
        items = {i["id"]: i for i in self.service.list_items("viewer")}
        self.assertEqual(items[overdue_item["id"]]["overdue_tasks"], 1)
        self.assertTrue(items[overdue_item["id"]]["has_overdue_tasks"])
        self.assertEqual(items[normal_item["id"]]["overdue_tasks"], 0)
        self.assertFalse(items[normal_item["id"]]["has_overdue_tasks"])
        self.assertGreater(items[overdue_item["id"]]["priority"],
                           items[normal_item["id"]]["priority"])
        overdue_only = self.service.list_items("viewer", overdue_only=True)
        self.assertIn(overdue_item["id"], [i["id"] for i in overdue_only])
        self.assertNotIn(normal_item["id"], [i["id"] for i in overdue_only])
        tasks = self.service.list_tasks(overdue_item["id"], "viewer")
        self.assertTrue(tasks[0]["overdue"])
        tasks = self.service.list_tasks(normal_item["id"], "viewer")
        self.assertFalse(tasks[0]["overdue"])


if __name__ == "__main__":
    unittest.main()
