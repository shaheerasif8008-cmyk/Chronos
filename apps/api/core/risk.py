from __future__ import annotations
"""
Risk Pricer — computes a 0..1 risk score for a tool call.

This replaces the implicit, per-tool reasoning scattered across the broker's
hard-coded safety dict with one explicit, inspectable model. The pricer does NOT
decide allow/deny — it only *prices* a call. The Autonomy Gate (core/autonomy.py)
turns a price into a decision against earned trust.

The hard safety limits in tool_broker (_check_safety_limits) remain the absolute
ceiling and run *before* the pricer. The pricer only governs the band beneath it.

Source of risk factors: a registry (RISK_REGISTRY) gives base blast-radius and
reversibility per provider/tool; data-class and magnitude are read from the args;
novelty is supplied by the trust ledger (defaults to "fully novel" when unknown).
"""
import re
from dataclasses import dataclass, field

# Factor weights — sum to 1.0 so risk stays in [0, 1].
_W_BLAST = 0.30
_W_IRREVERSIBILITY = 0.30
_W_DATA = 0.20
_W_MAGNITUDE = 0.10
_W_NOVELTY = 0.10

# Tier cut points.
TIER_LOW_MAX = 0.30
TIER_MEDIUM_MAX = 0.70

# Cold-start: action_classes safe enough to auto-execute on day one (no external
# effect, trivially reversible). They graduate "by seed" so a fresh org isn't all
# friction. Everything else must earn graduation. See chronos_graduated_autonomy.md.
SEEDED_AUTO_CLASSES: dict[str, float] = {
    "gmail.draft": 0.40,
    "doc.create": 0.40,
    "doc.create_slides": 0.40,
    "doc.render_chart": 0.40,
    "image.generate": 0.40,
}

# Per-provider base factors. (blast_radius, irreversibility) each in [0, 1].
# Defaults are deliberately cautious; admins can override via a registry table later.
_PROVIDER_BASE: dict[str, tuple[float, float]] = {
    "gmail": (0.6, 0.4),        # external comms
    "twitter": (1.0, 0.9),
    "linkedin": (1.0, 0.9),
    "website": (1.0, 0.8),
    "browser": (0.4, 0.3),
    "fs": (0.2, 0.5),
    "code": (0.3, 0.4),
    "doc": (0.1, 0.1),
    "image": (0.1, 0.1),
    "data": (0.2, 0.2),
    "chat_history": (0.1, 0.0),
    "computer": (0.8, 0.7),
    "local_computer": (0.9, 0.8),
    "desktop": (0.7, 0.6),
    "finance": (1.0, 1.0),
    "payment": (1.0, 1.0),
}

# Action verbs that lower/raise the base for a specific tool.
_READ_MARKERS = (".search", ".fetch", ".get", ".list", ".read", ".query", ".extract")
_DRAFT_MARKERS = (".draft", ".create", ".render")
_DESTRUCTIVE_MARKERS = (".delete", ".remove", ".publish", ".send", ".post", ".transfer")

# Cheap regulated-data detectors. Email addresses are intentionally excluded —
# they are ubiquitous (every recipient field has one) and not, on their own,
# regulated data worth gating. We flag genuinely sensitive identifiers.
_PII_PATTERNS = (
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),          # SSN-like
    re.compile(r"\b(?:\d[ -]?){13,16}\b"),         # card-like
)


@dataclass(frozen=True)
class RiskScore:
    value: float
    action_class: str
    tier: str
    factors: dict[str, float] = field(default_factory=dict)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def risk_tier(value: float) -> str:
    if value <= TIER_LOW_MAX:
        return "low"
    if value <= TIER_MEDIUM_MAX:
        return "medium"
    return "high"


def _base_factors(tool: str) -> tuple[float, float]:
    provider = tool.split(".", 1)[0]
    blast, irr = _PROVIDER_BASE.get(provider, (0.5, 0.5))
    if any(m in tool for m in _READ_MARKERS):
        blast *= 0.4
        irr *= 0.2
    elif any(m in tool for m in _DRAFT_MARKERS):
        blast *= 0.3          # a draft has no external effect until sent
        irr *= 0.2
    elif any(m in tool for m in _DESTRUCTIVE_MARKERS):
        blast = min(1.0, blast * 1.2)
        irr = min(1.0, irr * 1.2)
    return blast, irr


def _data_class(args: dict) -> float:
    """0 (public) .. 1 (regulated data present in payload)."""
    try:
        blob = repr(args)
    except Exception:
        return 0.0
    return 1.0 if any(p.search(blob) for p in _PII_PATTERNS) else 0.0


def _magnitude(tool: str, args: dict) -> tuple[float, str]:
    """Normalized magnitude in [0,1] plus a partition label for the action_class."""
    if tool == "gmail.send" or tool == "gmail.draft":
        to = args.get("to") or args.get("recipients") or []
        n = len(to) if isinstance(to, list) else 1
        return _clamp01(n / 10.0), ("bulk" if n > 1 else "single")
    if any(t in tool for t in ("finance.", "payment.")):
        amount = _safe_float(args.get("amount"))
        return _clamp01(amount / 100.0), ("over10" if amount > 10 else "small")
    if "delete" in tool:
        ids = args.get("ids", args.get("record_ids", []))
        n = args.get("count", len(ids) if isinstance(ids, list) else 1)
        return _clamp01(float(n) / 5.0), "default"
    return 0.0, "default"


def _safe_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def action_class(tool: str, args: dict) -> str:
    _, partition = _magnitude(tool, args)
    return tool if partition == "default" else f"{tool}:{partition}"


def price(tool: str, args: dict, *, novelty: float = 1.0) -> RiskScore:
    """Compute the risk score for a single tool call.

    ``novelty`` is supplied by the trust ledger: 1.0 means never-before-seen
    (cold start), 0.0 means well-established. Callers that don't have ledger
    context get the cautious default of fully novel.
    """
    blast, irr = _base_factors(tool)
    data = _data_class(args)
    magnitude, partition = _magnitude(tool, args)
    novelty = _clamp01(novelty)

    value = _clamp01(
        _W_BLAST * blast
        + _W_IRREVERSIBILITY * irr
        + _W_DATA * data
        + _W_MAGNITUDE * magnitude
        + _W_NOVELTY * novelty
    )
    klass = tool if partition == "default" else f"{tool}:{partition}"
    return RiskScore(
        value=value,
        action_class=klass,
        tier=risk_tier(value),
        factors={
            "blast_radius": round(blast, 3),
            "irreversibility": round(irr, 3),
            "data_class": round(data, 3),
            "magnitude": round(magnitude, 3),
            "novelty": round(novelty, 3),
        },
    )
