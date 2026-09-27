from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, normalize_task_category,
                     normalize_timestamp, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    TASK_ROLES, TASK_STATUSES, TERMINAL_STATES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


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
        counts = self.repository.item_counts(item_id)
        blockers = completion_blockers(
            target,
            counts["open_records"] - counts["open_shoreline_records"],
            counts["incomplete_recovery_tasks"],
            counts["open_shoreline_records"])
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def update_record_status(self, item_id: int, record_id: int,
                             payload: Dict[str, Any], actor: str,
                             role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] in TERMINAL_STATES:
            raise ConflictError("事件已关闭，无法变更记录")
        status = payload.get("status")
        if status not in ("open", "closed"):
            raise ValidationError("status必须是open或closed")
        record, previous = self.repository.update_record_status(
            item_id, record_id, status)
        self.repository.append_audit("record_status", ENTITY, item_id, actor, {
            "record_id": record_id, "kind": record["kind"],
            "from": previous, "to": status,
        })
        return record

    def create_task(self, item_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] in TERMINAL_STATES:
            raise ConflictError("事件已关闭，无法新增任务")
        category = normalize_task_category(payload.get("category"))
        responsible_unit = require_text(
            payload.get("responsible_unit"), "responsible_unit", 200)
        detail = payload.get("detail")
        detail = require_text(detail, "detail") if detail is not None else ""
        deadline = normalize_timestamp(payload.get("deadline"), "deadline")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        task = self.repository.create_task(item_id, category, responsible_unit,
                                           detail, deadline, external_ref, actor)
        self.repository.append_audit("task_create", ENTITY, item_id, actor, {
            "task_id": task["id"], "category": category,
            "responsible_unit": responsible_unit, "deadline": deadline,
        })
        return self.enrich_task(task)

    def update_task_status(self, item_id: int, task_id: int,
                           payload: Dict[str, Any], actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] in TERMINAL_STATES:
            raise ConflictError("事件已关闭，无法变更任务")
        status = payload.get("status")
        if status not in TASK_STATUSES:
            raise ValidationError("status必须是open或done")
        task, previous = self.repository.update_task_status(
            item_id, task_id, status)
        self.repository.append_audit("task_status", ENTITY, item_id, actor, {
            "task_id": task_id, "category": task["category"],
            "from": previous, "to": status,
        })
        return self.enrich_task(task)

    def list_tasks(self, item_id: int, role: str) -> list:
        self._view(role)
        return [self.enrich_task(task)
                for task in self.repository.list_tasks(item_id)]

    @staticmethod
    def enrich_task(task: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(task)
        result["overdue"] = (task["status"] != "done"
                             and task["deadline"] < utc_now())
        return result

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        counts = self.repository.item_counts(item["id"])
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"],
            counts["open_records"], counts["overdue_tasks"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["open_records"] = counts["open_records"]
        result["open_tasks"] = counts["open_tasks"]
        result["overdue_tasks"] = counts["overdue_tasks"]
        return result
