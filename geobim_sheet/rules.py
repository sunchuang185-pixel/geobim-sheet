"""Configurable utility clearance rules for industrial and mining site reviews."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClearanceRule:
    layer: str
    label_zh: str
    min_horizontal_m: float
    min_vertical_m: float
    rule_code: str
    guidance: str


# Default mining / industrial plant site pipeline & slope clearance baseline
# (Customizable per company/project; values represent conservative pre-review thresholds in metres)
DEFAULT_CLEARANCE_RULES: dict[str, ClearanceRule] = {
    "water": ClearanceRule(
        layer="water",
        label_zh="生产/生活给水管线",
        min_horizontal_m=3.0,
        min_vertical_m=0.5,
        rule_code="MINE-CLR-WATER-01",
        guidance="建构筑物基础边缘与给水管线最小水平参考净距 ≥ 3.0 m，交叉垂直净距 ≥ 0.5 m。",
    ),
    "slurry": ClearanceRule(
        layer="slurry",
        label_zh="矿浆/尾矿输送管线",
        min_horizontal_m=5.0,
        min_vertical_m=0.8,
        rule_code="MINE-CLR-SLURRY-01",
        guidance="高压矿浆/尾矿管线与建构筑物最小水平参考净距 ≥ 5.0 m，需预留检修与防泄漏通道。",
    ),
    "power": ClearanceRule(
        layer="power",
        label_zh="电力电缆沟/供电线路",
        min_horizontal_m=2.5,
        min_vertical_m=0.5,
        rule_code="MINE-CLR-POWER-01",
        guidance="电缆沟/供电线路与构筑物基础最小水平参考净距 ≥ 2.5 m，避免基础开挖损伤电缆。",
    ),
    "drainage": ClearanceRule(
        layer="drainage",
        label_zh="截洪沟/生产排水管线",
        min_horizontal_m=3.0,
        min_vertical_m=0.5,
        rule_code="MINE-CLR-DRAIN-01",
        guidance="排水管/截洪沟与建构筑物最小水平参考净距 ≥ 3.0 m，防止基础浸水与边坡冲刷。",
    ),
    "slope": ClearanceRule(
        layer="slope",
        label_zh="工业场地台阶边坡线",
        min_horizontal_m=4.0,
        min_vertical_m=0.0,
        rule_code="MINE-CLR-SLOPE-01",
        guidance="建构筑物基础边缘距山地台阶坡顶/坡脚最小安全退距 ≥ 4.0 m，需复核边坡稳定性。",
    ),
}

UTILITY_REVIEW_LAYERS = ("water", "slurry", "power", "drainage", "slope")


def evaluate_clearance(
    *,
    layer: str,
    distance_rev_a_m: float,
    distance_rev_b_m: float,
    intersects_rev_b: bool,
    elevation_missing: bool,
    buffer_m: float,
) -> dict:
    """Evaluate planar clearance against mining/industrial ruleset without fabricating 3D depth."""
    rule = DEFAULT_CLEARANCE_RULES.get(layer)
    min_h = rule.min_horizontal_m if rule else 3.0
    min_v = rule.min_vertical_m if rule else 0.5
    rule_code = rule.rule_code if rule else "SITE-CLR-GEN-01"
    delta = distance_rev_b_m - distance_rev_a_m

    if intersects_rev_b or distance_rev_b_m <= 0.01:
        compliance = "violation"
        severity = "critical"
        reason = f"新版设备轮廓与{rule.label_zh if rule else layer}平面交叠/压占（违反 {rule_code}，最小净距要求 {min_h:g} m），必须复核"
    elif distance_rev_b_m < min_h:
        compliance = "violation"
        severity = "high"
        reason = (
            f"新版平面净距 {distance_rev_b_m:.2f} m 小于最小参考净距 {min_h:g} m"
            f"（违反 {rule_code}，较旧版变化 {delta:+.2f} m），必须复核"
        )
    elif distance_rev_b_m <= min_h * 1.6 or (delta < -1.0 and distance_rev_b_m <= 12.0):
        compliance = "warning"
        severity = "high" if delta < -0.5 else "medium"
        reason = (
            f"设备换版移动后净距缩至 {distance_rev_b_m:.2f} m（变化 {delta:+.2f} m，接近 {min_h:g} m 阈值，{rule_code}），建议优先复核"
        )
    else:
        compliance = "compliant_in_buffer"
        severity = "medium"
        reason = f"位于 {buffer_m:g} m 复核范围内（新版净距 {distance_rev_b_m:.2f} m ≥ {min_h:g} m，{rule_code} 平面满足）"

    if elevation_missing and layer != "slope":
        reason += "；缺少埋深/高程，标记为“待核实高程”"

    return {
        "compliance_status": compliance,
        "severity": severity,
        "min_horizontal_clearance_m": min_h,
        "min_vertical_clearance_m": min_v,
        "rule_code": rule_code,
        "rule_guidance": rule.guidance if rule else "",
        "review_reason": reason,
    }
