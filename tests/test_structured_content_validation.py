from dataclasses import replace

import pytest

from ocr_mcp_server.services.structured_content import (
    StructuredContentInvalid,
    StructuredContentLimits,
    validate_formula_latex,
    validate_table_html,
)


@pytest.fixture
def limits() -> StructuredContentLimits:
    return StructuredContentLimits(
        max_characters=2_000,
        max_utf8_bytes=4_000,
        max_html_depth=12,
        max_html_elements=30,
        max_table_rows=5,
        max_table_cells=10,
        max_latex_repetition=8,
        max_artifact_bytes=100_000,
    )


@pytest.mark.parametrize(
    "value",
    [
        "<table><tr><td>账户</td></tr></table>",
        "<html><body><div><table><tbody><tr><th rowspan='2'>A&amp;B</th>"
        "<td colspan='2'>值</td></tr></tbody></table></div></body></html>",
    ],
)
def test_valid_table_html_is_returned_trimmed(value, limits):
    assert validate_table_html(f"  {value}\n", limits) == value


@pytest.mark.parametrize(
    "value",
    [
        "<table><tr><td></td></tr></table>",
        "<table><tr><td> </td></tr></table>",
        "<table><tr><td>x</td></tr>",
        "<table><tr><td>x</td></tr></table><table><tr><td>y</td></tr></table>",
        "<table><tr><td><table><tr><td>x</td></tr></table></td></tr></table>",
        "<table><tr><td onclick='x'>x</td></tr></table>",
        "<table><tr><td style='color:red'>x</td></tr></table>",
        "<table><tr><td href='https://unsafe.invalid'>x</td></tr></table>",
        "<table><script>x</script><tr><td>x</td></tr></table>",
        "<table><tr><td rowspan='0'>x</td></tr></table>",
        "<table><tr><td rowspan='999'>x</td></tr></table>",
        "text<table><tr><td>x</td></tr></table>",
        "<table><!--x--><tr><td>x</td></tr></table>",
        "<table><tr><td>x\x00</td></tr></table>",
        "<table><tr><td>x\ny</td></tr></table>",
        "<table><tr><td>x\ty</td></tr></table>",
        "<table><tr><td>x\u202ey</td></tr></table>",
        "<table><tr><td>x&#10;y</td></tr></table>",
        "<table><tr><td>x&#x202e;y</td></tr></table>",
        "<table><tr><td>&unknown;</td></tr></table>",
        "<table><tr><td>&#x110000;</td></tr></table>",
        "<table><tr><td>&#12x;</td></tr></table>",
        "<table><tr><td>&amp</td></tr></table>",
        "<table><tr><td>x</td></tr></table junk>",
        '<table><tr><td>x</td></tr></table foo="bar">',
        "<table><tr><td>x</td></tr></table/ >",
        "<table><tr><td>x<</td></tr></table>",
        "<table><tr><td>< </td></tr></table>",
        "<table><tr><td>x\ud800</td></tr></table>",
    ],
)
def test_invalid_table_html_is_rejected_without_echo(value, limits):
    with pytest.raises(StructuredContentInvalid) as raised:
        validate_table_html(value, limits)
    if value:
        assert value not in str(raised.value)
    assert raised.value.__cause__ is None


