from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='溢油应急响应与任务追踪'; ENTITY='溢油事件'; ID_PREFIX='OS'
SEVERITIES=['minor', 'moderate', 'major', 'catastrophic']; STATES=['reported', 'assessing', 'containing', 'recovering', 'monitoring', 'closed']; TRANSITIONS={'reported': ['assessing'], 'assessing': ['containing'], 'containing': ['recovering'], 'recovering': ['monitoring'], 'monitoring': ['closed'], 'closed': []}; TRANSITION_ROLES={'assessing': ['response_commander'], 'containing': ['response_commander'], 'recovering': ['operations'], 'monitoring': ['operations'], 'closed': ['response_commander']}
CREATE_ROLES=set(['observer', 'response_commander']); RECORD_ROLES=set(['response_commander', 'operations']); AUDIT_ROLES=set(['response_commander', 'viewer']); VIEW_ROLES=set(['observer', 'response_commander', 'operations', 'viewer'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'major': 6.0, 'catastrophic': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'major': 8, 'catastrophic': 4}; TERMINAL_STATES=set(['closed'])
TASK_CATEGORIES=['containment', 'recovery', 'shoreline_protection', 'waste_disposal']; TASK_STATUSES=['open', 'in_progress', 'completed']; TASK_TRANSITIONS={'open': ['in_progress', 'completed'], 'in_progress': ['completed'], 'completed': []}; TASK_ROLES=set(['response_commander', 'operations'])
SHORELINE_RECORD_KIND='shoreline_monitoring'; RECOVERY_CATEGORY='recovery'; OVERDUE_PRIORITY_BOOST=2.0
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0,overdue_tasks=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    score=SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))+min(OVERDUE_PRIORITY_BOOST,float(overdue_tasks))
    return max(0,min(10,int(round(score))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records,open_recovery_tasks=0,open_shoreline_records=0):
    if target not in TERMINAL_STATES: return []
    blockers=[]
    if open_records>0: blockers.append("仍有未关闭事项")
    if open_recovery_tasks>0: blockers.append("回收任务未完成")
    if open_shoreline_records>0: blockers.append("岸线监测记录未关闭")
    return blockers
def validate_task_transition(current,target):
    if current not in TASK_STATUSES or target not in TASK_STATUSES: raise ValidationError("未知任务状态")
    if target not in TASK_TRANSITIONS.get(current,[]): raise ConflictError(f"任务不能从{current}转换到{target}")
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
