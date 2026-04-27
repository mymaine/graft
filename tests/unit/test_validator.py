"""Unit tests for graft.validator.

Validator is a pure ast checker. It returns a fail reason string or None,
never raises. Loader wraps the reason into HelperLoadError downstream.
"""

from __future__ import annotations

from graft.validator import check


def test_happy_path_passes() -> None:
    source = '''
def list_issues(owner: str, repo: str) -> list[str]:
    """List issues.

    Generalization:
        Works for any (owner, repo).
    """
    return []
'''
    assert check(source, "github") is None


def test_docstring_missing_generalization_marker() -> None:
    source = '''
def list_issues(owner: str) -> None:
    """List issues for an owner."""
'''
    reason = check(source, "github")
    assert reason is not None
    assert "list_issues" in reason
    assert "Generalization" in reason


def test_no_docstring_at_all() -> None:
    source = """
def list_issues(owner: str) -> None:
    return None
"""
    reason = check(source, "github")
    assert reason is not None
    assert "list_issues" in reason


def test_first_function_ok_second_missing() -> None:
    source = '''
def good(owner: str) -> None:
    """Good helper.

    Generalization:
        Works for any owner.
    """


def bad(owner: str) -> None:
    """Bad helper without marker."""
'''
    reason = check(source, "github")
    assert reason is not None
    assert "bad" in reason
    assert "good" not in reason


def test_private_function_skipped() -> None:
    source = """
def _internal(owner: str) -> None:
    return None
"""
    assert check(source, "github") is None


def test_async_public_function_checked() -> None:
    source = """
async def fetch(owner: str) -> None:
    return None
"""
    reason = check(source, "github")
    assert reason is not None
    assert "fetch" in reason


def test_async_public_with_generalization_passes() -> None:
    source = '''
async def fetch(owner: str) -> None:
    """Fetch.

    Generalization:
        Works for any owner.
    """
'''
    assert check(source, "github") is None


def test_cross_service_import_rejected() -> None:
    source = """
from helpers.notion import something
"""
    reason = check(source, "github")
    assert reason is not None
    assert "helpers.notion" in reason
    assert "github" in reason


def test_cross_service_plain_import_rejected() -> None:
    source = """
import helpers.notion
"""
    reason = check(source, "github")
    assert reason is not None
    assert "helpers.notion" in reason


def test_same_service_import_allowed() -> None:
    source = '''
from helpers.github import _internal


def list_issues(owner: str) -> None:
    """List.

    Generalization:
        Works for any owner.
    """
'''
    assert check(source, "github") is None


def test_import_importlib_rejected() -> None:
    source = """
import importlib
"""
    reason = check(source, "github")
    assert reason is not None
    assert "importlib" in reason


def test_import_importlib_with_alias_rejected() -> None:
    source = """
import importlib as i
"""
    reason = check(source, "github")
    assert reason is not None
    assert "importlib" in reason


def test_from_importlib_import_rejected() -> None:
    source = """
from importlib import import_module
"""
    reason = check(source, "github")
    assert reason is not None
    assert "importlib" in reason


def test_dunder_import_call_rejected() -> None:
    source = """
x = __import__("os")
"""
    reason = check(source, "github")
    assert reason is not None
    assert "__import__" in reason


def test_exec_call_rejected() -> None:
    source = """
exec("print(1)")
"""
    reason = check(source, "github")
    assert reason is not None
    assert "exec" in reason


def test_eval_call_rejected() -> None:
    source = """
x = eval("1+1")
"""
    reason = check(source, "github")
    assert reason is not None
    assert "eval" in reason


def test_syntax_error_returns_reason() -> None:
    source = "def broken(:\n"
    reason = check(source, "github")
    assert reason is not None
    assert "syntax" in reason.lower()


def test_nested_function_not_checked() -> None:
    source = '''
def outer(owner: str) -> None:
    """Outer.

    Generalization:
        Works for any owner.
    """

    def inner() -> None:
        return None
'''
    assert check(source, "github") is None


def test_plain_helpers_import_allowed() -> None:
    source = '''
import helpers


def list_issues(owner: str) -> None:
    """List.

    Generalization:
        Works for any owner.
    """
'''
    assert check(source, "github") is None


def test_module_level_class_not_checked_for_docstring() -> None:
    source = """
class Foo:
    pass
"""
    assert check(source, "github") is None


def test_relative_import_rejected() -> None:
    source = '''
from . import helper


def foo() -> None:
    """Generalization: ..."""
'''
    reason = check(source, "github")
    assert reason is not None
    assert "relative import" in reason


def test_multi_alias_violation_in_second_position() -> None:
    source = '''
import os, importlib


def foo() -> None:
    """Generalization: ..."""
'''
    reason = check(source, "github")
    assert reason is not None
    assert "importlib" in reason
