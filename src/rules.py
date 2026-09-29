"""证券结算与企业行动处理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "captured"
CREATE_ROLES = {'trader'}
ACTION_ROLES = {'apply_corporate': {'corporate_actions'}, 'confirm_announcement': {'corporate_actions'}, 'approve': {'settlement_officer'}, 'settle': {'settlement_officer'}, 'fail': {'settlement_officer'}, 'reverse': {'corporate_actions', 'settlement_officer'}}
# 已终局的指令不再受公告比例修正影响
FROZEN_STATES = {'settled', 'reversed'}
ANNOUNCEMENT_ACTIONS = ['split', 'dividend', 'merger']
TRANSITIONS = {'apply_corporate': {'captured': 'adjusted'}, 'approve': {'captured': 'approved', 'adjusted': 'approved'}, 'settle': {'approved': 'settled'}, 'fail': {'approved': 'failed'}, 'reverse': {'settled': 'reversed', 'failed': 'reversed'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "instrument")
        choice(p, "side", ["buy", "sell"])
        integer(p, "quantity", 1)
        number(p, "price", 0.01)
        number(p, "fees", 0)
        choice(p, "currency", ["CNY", "USD", "HKD"])
        integer(p, "settlement_day", 0)
        choice(p, "corporate_action", ["none", "split", "dividend", "merger"])
        number(p, "action_ratio", 0.01)
        p["announcement_id"] = optional_text(p, "announcement_id")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        gross = float(p["quantity"]) * float(p["price"])
        fee = float(p["fees"])
        p["gross_amount"] = round(gross, 2)
        p["net_amount"] = round(gross + fee if p["side"] == "buy" else gross - fee, 2)
        # 公告到账前以入库时的临时比例占位，原比例留档供冲正追回
        p["original_action_ratio"] = float(p["action_ratio"])
        p["adjusted_quantity"] = p["quantity"]
        p["adjusted_price"] = p["price"]
        if p["corporate_action"] == "split":
            p["adjusted_quantity"] = int(float(p["quantity"]) * float(p["action_ratio"]))
            p["adjusted_price"] = round(float(p["price"]) / float(p["action_ratio"]), 4)
        elif p["corporate_action"] == "dividend":
            p["cash_entitlement"] = round(float(p["quantity"]) * float(p["action_ratio"]), 2)
        return p

    def validate_announcement(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        text(p, "instrument")
        choice(p, "corporate_action", ANNOUNCEMENT_ACTIONS)
        number(p, "action_ratio", 0.01)
        return p

    @staticmethod
    def same_ratio(left: Any, right: Any) -> bool:
        if left is None or right is None:
            return False
        return abs(float(left) - float(right)) < 1e-9

    @staticmethod
    def recompute(payload: Dict[str, Any], ratio: float) -> Dict[str, Any]:
        """按新比例重算单条未交收指令；已应用公司行动的指令同步刷新生效值。"""
        p = dict(payload)
        p["action_ratio"] = float(ratio)
        if p["corporate_action"] == "split":
            p["adjusted_quantity"] = int(float(p["quantity"]) * float(ratio))
            p["adjusted_price"] = round(float(p["price"]) / float(ratio), 4)
            if p.get("corporate_applied"):
                p["effective_quantity"] = p["adjusted_quantity"]
                p["effective_price"] = p["adjusted_price"]
        elif p["corporate_action"] == "dividend":
            p["cash_entitlement"] = round(float(p["quantity"]) * float(ratio), 2)
            if p.get("corporate_applied"):
                p["effective_cash_entitlement"] = p["cash_entitlement"]
        return p

    def transform_for_announcement(self, record: Dict[str, Any], instrument: str, action: str, ratio: float) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """判断一条指令是否被公告覆盖并产出新payload；无需调整时返回(None, {})。"""
        p = record["payload"]
        if p.get("corporate_action") != action or p.get("instrument") != instrument:
            return None, {}
        if record["state"] in FROZEN_STATES:
            return None, {}
        if self.same_ratio(p.get("action_ratio"), ratio):
            return None, {}
        old_ratio = p.get("original_action_ratio", p.get("action_ratio", ratio))
        changes = {"old_ratio": old_ratio, "new_ratio": float(ratio), "state": record["state"]}
        return self.recompute(p, ratio), changes

    @staticmethod
    def frozen_ratio(payload: Dict[str, Any]) -> float:
        """冲正读取交收时冻结的比例；升级前的旧指令退回入库原比例。"""
        if payload.get("settled_action_ratio") is not None:
            return float(payload["settled_action_ratio"])
        if payload.get("original_action_ratio") is not None:
            return float(payload["original_action_ratio"])
        return float(payload.get("action_ratio", 0))

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"settled", "reversed"} and item["payload"].get("instrument") == payload.get("instrument") and item["payload"].get("settlement_day") == payload.get("settlement_day"):
                if item["payload"].get("side") == payload.get("side") and item["payload"].get("quantity") == payload.get("quantity") and item["payload"].get("price") == payload.get("price"):
                    raise Conflict("疑似重复结算指令")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "apply_corporate":
            if p["corporate_action"] == "none":
                raise ValidationError("没有待处理的公司行动")
            changes["corporate_applied"] = True
            changes["effective_quantity"] = p["adjusted_quantity"]
            changes["effective_price"] = p["adjusted_price"]
            if p["corporate_action"] == "dividend":
                changes["effective_cash_entitlement"] = p["cash_entitlement"]
            summary = "公司行动已应用"
        elif action == "approve":
            changes["approved_amount"] = p["net_amount"]
            summary = "结算指令复核通过"
        elif action == "settle":
            delivered = integer(data, "delivered_quantity", 0)
            paid = number(data, "cash_paid", 0)
            required_quantity = int(p.get("effective_quantity", p["quantity"]))
            if delivered != required_quantity:
                raise ValidationError("交收证券数量不匹配")
            if paid < float(p["net_amount"]):
                raise ValidationError("交收资金不足")
            changes["delivered_quantity"] = delivered
            changes["cash_paid"] = paid
            # 交收即冻结当时比例，之后公告再修正也不影响本指令
            changes["settled_action_ratio"] = float(p.get("action_ratio"))
            summary = "交收完成"
        elif action == "fail":
            changes["fail_reason"] = text(data, "fail_reason")
            summary = "交收失败"
        elif action == "reverse":
            changes["reverse_reason"] = text(data, "reverse_reason")
            # 按交收时冻结的原比例追回现金差额，不读取公告修正后的最新比例
            frozen_ratio = self.frozen_ratio(p)
            changes["reversed_action_ratio"] = frozen_ratio
            if p.get("corporate_action") == "dividend" and record["state"] == "settled":
                changes["cash_recovered"] = round(float(p["quantity"]) * frozen_ratio, 2)
            else:
                changes["cash_recovered"] = 0.0
            summary = "交收冲正，按原比例%s追回现金差额%s" % (frozen_ratio, changes["cash_recovered"])
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
