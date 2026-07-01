from __future__ import annotations

from agents.schemas.brief import UserBuildBrief
from agents.schemas.build_card import BuildCard
from agents.schemas.feasibility import FeasibilityVerdict
from agents.schemas.price_bands import PriceBands

_SEP = "=" * 80
_BAND_COL = (14, 14, 14, 14)


def _inr(amount: int) -> str:
    return f"INR {amount:,}"


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _split_brand_model(name: str) -> tuple[str, str]:
    parts = name.split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")


def format_price_bands(price_bands: PriceBands) -> str:
    """Clean price-bands table; matches _print_price_bands in run_pipeline.py."""
    col = _BAND_COL
    rule = "  " + "-" * sum(col)
    lines: list[str] = [
        f"  {'Component':<{col[0]}}{'Low':>{col[1]}}{'Mid':>{col[2]}}{'High':>{col[3]}}",
        rule,
    ]
    for slot, band in price_bands.root.items():
        name = slot.value.capitalize()
        lines.append(
            f"  {name:<{col[0]}}{_inr(band.low):>{col[1]}}"
            f"{_inr(band.mid):>{col[2]}}{_inr(band.high):>{col[3]}}"
        )
    lines.append(rule)
    lines.append(
        f"  {'TOTAL':<{col[0]}}{_inr(price_bands.total_low()):>{col[1]}}"
        f"{_inr(price_bands.total_mid()):>{col[2]}}{_inr(price_bands.total_high()):>{col[3]}}"
    )
    return "\n".join(lines)


def format_build_card(build_card: BuildCard, brief: UserBuildBrief) -> str:
    """Full multi-section build card for CLI display."""
    C_COMP  = 14
    C_BRAND = 12
    C_MODEL = 22
    C_PRICE = 14
    C_JUST  = 32

    rule = "  " + "-" * (C_COMP + C_BRAND + C_MODEL + C_PRICE + 2 + C_JUST)

    use_case = brief.purpose.primary_use_case.replace("_", " ").title()
    sub = f" / {brief.purpose.sub_case}" if brief.purpose.sub_case else ""

    lines: list[str] = [
        _SEP,
        "  YOUR PC BUILD",
        f"  Use case  : {use_case}{sub}",
        f"  Budget    : {_inr(brief.budget.comfortable_max)} comfortable  /  "
        f"{_inr(brief.budget.ceiling)} ceiling",
        _SEP,
        "",
        f"  {'Component':<{C_COMP}}{'Brand':<{C_BRAND}}{'Model':<{C_MODEL}}"
        f"{'Price (INR)':>{C_PRICE}}  Justification",
        rule,
    ]

    for part in build_card.parts:
        brand, model = _split_brand_model(part.name)
        lines.append(
            f"  {part.slot.value.capitalize():<{C_COMP}}"
            f"{_truncate(brand, C_BRAND):<{C_BRAND}}"
            f"{_truncate(model, C_MODEL):<{C_MODEL}}"
            f"{_inr(part.price_inr):>{C_PRICE}}  "
            f"{_truncate(part.justification, C_JUST)}"
        )

    total_label_width = C_COMP + C_BRAND + C_MODEL
    lines += [
        rule,
        f"  {'TOTAL':<{total_label_width}}{_inr(build_card.total_price_inr):>{C_PRICE}}",
        "",
        "  Budget summary:",
        f"    Build total     : {_inr(build_card.total_price_inr)}",
        f"    Comfortable max : {_inr(brief.budget.comfortable_max)}",
        f"    Ceiling         : {_inr(brief.budget.ceiling)}",
    ]

    diff = build_card.total_price_inr - brief.budget.comfortable_max
    if diff <= 0:
        lines.append(f"    Headroom        : {_inr(-diff)} under comfortable max")
    else:
        lines.append(f"    Over budget by  : {_inr(diff)} above comfortable max")

    constraints = brief.hard_constraints
    if constraints.must_have or constraints.must_not:
        lines.append("")
        lines.append("  Hard constraints:")
        part_names_lower = [p.name.lower() for p in build_card.parts]
        for c in constraints.must_have:
            met = any(c.value.lower() in n for n in part_names_lower)
            lines.append(f"    {'[OK]' if met else '[?] '} MUST HAVE : {c.value}")
        for c in constraints.must_not:
            violated = any(c.value.lower() in n for n in part_names_lower)
            lines.append(f"    {'[!!]' if violated else '[OK]'} MUST NOT  : {c.value}")

    if build_card.warnings:
        lines.append("")
        lines.append("  Unresolved (needs your attention):")
        for w in build_card.warnings:
            lines.append(f"    - {w}")

    lines += [
        "",
        _SEP,
        "  Confirm this build? (yes/no)",
        _SEP,
    ]
    return "\n".join(lines)


def format_impossible(verdict: FeasibilityVerdict) -> str:
    """Explain why the build is impossible and list suggested adjustments."""
    lines: list[str] = [
        _SEP,
        "  BUILD IMPOSSIBLE",
        _SEP,
        "",
        f"  {verdict.reason}",
    ]
    if verdict.binding_constraint:
        lines += ["", f"  Binding constraint: {verdict.binding_constraint}"]
    if verdict.suggested_adjustments:
        lines += ["", "  Suggested adjustments:"]
        for i, adj in enumerate(verdict.suggested_adjustments, 1):
            lines.append(f"    {i}. {adj}")
    lines += ["", _SEP]
    return "\n".join(lines)


def format_tight_warning(verdict: FeasibilityVerdict) -> str:
    """One-paragraph warning shown before allocation when budget is tight."""
    parts = ["Warning: your budget is tight for this build.", verdict.reason]
    if verdict.binding_constraint:
        parts.append(f"The tightest constraint is: {verdict.binding_constraint}.")
    parts.append("Proceeding to allocation — expect some compromises on component tiers.")
    return "  " + " ".join(parts)
