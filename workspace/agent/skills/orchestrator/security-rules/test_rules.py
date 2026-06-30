import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rules import merge_rules, load_effective_rules


def test_merge_overrides_by_id():
    default = [{"id": "R-01", "severity": "block", "pattern": "a", "category": "x", "description": "d"}]
    overrides = [{"id": "R-01", "severity": "warn", "pattern": "b", "category": "x", "description": "d"}]
    merged = merge_rules(default, overrides)
    assert len(merged) == 1
    assert merged[0]["severity"] == "warn"
    assert merged[0]["pattern"] == "b"


def test_merge_appends_new_rules():
    default = [{"id": "R-01", "severity": "block", "pattern": "a", "category": "x", "description": "d"}]
    overrides = [{"id": "R-02", "severity": "block", "pattern": "b", "category": "x", "description": "d"}]
    merged = merge_rules(default, overrides)
    assert {r["id"] for r in merged} == {"R-01", "R-02"}


def test_load_effective_rules_includes_defaults():
    rules = load_effective_rules()
    ids = {r["id"] for r in rules}
    assert "R-DANGER-01" in ids
    assert "R-SECRET-01" in ids


def test_override_takes_effect_in_load():
    rules = load_effective_rules()
    inject02 = next(r for r in rules if r["id"] == "R-INJECT-02")
    assert inject02["severity"] == "warn", "user override should relax R-INJECT-02 to warn"


def test_user_appended_rule_present():
    rules = load_effective_rules()
    assert "R-USER-01" in {r["id"] for r in rules}


def test_every_rule_has_required_fields():
    for r in load_effective_rules():
        assert "id" in r
        assert "category" in r
        assert "pattern" in r
        assert "severity" in r
        assert "description" in r
        assert r["severity"] in ("block", "warn")
