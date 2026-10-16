"""Unit tests for ceo_agent — no live Postgres or LLM required."""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ceo_agent  # noqa: E402


def test_int_returns_default_when_missing():
    assert ceo_agent._int("CEO_TEST_MISSING_VAR_XYZ", 42) == 42


def test_int_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("CEO_TEST_INT", "not-a-number")
    assert ceo_agent._int("CEO_TEST_INT", 7) == 7


def test_int_parses_valid_value(monkeypatch):
    monkeypatch.setenv("CEO_TEST_INT", "30")
    assert ceo_agent._int("CEO_TEST_INT", 7) == 30


def test_build_prompt_includes_intel_fields():
    rows = [(1, "vera", "Intel about X", 80, "2026-10-16 05:00:00", "report")]
    prompt = ceo_agent.build_prompt(rows)
    assert "[from vera]" in prompt
    assert "priority 80" in prompt
    assert "report" in prompt
    assert "Intel about X" in prompt


def test_build_prompt_truncates_long_content():
    rows = [(2, "altair", "z" * 600, 50, "2026-10-16 05:00:00", "note")]
    prompt = ceo_agent.build_prompt(rows)
    assert "z" * 600 not in prompt
    assert "z" * 500 in prompt


def test_build_prompt_empty_rows():
    prompt = ceo_agent.build_prompt([])
    assert "no pending intelligence" in prompt


def test_targets_parsed_from_env(monkeypatch):
    monkeypatch.setenv("INTEL_TARGETS", "ceo, ops ,all")
    reloaded = importlib.reload(ceo_agent)
    try:
        assert reloaded.TARGETS == ["ceo", "ops", "all"]
    finally:
        monkeypatch.undo()
        importlib.reload(ceo_agent)
