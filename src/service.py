"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, NotFound, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def list_announcements(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_announcements(limit=limit)

    def get_announcement(self, actor: Actor, reference: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        announcement = self.repository.get_announcement(text({"reference": reference}, "reference"))
        if announcement is None:
            raise NotFound("公司行动公告不存在")
        return announcement

    def adjust_announcement_ratio(self, actor: Actor, reference: str, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "adjust_ratio"):
            raise PermissionDenied("角色无权调整公司行动比例")
        reference = text({"reference": reference}, "reference")
        ratio = self.rules.validate_ratio(data or {})
        # 同一公告的重试必须携带同一request_id，保证不重复调整
        request_id = text(data or {}, "request_id")
        return self.repository.adjust_announcement(
            reference=reference,
            expected_version=int(expected_version),
            ratio=ratio,
            request_id=request_id,
            actor_id=actor.user_id,
            build_adjustment=self.rules.build_adjustment,
            is_unsettled=self.rules.is_unsettled,
        )

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
