import hashlib
import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import re

CONFIG_PATH = Path(__file__).parent.parent / "configs" / "sel-decision-rules.yaml"


@dataclass
class DecisionRule:
    id: str
    current_state: str
    previous_state: Optional[str]              # exact match (or "*" wildcard)
    previous_state_pattern: Optional[str]       # regex pattern
    action: str                                  # open_long | open_short | close | hold | no_action
    position_multiplier: Optional[float]
    notes: str


@dataclass
class PositionConfig:
    base_size_pct: float
    max_concurrent: int
    allow_add_to_position: bool


@dataclass
class AccountConfig:
    initial_nav_usdt: float
    currency: str


@dataclass
class RiskConfig:
    max_loss_per_trade_pct: float
    max_daily_drawdown_pct: float
    consecutive_loss_stop: int
    signal_lag_max_hours: int
    cascade_always_overrides: bool


@dataclass
class ExecutionConfig:
    maker_fee_pct: float
    taker_fee_pct: float
    slippage_open_pct: float
    slippage_close_pct: float
    assume_taker: bool


@dataclass
class DecisionConfig:
    version: str
    config_hash: str            # SHA256 of raw YAML content (first 16 hex chars)
    rules: list[DecisionRule]
    position: PositionConfig
    account: AccountConfig
    risk: RiskConfig
    execution: ExecutionConfig
    weekly_report_dir: str

    def find_rule(self, current_state: str, previous_state: Optional[str]) -> Optional[DecisionRule]:
        """First-match rule lookup. Wildcards and regex supported."""
        for rule in self.rules:
            if rule.current_state != current_state and rule.current_state != "*":
                continue
            if _matches_previous(rule, previous_state):
                return rule
        return None


def _matches_previous(rule: DecisionRule, prev: Optional[str]) -> bool:
    # Wildcard
    if rule.previous_state == "*":
        return True
    # Exact match
    if rule.previous_state is not None and rule.previous_state == prev:
        return True
    # Regex pattern
    if rule.previous_state_pattern is not None:
        pattern = rule.previous_state_pattern
        target = prev or ""
        return bool(re.match(pattern, target))
    return False


def load_config(path: Path = CONFIG_PATH) -> DecisionConfig:
    """Load and validate config. Raises ValueError on schema errors."""
    raw = path.read_text()
    data = yaml.safe_load(raw)
    _validate(data)
    config_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return _parse(data, config_hash)


def _validate(data: dict) -> None:
    required_top = {"meta", "decision_matrix", "position", "account", "risk"}
    missing = required_top - set(data)
    if missing:
        raise ValueError(f"Config missing required sections: {missing}")
    for rule in data.get("decision_matrix", []):
        if "id" not in rule or "current_state" not in rule or "action" not in rule:
            raise ValueError(f"Rule missing required fields: {rule}")
        valid_actions = {"open_long", "open_short", "close", "hold", "no_action"}
        if rule["action"] not in valid_actions:
            raise ValueError(f"Unknown action '{rule['action']}' in rule {rule['id']}")


def _parse(data: dict, config_hash: str) -> DecisionConfig:
    rules = [
        DecisionRule(
            id=r["id"],
            current_state=r["current_state"],
            previous_state=r.get("previous_state"),
            previous_state_pattern=r.get("previous_state_pattern"),
            action=r["action"],
            position_multiplier=r.get("position_multiplier"),
            notes=r.get("notes", ""),
        )
        for r in data["decision_matrix"]
    ]
    pos = data["position"]
    acc = data["account"]
    risk = data["risk"]
    exec_ = data
    rep = data.get("reports", {})
    return DecisionConfig(
        version=data["meta"]["version"],
        config_hash=config_hash,
        rules=rules,
        position=PositionConfig(
            base_size_pct=pos["base_size_pct"],
            max_concurrent=pos["max_concurrent"],
            allow_add_to_position=pos["allow_add_to_position"],
        ),
        account=AccountConfig(
            initial_nav_usdt=acc["initial_nav_usdt"],
            currency=acc["currency"],
        ),
        risk=RiskConfig(
            max_loss_per_trade_pct=risk["max_loss_per_trade_pct"],
            max_daily_drawdown_pct=risk["max_daily_drawdown_pct"],
            consecutive_loss_stop=risk["consecutive_loss_stop"],
            signal_lag_max_hours=risk["signal_lag_max_hours"],
            cascade_always_overrides=risk["cascade_always_overrides"],
        ),
        execution=ExecutionConfig(
            maker_fee_pct=exec_.get("maker_fee_pct", 0.0002),
            taker_fee_pct=exec_.get("taker_fee_pct", 0.0005),
            slippage_open_pct=exec_.get("slippage_open_pct", 0.0005),
            slippage_close_pct=exec_.get("slippage_close_pct", 0.0005),
            assume_taker=exec_.get("assume_taker", True),
        ),
        weekly_report_dir=rep.get("weekly_report_dir", "reports/weekly"),
    )
