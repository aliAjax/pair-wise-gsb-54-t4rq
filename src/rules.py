"""跨海光缆故障与抢修协调领域规则与状态转换。"""
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Conflict, ValidationError, boolean, integer, number, text


INITIAL_STATE = "detected"
RECTIFYING_STATE = "rectifying"
CREATE_ROLES = {'noc_operator'}
ACTION_ROLES = {'approve': {'repair_manager'}, 'mobilize': {'vessel_master'}, 'survey': {'cable_engineer'}, 'splice': {'cable_engineer'}, 'rectify': {'cable_engineer'}, 'backfill': {'cable_engineer'}, 'test': {'noc_operator'}, 'restore': {'noc_operator', 'repair_manager'}, 'cancel': {'repair_manager'}}
TRANSITIONS = {'approve': {'detected': 'approved'}, 'mobilize': {'approved': 'mobilized'}, 'survey': {'mobilized': 'surveyed'}, 'splice': {'surveyed': 'spliced'}, 'rectify': {RECTIFYING_STATE: 'spliced'}, 'backfill': {'spliced': 'spliced', 'tested': 'tested', RECTIFYING_STATE: RECTIFYING_STATE}, 'test': {'spliced': 'tested'}, 'restore': {'tested': 'restored'}, 'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled', RECTIFYING_STATE: 'cancelled'}}