def test_table_limits_are_enforced(limits):
    too_many_rows = "<table>" + "".join("<tr><td>x</td></tr>" for _ in range(6)) + "</table>"
    too_deep = "<html><body>" + "<div>" * 11 + "<table><tr><td>x</td></tr></table>" + "</div>" * 11 + "</body></html>"
    for value in (too_many_rows, too_deep, "x" * 2_001):
        with pytest.raises(StructuredContentInvalid):
            validate_table_html(value, limits)

    too_many_cells = "<table><tr>" + "".join("<td>x</td>" for _ in range(11)) + "</tr></table>"
    with pytest.raises(StructuredContentInvalid):
        validate_table_html(too_many_cells, limits)

    element_limits = StructuredContentLimits(
        max_characters=100,
        max_utf8_bytes=200,
        max_html_depth=5,
        max_html_elements=5,
        max_table_rows=2,
        max_table_cells=5,
        max_latex_repetition=8,
        max_artifact_bytes=500,
    )
    with pytest.raises(StructuredContentInvalid):
        validate_table_html("<html><body><div><table><tr><td>x</td></tr></table></div></body></html>", element_limits)

    byte_limits = replace(limits, max_characters=100, max_utf8_bytes=100)
    with pytest.raises(StructuredContentInvalid):
        validate_table_html("<table><tr><td>" + "银" * 30 + "</td></tr></table>", byte_limits)


@pytest.mark.parametrize(
    "value",
    [
        r"x_{i} + (y[z])",
        r"\begin{matrix}a & b \\ c & d\end{matrix}",
        r"\(x + y\)",
        r"$$x^2 + y^2$$",
        r"\{x\} + [y]",
    ],
)
def test_valid_formula_is_returned_trimmed(value, limits):
    assert validate_formula_latex(f" {value}\n", limits) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "x\x00",
        "x\ny",
        "x\ty",
        "x\u202ey",
        "x\ud800",
        "{x]",
        r"\begin{matrix}x\end{array}",
        r"\begin{bad.name}x\end{bad.name}",
        r"\begin matrix x\end matrix",
        r"\begin{matrix}\begin{array}x\end{matrix}\end{array}",
        r"$x",
        r"\(x]",
        r"\input{secret}",
        r"\csname evil\endcsname",
        r"\write18{evil}",
        r"\openout1=evil",
        r"\def\x{evil}",
        r"\newcommand{\x}{evil}",
        r"\directlua{evil}",
        r"\href{https://unsafe.invalid}{x}",
        r"\includegraphics{../../secret}",
        r"\pdfximage{../../secret}",
        r"\verbatiminput{../../secret}",
        r"\lstinputlisting{../../secret}",
        r"\inputminted{python}{../../secret}",
        r"\ShellEscape{evil}",
        r"\^^69nput{../../secret}",
        "x" * 2_001,
        "a" * 9,
        r"\alpha " * 9,
    ],
)
def test_invalid_formula_is_rejected_without_echo(value, limits):
    with pytest.raises(StructuredContentInvalid) as raised:
        validate_formula_latex(value, limits)
    if value:
        assert value not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("field", ["max_characters", "max_utf8_bytes", "max_html_depth", "max_artifact_bytes"])
def test_limits_reject_boolean_and_non_positive_values(field):
    values = dict(
        max_characters=10,
        max_utf8_bytes=20,
        max_html_depth=3,
        max_html_elements=5,
        max_table_rows=2,
        max_table_cells=3,
        max_latex_repetition=4,
        max_artifact_bytes=100,
    )
    values[field] = True
    with pytest.raises(ValueError):
        StructuredContentLimits(**values)
    values[field] = 0
    with pytest.raises(ValueError):
        StructuredContentLimits(**values)


def test_limits_reject_contradictions():
    with pytest.raises(ValueError):
        StructuredContentLimits(
            max_characters=100,
            max_utf8_bytes=99,
            max_html_depth=3,
            max_html_elements=2,
            max_table_rows=2,
            max_table_cells=3,
            max_latex_repetition=4,
            max_artifact_bytes=90,
        )


def test_limits_reject_values_above_hard_safety_caps():
    with pytest.raises(ValueError):
        StructuredContentLimits(
            max_characters=10_000_001,
            max_utf8_bytes=40_000_000,
            max_html_depth=64,
            max_html_elements=20_000,
            max_table_rows=5_000,
            max_table_cells=10_000,
            max_latex_repetition=128,
            max_artifact_bytes=64 * 1024 * 1024,
        )
