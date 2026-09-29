"""证券结算与企业行动处理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "captured"
CREATE_ROLES = {'trader'}
ACTION_ROLES = {'apply_corporate': {'corporate_actions'}, 'adjust_ratio': {'corporate_actions'}, 'approve': {'settlement_officer'}, 'settle': {'settlement_officer'}, 'fail': {'settlement_officer'}, 'reverse': {'settlement_officer'}}
TRANSITIONS = {'apply_corporate': {'captured': 'adjusted'}, 'approve': {'captured': 'approved', 'adjusted': 'approved'}, 'settle': {'approved': 'settled'}, 'fail': {'approved': 'failed'}, 'reverse': {'settled': 'reversed', 'failed': 'reversed'}}
# 已交收、已冲正的指令冻结交收时的原比例，不再参与公告比例重算
FROZEN_STATES = {'settled', 'reversed'}
UNSETTLED_STATES = {'captured', 'adjusted', 'approved', 'failed'}


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
        # 公司行动公告编号：同一公告下的指令随公告比例一起重算，旧指令可缺省
        p["announcement_ref"] = optional_text(p, "announcement_ref")
        return p

    @staticmethod
    def ratio_effects(action: str, quantity: float, price: float, ratio: float) -> Dict[str, float]:
        """按给定比例计算公司行动影响的派生参数。"""
        effects: Dict[str, float] = {}
        ratio = float(ratio)
        if action == "split":
            effects["adjusted_quantity"] = int(float(quantity) * ratio)
            effects["adjusted_price"] = round(float(price) / ratio, 4)
        elif action == "dividend":
            effects["cash_entitlement"] = round(float(quantity) * ratio, 2)
        return effects

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        gross = float(p["quantity"]) * float(p["price"])
        fee = float(p["fees"])
        p["gross_amount"] = round(gross, 2)
        p["net_amount"] = round(gross + fee if p["side"] == "buy" else gross - fee, 2)
        p["adjusted_quantity"] = p["quantity"]
        p["adjusted_price"] = p["price"]
        # 比例随指令入库：首次计算结果落库，之后由公告调整统一重算
        p.update(self.ratio_effects(p["corporate_action"], p["quantity"], p["price"], p["action_ratio"]))
        return p

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
            # 交收时冻结当时比例：冲正必须按原比例追回，后续公告调整不得改写
            changes["settled_action_ratio"] = float(p["action_ratio"])
            if "cash_entitlement" in p:
                changes["settled_cash_entitlement"] = float(p["cash_entitlement"])
            summary = "交收完成"
        elif action == "fail":
            changes["fail_reason"] = text(data, "fail_reason")
            summary = "交收失败"
        elif action == "reverse":
            changes["reverse_reason"] = text(data, "reverse_reason")
            # 按交收时冻结的原比例追回现金差额；升级前的旧指令缺少冻结字段时按入库参数兜底
            original_ratio = p.get("settled_action_ratio")
            if original_ratio is None:
                original_ratio = p.get("action_ratio", 0)
            original_entitlement = p.get("settled_cash_entitlement")
            if original_entitlement is None and p.get("corporate_action") == "dividend":
                original_entitlement = round(float(p["quantity"]) * float(original_ratio), 2)
            changes["recouped_cash"] = round(float(original_entitlement or 0), 2)
            changes["reversed_action_ratio"] = round(float(original_ratio), 6)
            summary = "交收冲正，按原比例%s追回现金差额%s" % (
                format(float(original_ratio), "g"),
                format(changes["recouped_cash"], "g"),
            )
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    def validate_ratio(self, data: Dict[str, Any]) -> float:
        return number(data or {}, "ratio", 0.01)

    def is_unsettled(self, record: Dict[str, Any]) -> bool:
        return record["state"] in UNSETTLED_STATES

    def build_adjustment(self, record: Dict[str, Any], ratio: float) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """未交收指令按公告新比例重算；已交收/已冲正指令保留原比例（调用方不应调用）。"""
        p = dict(record["payload"])
        changes: Dict[str, Any] = {"action_ratio": round(float(ratio), 6)}
        derived = self.ratio_effects(p.get("corporate_action", "none"), p["quantity"], p["price"], ratio)
        changes.update(derived)
        # 公司行动已经应用的指令，有效数量/价格也要同步到新比例
        if p.get("corporate_applied"):
            if "adjusted_quantity" in changes:
                changes["effective_quantity"] = changes["adjusted_quantity"]
            if "adjusted_price" in changes:
                changes["effective_price"] = changes["adjusted_price"]
        p.update(changes)
        return p, changes
