from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (NotFoundError, ValidationError, ensure_role,
                     normalize_severity, require_datetime, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    RECOVERY_CATEGORY, TASK_CATEGORIES, TASK_ROLES,
                    TASK_STATUSES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_task_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(
            target,
            self.repository.open_record_count(item_id),
            self.repository.open_task_count(item_id, RECOVERY_CATEGORY),
            self.repository.open_shoreline_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def add_task(self, item_id: int, payload: Dict[str, Any], actor: str,
                 role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_ROLES)
        actor = require_text(actor, "actor", 100)
        category = payload.get("category")
        if category not in TASK_CATEGORIES:
            raise ValidationError("category不在允许范围内")
        responsible_unit = require_text(payload.get("responsible_unit"),
                                        "responsible_unit", 200)
        due_at = require_datetime(payload.get("due_at"), "due_at")
        status = payload.get("status", "open")
        if status not in TASK_STATUSES:
            raise ValidationError("status不在允许范围内")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        task = self.repository.add_task(item_id, category, responsible_unit,
                                        due_at, status, external_ref, actor)
        self.repository.append_audit("task_create", ENTITY, item_id, actor, {
            "task_id": task["id"], "category": category,
            "responsible_unit": responsible_unit, "due_at": due_at,
            "status": status,
        })
        return task

    def update_task_status(self, item_id: int, task_id: int,
                           payload: Dict[str, Any], actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_ROLES)
        actor = require_text(actor, "actor", 100)
        target = payload.get("status")
        if target not in TASK_STATUSES:
            raise ValidationError("status不在允许范围内")
        task = self.repository.get_task(task_id)
        if task["item_id"] != item_id:
            raise NotFoundError("任务不存在")
        validate_task_transition(task["status"], target)
        updated = self.repository.update_task_status(item_id, task_id, target)
        self.repository.append_audit("task_status", ENTITY, item_id, actor, {
            "task_id": task_id, "category": task["category"],
            "from": task["status"], "to": target,
        })
        return updated

    def list_tasks(self, item_id: int, role: str) -> list:
        self._view(role)
        now = utc_now()
        tasks = self.repository.list_tasks(item_id)
        for task in tasks:
            task["overdue"] = task["status"] != "completed" and task["due_at"] < now
        return tasks

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None,
                   overdue_only: bool = False) -> list:
        self._view(role)
        items = [self.enrich(item) for item in self.repository.list_items(status)]
        if overdue_only:
            items = [item for item in items if item["has_overdue_tasks"]]
        return items

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        open_records = self.repository.open_record_count(item["id"])
        open_tasks = self.repository.open_task_count(item["id"])
        overdue_tasks = self.repository.overdue_task_count(item["id"], utc_now())
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"],
            open_records + open_tasks, overdue_tasks)
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["open_records"] = open_records
        result["open_tasks"] = open_tasks
        result["overdue_tasks"] = overdue_tasks
        result["has_overdue_tasks"] = overdue_tasks > 0
        return result