# 接续后的后续动作：旧单未补齐历史接续项前一律冻结
HISTORY_GATED_ACTIONS = {'test', 'restore'}
# 允许接收晚到现场接续数据的状态
FIELD_SPLICE_STATES = {'spliced', 'tested', RECTIFYING_STATE, 'restored'}
# 晚到数据可以推翻测试结论的状态（已恢复的故障单不再回退）
REGRESSIBLE_STATES = {'spliced', 'tested'}
DEFAULT_LOSS_BUDGET_DB = 0.5
SPLICE_LOSS_MAX_DB = 2.0
FIELD_DATA_ACTIONS = {'cable_engineer', 'repair_manager'}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    RECTIFYING_STATE = RECTIFYING_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_field_data(self, role: str) -> bool:
        return role == "admin" or role in FIELD_DATA_ACTIONS

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "cable")
        text(p, "segment")
        start = number(p, "start_km", 0)
        end = number(p, "end_km", 0)
        number(p, "depth_m", 1)
        integer(p, "sea_state", 0, 9)
        boolean(p, "vessel_available")
        number(p, "spare_length_km", 0)
        boolean(p, "permit_valid")
        integer(p, "capacity_gbps", 1)
        if end <= start:
            raise ValidationError("结束里程必须大于开始里程")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        distance = float(p["end_km"]) - float(p["start_km"])
        p["repair_distance_km"] = round(distance, 2)
        p["required_spare_km"] = round(distance * 1.05, 2)
        p["estimated_repair_hours"] = round(distance / 2.0 + float(p["depth_m"]) / 100.0 + int(p["sea_state"]) * 2.0, 2)
        p["repair_feasible"] = bool(p["vessel_available"] and p["permit_valid"] and p["spare_length_km"] >= p["required_spare_km"] and int(p["sea_state"]) <= 5)
        # 区段接续档案预算：同一光缆区段多轮抢修的接续损耗累计上限
        p["splice_loss_budget_db"] = round(number(p, "splice_loss_budget_db", 0.01, 5.0), 4) if "splice_loss_budget_db" in p else DEFAULT_LOSS_BUDGET_DB
        # 旧单在档案建立前遗留、尚未补登的接续点数量
        legacy = integer(p, "legacy_unrecorded_splices", 0, 50) if "legacy_unrecorded_splices" in p else 0
        p["legacy_unrecorded_splices"] = legacy
        p["history_items_recorded"] = 0
        p["history_complete"] = legacy == 0
        p["cumulative_loss_db"] = 0.0
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"restored", "cancelled"} or item["payload"].get("cable") != payload.get("cable") or item["payload"].get("segment") != payload.get("segment"):
                continue
            if float(payload["start_km"]) < float(item["payload"].get("end_km", 0)) and float(payload["end_km"]) > float(item["payload"].get("start_km", 0)):
                raise Conflict("同一光缆区段已有未结束抢修")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    @staticmethod
    def parse_occurred_at(value: Any) -> str:
        """现场发生时刻：必须是可解析的ISO时间且不晚于当前时间，统一归一到UTC存储。"""
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("occurred_at必须是ISO时间文本")
        try:
            moment = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValidationError("occurred_at必须是ISO时间") from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
        if moment > datetime.now(timezone.utc):
            raise ValidationError("现场发生时刻不能晚于当前时间")
        return moment.isoformat()

    def validate_splice_fields(self, data: Dict[str, Any], payload: Dict[str, Any], *, require_spare: bool) -> Dict[str, Any]:
        """校验一次现场接续（正式动作或晚到现场数据共用）。"""
        fields = {
            "submission_id": text(data, "submission_id"),
            "splice_point_km": round(number(data, "splice_point_km", float(payload["start_km"]), float(payload["end_km"])), 6),
            "splice_loss_db": round(number(data, "splice_loss_db", 0.0, SPLICE_LOSS_MAX_DB), 4),
            "occurred_at": self.parse_occurred_at(data.get("occurred_at")),
        }
        if require_spare:
            used = number(data, "spare_used_km", 0)
            if used < float(payload["repair_distance_km"]):
                raise ValidationError("备缆使用长度不足")
            fields["spare_used_km"] = used
        return fields

    def validate_backfill_items(self, data: Dict[str, Any], payload: Dict[str, Any], remaining: int) -> List[Dict[str, Any]]:
        raw = data.get("items")
        if not isinstance(raw, list) or not raw:
            raise ValidationError("items必须是非空的历史接续项列表")
        items: List[Dict[str, Any]] = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise ValidationError("第%s项必须是对象" % (index + 1))
            items.append({
                "submission_id": text(entry, "submission_id"),
                "splice_point_km": round(number(entry, "splice_point_km", float(payload["start_km"]), float(payload["end_km"])), 6),
                "splice_loss_db": round(number(entry, "splice_loss_db", 0.0, SPLICE_LOSS_MAX_DB), 4),
                "occurred_at": self.parse_occurred_at(entry.get("occurred_at")),
            })
        return items

    @staticmethod
    def history_remaining(record: Dict[str, Any]) -> int:
        p = record["payload"]
        required = int(p.get("legacy_unrecorded_splices", 0))
        recorded = int(p.get("history_items_recorded", 0))
        return max(0, required - recorded)

    def ensure_history_complete(self, record: Dict[str, Any]) -> None:
        if self.history_remaining(record) > 0:
            raise Conflict("旧单缺少接续记录，请先执行backfill补历史项，补齐前不能继续后续动作")

    def restore_blockers(self, record: Dict[str, Any], archive: Dict[str, Any]) -> List[str]:
        """恢复流量前按最新区段接续档案复核，返回阻塞原因。"""
        blockers: List[str] = []
        if self.history_remaining(record) > 0:
            blockers.append("旧单缺少接续记录，请先补历史项")
        if int(archive.get("pending_count", 0)) > 0:
            blockers.append("存在%s条待确认现场接续数据" % archive["pending_count"])
        budget = float(record["payload"].get("splice_loss_budget_db", DEFAULT_LOSS_BUDGET_DB))
        if round(float(archive.get("cumulative_loss_db", 0.0)), 6) > budget + 1e-9:
            blockers.append("累计接续损耗%sdB超过预算%sdB，退回待整改" % (archive["cumulative_loss_db"], budget))
        confirmed = int(archive.get("confirmed_count", 0))
        if confirmed == 0:
            blockers.append("区段接续档案中没有已确认接续记录")
        return blockers

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "approve":
            if not bool(p["permit_valid"]) or not bool(p["vessel_available"]):
                raise ValidationError("许可或船舶条件不满足")
            changes["repair_manager"] = text(data, "repair_manager")
            summary = "抢修方案已批准"
        elif action == "mobilize":
            if float(data.get("weather_window_hours", 0)) < float(p["estimated_repair_hours"]):
                raise ValidationError("海况窗口不足以完成抢修")
            if float(data.get("available_spare_km", 0)) < float(p["required_spare_km"]):
                raise ValidationError("船上备缆不足")
            changes["weather_window_hours"] = float(data["weather_window_hours"])
            changes["vessel_name"] = text(data, "vessel_name")
            summary = "抢修船已动员"
        elif action == "survey":
            if not boolean(data, "survey_complete"):
                raise ValidationError("勘察尚未完成")
            fault_km = number(data, "fault_location_km", 0)
            if not (float(p["start_km"]) <= fault_km <= float(p["end_km"])):
                raise ValidationError("故障点不在申报区段")
            changes["fault_location_km"] = fault_km
            summary = "故障点勘察完成"
        elif action == "test":
            end_loss = number(data, "end_to_end_loss_db", 0)
            if end_loss > 0.5:
                raise ValidationError("端到端损耗不合格")
            changes["end_to_end_loss_db"] = end_loss
            changes["test_passed"] = True
            summary = "系统测试通过"
        elif action == "restore":
            if not boolean(data, "traffic_restored"):
                raise ValidationError("业务流量尚未恢复")
            changes["traffic_restored"] = True
            changes["restore_capacity_gbps"] = integer(data, "restore_capacity_gbps", 1)
            summary = "通信恢复"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "抢修取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
