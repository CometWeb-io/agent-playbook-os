import pytest
from agent_playbook_os.expressions import render, safe_eval


def test_native_template_value():
    ctx = {"inputs":{"x":{"a":1}}}
    assert render("{{ inputs.x }}", ctx) == {"a":1}


def test_interpolated_template():
    assert render("hi {{ inputs.name }}", {"inputs":{"name":"Ada"}}) == "hi Ada"


def test_safe_condition():
    ctx = {"steps":{"a":{"output":{"ready":True}}}}
    assert safe_eval("steps.a.output.ready == True", ctx) is True


def test_function_calls_are_forbidden():
    with pytest.raises(ValueError, match="not allowed"):
        safe_eval("__import__('os').system('echo bad')", {})


def test_hyphenated_step_id_is_supported_in_dot_reference():
    ctx = {"steps": {"independent-reviews": {"output": {"ok": True}}}}
    assert render("{{ steps.independent-reviews.output.ok }}", ctx) is True


def test_expression_length_is_bounded():
    with pytest.raises(ValueError, match="expression too large"):
        safe_eval("True and " * 1000 + "True", {})


def test_expression_ast_size_is_bounded():
    expr = " and ".join(["True"] * 300)
    with pytest.raises(ValueError, match="AST too large"):
        safe_eval(expr, {})
