"""Balatro Bot — rule-based bot that plays Balatro via the balatrobot mod's JSON-RPC API."""


def __getattr__(name: str):
    if name == "RuleEngine":
        from balatro_bot.engine import RuleEngine
        return RuleEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["RuleEngine"]
