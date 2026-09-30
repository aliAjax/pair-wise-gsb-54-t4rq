"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, choice, integer, text
from .repository import Repository
from .rules import (
    DEFAULT_LOSS_BUDGET_DB,
    FIELD_SPLICE_STATES,
    HISTORY_GATED_ACTIONS,
    DomainRules,
)


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

    @staticmethod
    def _budget(payload: Dict[str, Any]) -> float:
        return float(payload.get("splice_loss_budget_db", DEFAULT_LOSS_BUDGET_DB))

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
        data = data or {}
        record = self.repository.get(record_id)

        if action in ("splice", "rectify"):
            return self._splice(actor, record, expected_version, action, data)
        if action == "backfill":
            return self._backfill(actor, record, expected_version, data)

        self.rules.require_transition(record, action)

        # 恢复流量前按最新区段接续档案复核
        if action in HISTORY_GATED_ACTIONS:
            self.rules.ensure_history_complete(record)
        if action == "restore":
            record = self._review_before_restore(actor, record)

        new_state, new_payload, summary = self.rules.apply_action(record, action, data)
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data, "from": record["state"], "to": new_state},
        )

    def _splice(self, actor: Actor, record: Dict[str, Any], expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        fields = self.rules.validate_splice_fields(data, record["payload"], require_spare=(action == "splice"))
        replaces_entry_id = None
        if action == "rectify":
            replaces_entry_id = integer(data, "replaces_entry_id", 1)
        outcome = self.repository.commit_splice(
            record_id=record["id"],
            expected_version=int(expected_version),
            actor_id=actor.user_id,
            fields=fields,
            replaces_entry_id=replaces_entry_id,
            budget=self._budget(record["payload"]),
            action=action,
        )
        return outcome["record"]

    def _backfill(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        remaining = self.rules.history_remaining(record)
        items = self.rules.validate_backfill_items(data, record["payload"], remaining)
        outcome = self.repository.backfill_history(
            record_id=record["id"],
            expected_version=int(expected_version),
            actor_id=actor.user_id,
            items=items,
            budget=self._budget(record["payload"]),
        )
        return outcome["record"]

    def _review_before_restore(self, actor: Actor, record: Dict[str, Any]) -> Dict[str, Any]:
        outcome = self.repository.enforce_budget(record["id"], actor.user_id)
        record = outcome["record"]
        archive = self.repository.get_archive(record["payload"]["cable"], record["payload"]["segment"])
        blockers = self.rules.restore_blockers(record, archive)
        if blockers:
            # 若复核已把故障单退回待整改，刷新后的状态不再允许restore，错误信息说明原因
            raise Conflict("；".join(blockers))
        return record

    # ------------------------------------------------------------------
    # 现场接续数据（并行/晚到记录）
    # ------------------------------------------------------------------

    def submit_field_splice(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_field_data(actor.role):
            raise PermissionDenied("角色无权提交现场接续数据")
        record = self.repository.get(record_id)
        if record["state"] not in FIELD_SPLICE_STATES:
            raise Conflict("当前状态不接收现场接续数据")
        fields = self.rules.validate_splice_fields(data, record["payload"], require_spare=False)
        return self.repository.ingest_field_entry(record["id"], actor.user_id, fields, self._budget(record["payload"]))

    def resolve_field_entry(self, actor: Actor, entry_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_field_data(actor.role):
            raise PermissionDenied("角色无权处理现场接续数据")
        decision = choice(data or {}, "decision", ["confirm", "discard"])
        entry = self.repository.get_splice_entry(entry_id)
        budget = self._budget(self.repository.get(entry["record_id"])["payload"]) if entry["record_id"] else DEFAULT_LOSS_BUDGET_DB
        return self.repository.resolve_field_entry(entry_id, actor.user_id, decision, budget)

    def get_archive(self, actor: Actor, cable: str, segment: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        cable = text({"cable": cable}, "cable")
        segment = text({"segment": segment}, "segment")
        archive = self.repository.get_archive(cable, segment)
        if archive is None:
            from .domain import NotFound
            raise NotFound("区段接续档案不存在")
        archive["entries"] = self.repository.list_splice_entries(cable=cable, segment=segment, limit=500)
        return archive

    def list_splice_entries(self, actor: Actor, status: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if status and status not in {"confirmed", "pending", "superseded", "discarded"}:
            from .domain import ValidationError
            raise ValidationError("status不合法")
        return self.repository.list_splice_entries(status=status, limit=500)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
