"""
Strategy catalog for Gold Genious.

Only ``10_PIPS`` is live. The rest are named toggles reserved for later.
"""

# (id, button label, implemented?)
STRATEGIES = [
    ("10_PIPS", "10 PIPS", True),
    ("EMA_15", "EMA 15", False),
    ("OB_SR", "OB & SR", False),
    ("BOS_EMA", "BOS_EMA", False),
    ("SONAR_LAB", "SONAR LAB", False),
    ("PRICE_ACTION", "PRICE ACTION", False),
    ("SMART_MONEY", "SMART MONEY", False),
    ("ORDER_FLOW", "ORDER FLOW", False),
    ("FIB_RETRACE", "FIB RETRACE", False),
    ("ASIAN_RANGE", "ASIAN RANGE", False),
]


def strategy_ids() -> list[str]:
    return [s[0] for s in STRATEGIES]


def label_for(sid: str) -> str:
    for i, label, _ in STRATEGIES:
        if i == sid:
            return label
    return sid


def is_implemented(sid: str) -> bool:
    for i, _, ok in STRATEGIES:
        if i == sid:
            return ok
    return False
