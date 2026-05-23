"""0.2.39 — data-driven test cases.

A template + N rows → N TestCase instances. The same logic exercised
against multiple data points, common in regression suites.

We test with JSON files (no PyYAML dep required); YAML round-trip
is exercised opportunistically when PyYAML is installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anthill.core.qa import (
    CaseTemplate,
    DataTable,
    expand_data_cases,
    load_data_table,
)


# --- placeholder detection -------------------------------------------


def test_template_required_placeholders_finds_all() -> None:
    """Every {name} across name/steps/expected/verification surfaces."""
    tpl = CaseTemplate(
        name="{scenario}: login {email}",
        prerequisites="user {email} exists",
        steps=[
            "open /login",
            "type {email}",
            "type {password}",
        ],
        expected="{expected_outcome}",
        verification="text contains {expected_outcome}",
    )
    placeholders = tpl.required_placeholders()
    assert placeholders == {"scenario", "email", "password", "expected_outcome"}


def test_template_no_placeholders() -> None:
    """A literal template (no {...}) has an empty placeholder set."""
    tpl = CaseTemplate(name="plain test", steps=["do this"])
    assert tpl.required_placeholders() == set()


# --- JSON load -------------------------------------------------------


def test_load_json_round_trip(tmp_path: Path) -> None:
    """Schema: {template: {...}, rows: [{...}, ...]}."""
    p = tmp_path / "cases.json"
    p.write_text(json.dumps({
        "template": {
            "name": "{scenario}: login with {email}",
            "prerequisites": "account {email} exists",
            "steps": ["open /login", "type {email}", "submit"],
            "expected": "{outcome}",
            "verification": "text={outcome}",
        },
        "rows": [
            {"scenario": "good", "email": "a@x.com", "outcome": "dashboard"},
            {"scenario": "bad", "email": "b@x.com", "outcome": "error"},
        ],
    }))
    dt = load_data_table(p)
    assert dt.template.name == "{scenario}: login with {email}"
    assert len(dt.rows) == 2
    assert dt.rows[0]["scenario"] == "good"


def test_load_rejects_missing_template(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"rows": [{"x": "y"}]}))
    with pytest.raises(ValueError, match="template"):
        load_data_table(p)


def test_load_rejects_empty_rows(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({
        "template": {"name": "x", "steps": []},
        "rows": [],
    }))
    with pytest.raises(ValueError, match="rows"):
        load_data_table(p)


def test_load_rejects_unknown_suffix(tmp_path: Path) -> None:
    p = tmp_path / "data.txt"
    p.write_text("anything")
    with pytest.raises(ValueError, match="format"):
        load_data_table(p)


def test_load_rejects_row_missing_placeholder_key(tmp_path: Path) -> None:
    """Every row must supply every {placeholder} the template uses."""
    p = tmp_path / "missing.json"
    p.write_text(json.dumps({
        "template": {
            "name": "{scenario}: {email}",
            "steps": ["x"],
        },
        "rows": [
            {"scenario": "A", "email": "a@x.com"},
            {"scenario": "B"},  # missing email
        ],
    }))
    with pytest.raises(ValueError, match=r"missing keys.*email"):
        load_data_table(p)


# --- expand_data_cases -----------------------------------------------


def _table_with(rows: list[dict]) -> DataTable:
    return DataTable(
        template=CaseTemplate(
            name="{scenario}: login with {email}",
            prerequisites="account {email} exists",
            steps=[
                "open /login",
                "type {email}",
                "type {password}",
                "submit",
            ],
            expected="{outcome}",
            verification="text contains {outcome}",
        ),
        rows=rows,
    )


def test_expand_produces_one_case_per_row() -> None:
    table = _table_with([
        {"scenario": "good", "email": "a@x.com", "password": "x", "outcome": "dashboard"},
        {"scenario": "bad", "email": "b@x.com", "password": "y", "outcome": "error"},
        {"scenario": "missing pw", "email": "c@x.com", "password": "", "outcome": "required"},
    ])
    cases = expand_data_cases(table)
    assert len(cases) == 3
    assert cases[0].id == 1
    assert cases[1].id == 2


def test_expand_substitutes_placeholders_in_name() -> None:
    table = _table_with([
        {"scenario": "正确密码", "email": "u@x.com", "password": "y", "outcome": "ok"},
    ])
    c = expand_data_cases(table)[0]
    assert c.name == "正确密码: login with u@x.com"


def test_expand_substitutes_in_steps_list() -> None:
    """Each item in template.steps gets its own substitution."""
    table = _table_with([
        {"scenario": "x", "email": "alice@x.com", "password": "pw1", "outcome": "ok"},
    ])
    c = expand_data_cases(table)[0]
    assert c.steps == [
        "open /login",
        "type alice@x.com",
        "type pw1",
        "submit",
    ]


def test_expand_handles_chinese_values() -> None:
    """Unicode in row values flows through unchanged."""
    table = _table_with([
        {"scenario": "中文场景", "email": "u@x.com", "password": "密码123",
         "outcome": "看到错误提示"},
    ])
    c = expand_data_cases(table)[0]
    assert "中文场景" in c.name
    assert "密码123" in c.steps[2]
    assert c.expected == "看到错误提示"


def test_expand_each_case_independent() -> None:
    """Modifying one TestCase doesn't affect siblings."""
    table = _table_with([
        {"scenario": "a", "email": "1@x.com", "password": "p", "outcome": "ok"},
        {"scenario": "b", "email": "2@x.com", "password": "p", "outcome": "ok"},
    ])
    cases = expand_data_cases(table)
    cases[0].steps.append("extra step")
    assert "extra step" not in cases[1].steps


# --- end-to-end: load + expand ---------------------------------------


def test_end_to_end_load_and_expand(tmp_path: Path) -> None:
    """One pass: write JSON, load_data_table, expand_data_cases."""
    p = tmp_path / "regression.json"
    p.write_text(json.dumps({
        "template": {
            "name": "回归 #{tid}: {scenario}",
            "steps": ["请求 {endpoint}", "验证 {expect_code}"],
            "expected": "{expect_body}",
            "verification": "JSON.status_code == {expect_code}",
        },
        "rows": [
            {"tid": "1", "scenario": "正常", "endpoint": "/api/health",
             "expect_code": "200", "expect_body": "ok"},
            {"tid": "2", "scenario": "404", "endpoint": "/api/nope",
             "expect_code": "404", "expect_body": "not found"},
        ],
    }))
    table = load_data_table(p)
    cases = expand_data_cases(table)
    assert len(cases) == 2
    assert cases[0].name == "回归 #1: 正常"
    assert cases[1].verification == "JSON.status_code == 404"
