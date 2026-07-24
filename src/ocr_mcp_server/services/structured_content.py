"""Conservative structural validation for recognized table and formula content."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
import unicodedata


class StructuredContentInvalid(Exception):
    """Internal content rejection that deliberately carries no input context."""

    def __init__(self) -> None:
        super().__init__("Structured recognized content is invalid.")


@dataclass(frozen=True, slots=True)
class StructuredContentLimits:
    max_characters: int
    max_utf8_bytes: int
    max_html_depth: int
    max_html_elements: int
    max_table_rows: int
    max_table_cells: int
    max_latex_repetition: int
    max_artifact_bytes: int

    def __post_init__(self) -> None:
        values = (
            self.max_characters,
            self.max_utf8_bytes,
            self.max_html_depth,
            self.max_html_elements,
            self.max_table_rows,
            self.max_table_cells,
            self.max_latex_repetition,
            self.max_artifact_bytes,
        )
        hard_caps = (
            10_000_000,
            40_000_000,
            256,
            100_000,
            20_000,
            100_000,
            1_024,
            256 * 1024 * 1024,
        )
        if any(
            type(value) is not int or value < 1 or value > cap
            for value, cap in zip(values, hard_caps, strict=True)
        ):
            raise ValueError("structured-content limits must be positive integers")
        if (
            self.max_utf8_bytes < self.max_characters
            or self.max_artifact_bytes < self.max_utf8_bytes
            or self.max_html_depth > self.max_html_elements
            or self.max_table_rows > self.max_html_elements
            or self.max_table_cells > self.max_html_elements
        ):
            raise ValueError("structured-content limits are contradictory")


_ALLOWED_TAGS = frozenset(
    {"html", "body", "div", "table", "thead", "tbody", "tfoot", "tr", "td", "th"}
)
_CELL_TAGS = frozenset({"td", "th"})
_SPAN_ATTRIBUTES = frozenset({"rowspan", "colspan"})
_DANGEROUS_TEX = re.compile(
    r"\\(?:input|include|includeonly|write|immediate|openout|openin|read|catcode|csname|usepackage|documentclass|newread|newwrite|def|edef|gdef|xdef|newcommand|renewcommand|special|directlua|href|url|loop|repeat)(?![A-Za-z])",
    re.IGNORECASE,
)
_ENVIRONMENT = re.compile(r"\\(begin|end)\{([A-Za-z][A-Za-z0-9*_-]{0,31})\}")
_TOKEN = re.compile(r"\\[A-Za-z]+|\\.|[A-Za-z0-9]+|[^\s]")
_TEX_COMMAND = re.compile(r"\\([A-Za-z]+|.)", re.DOTALL)
_SAFE_TEX_ENVIRONMENTS = frozenset(
    {
        "array", "matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix",
        "Vmatrix", "smallmatrix", "cases", "aligned", "alignedat", "gathered",
        "split", "equation", "equation*", "align", "align*", "gather", "gather*",
        "multline", "multline*",
    }
)
_SAFE_TEX_COMMANDS = frozenset(
    """
    begin end frac dfrac tfrac cfrac sqrt binom dbinom tbinom substack
    overset underset stackrel limits nolimits left right middle big Big bigg Bigg
    bigl bigr bigm Bigl Bigr Bigm biggl biggr biggm Biggl Biggr Biggm
    mathrm mathbf mathit mathsf mathtt mathcal mathbb mathfrak text operatorname
    boldsymbol bm overline underline hat widehat bar vec dot ddot dddot tilde
    widetilde breve check acute grave sum prod coprod int iint iiint iiiint oint
    lim max min sup inf log ln exp sin cos tan cot sec csc arcsin arccos arctan
    sinh cosh tanh det gcd Pr pmod bmod mod quad qquad
    alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa
    lambda mu nu xi omicron pi varpi rho varrho sigma varsigma tau upsilon phi
    varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
    partial nabla infty ell hbar imath jmath Re Im wp prime
    times div cdot ast star circ bullet cap cup uplus sqcap sqcup vee wedge setminus
    wr diamond triangle triangleleft triangleright bigtriangleup bigtriangledown
    le leq ge geq ne neq approx sim simeq equiv cong propto ll gg subset superset
    subseteq supseteq nsubseteq nsupseteq in notin ni parallel perp mid
    to gets mapsto rightarrow leftarrow leftrightarrow Rightarrow Leftarrow
    Leftrightarrow longrightarrow longleftarrow longleftrightarrow Longrightarrow
    Longleftarrow Longleftrightarrow uparrow downarrow updownarrow Uparrow Downarrow
    Updownarrow hookrightarrow hookleftarrow nearrow searrow swarrow nwarrow
    forall exists nexists neg emptyset varnothing top bot angle measuredangle
    ldots cdots vdots ddots dots dotsc dotsb dotsm
    """.split()
)
_HTML_ENTITY = re.compile(
    r"&(?:(?P<numeric>#[0-9]+|#[xX][0-9A-Fa-f]+)|amp|lt|gt|quot|apos|nbsp);"
)
_HTML_TAG_TOKEN = re.compile(
    r"</?[A-Za-z][A-Za-z0-9]*(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*"
    r"(?:\s*=\s*(?:\"[^\"<>]*\"|'[^'<>]*'|[^\s\"'=<>`]+))?)*>"
)


def _validate_text(value: object, limits: StructuredContentLimits, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise StructuredContentInvalid() from None
    selected = value.strip()
    try:
        encoded = selected.encode("utf-8", errors="strict")
    except UnicodeError:
        raise StructuredContentInvalid() from None
    if (
        (not selected and not allow_empty)
        or len(selected) > limits.max_characters
        or len(encoded) > limits.max_utf8_bytes
        or any(
            character == "\x00"
            or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in selected
        )
    ):
        raise StructuredContentInvalid() from None
    return selected


def validate_plain_text(
    value: object,
    limits: StructuredContentLimits,
) -> str:
    if not isinstance(value, str):
        raise StructuredContentInvalid() from None
    selected = value.strip()
    try:
        encoded = selected.encode("utf-8", errors="strict")
    except UnicodeError:
        raise StructuredContentInvalid() from None
    if (
        not selected
        or len(selected) > limits.max_characters
        or len(encoded) > limits.max_utf8_bytes
        or any(
            character != "\n"
            and unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in selected
        )
    ):
        raise StructuredContentInvalid() from None
    return selected


class _TableParser(HTMLParser):
    def __init__(self, limits: StructuredContentLimits) -> None:
        super().__init__(convert_charrefs=True)
        self.limits = limits
        self.stack: list[str] = []
        self.elements = 0
        self.tables = 0
        self.rows = 0
        self.cells = 0
        self.nonempty_cell = False
        self._cell_depth = 0

    def _reject(self) -> None:
        raise StructuredContentInvalid() from None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            self._reject()
        self.elements += 1
        if self.elements > self.limits.max_html_elements or len(self.stack) + 1 > self.limits.max_html_depth:
            self._reject()
        names = [name.lower() for name, _ in attrs]
        if len(names) != len(set(names)):
            self._reject()
        if tag not in _CELL_TAGS and attrs:
            self._reject()
        for name, value in attrs:
            if name.lower() not in _SPAN_ATTRIBUTES or value is None or not value.isascii() or not value.isdecimal():
                self._reject()
            span = int(value)
            bound = self.limits.max_table_rows if name.lower() == "rowspan" else self.limits.max_table_cells
            if span < 1 or span > bound:
                self._reject()

        parent = self.stack[-1] if self.stack else None
        if tag == "html" and parent is not None:
            self._reject()
        if tag == "body" and parent != "html":
            self._reject()
        if tag == "div" and parent not in {None, "html", "body", "div"}:
            self._reject()
        if tag == "table":
            if self.tables or parent not in {None, "html", "body", "div"}:
                self._reject()
            self.tables += 1
        elif tag in {"thead", "tbody", "tfoot"} and parent != "table":
            self._reject()
        elif tag == "tr":
            if parent not in {"table", "thead", "tbody", "tfoot"}:
                self._reject()
            self.rows += 1
            if self.rows > self.limits.max_table_rows:
                self._reject()
        elif tag in _CELL_TAGS:
            if parent != "tr":
                self._reject()
            self.cells += 1
            if self.cells > self.limits.max_table_cells:
                self._reject()
            self._cell_depth += 1
        self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._reject()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self.stack or self.stack[-1] != tag:
            self._reject()
        self.stack.pop()
        if tag in _CELL_TAGS:
            self._cell_depth -= 1

    def parse_endtag(self, index: int) -> int:
        end = self.rawdata.find(">", index + 2)
        if end < 0:
            return -1
        if re.fullmatch(
            r"</[A-Za-z][A-Za-z0-9]*>", self.rawdata[index : end + 1]
        ) is None:
            self._reject()
        return super().parse_endtag(index)

    def handle_data(self, data: str) -> None:
        if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in data):
            self._reject()
        if data.strip():
            if self._cell_depth < 1:
                self._reject()
            self.nonempty_cell = True

    def handle_comment(self, data: str) -> None:
        del data
        self._reject()

    def handle_decl(self, decl: str) -> None:
        del decl
        self._reject()

    def handle_pi(self, data: str) -> None:
        del data
        self._reject()

    def unknown_decl(self, data: str) -> None:
        del data
        self._reject()


def validate_table_html(value: object, limits: StructuredContentLimits) -> str:
    selected = _validate_text(value, limits, allow_empty=False)
    tag_cursor = 0
    while True:
        tag_cursor = selected.find("<", tag_cursor)
        if tag_cursor < 0:
            break
        tag = _HTML_TAG_TOKEN.match(selected, tag_cursor)
        if tag is None:
            raise StructuredContentInvalid() from None
        tag_cursor = tag.end()
    cursor = 0
    while True:
        cursor = selected.find("&", cursor)
        if cursor < 0:
            break
        entity = _HTML_ENTITY.match(selected, cursor)
        if entity is None:
            raise StructuredContentInvalid() from None
        numeric = entity.group("numeric")
        if numeric is not None:
            try:
                codepoint = int(
                    numeric[2:], 16
                ) if numeric[1] in {"x", "X"} else int(numeric[1:], 10)
                character = chr(codepoint)
            except (ValueError, OverflowError):
                raise StructuredContentInvalid() from None
            if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
                raise StructuredContentInvalid() from None
        cursor = entity.end()
    parser = _TableParser(limits)
    try:
        parser.feed(selected)
        parser.close()
    except StructuredContentInvalid:
        raise
    except BaseException:
        raise StructuredContentInvalid() from None
    if parser.stack or parser.tables != 1 or parser.rows < 1 or parser.cells < 1 or not parser.nonempty_cell:
        raise StructuredContentInvalid() from None
    return selected


def _is_escaped(value: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and value[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def validate_formula_latex(value: object, limits: StructuredContentLimits) -> str:
    selected = _validate_text(value, limits, allow_empty=False)
    if _DANGEROUS_TEX.search(selected) or "^^" in selected:
        raise StructuredContentInvalid() from None
    for index, character in enumerate(selected):
        if character == "%" and not _is_escaped(selected, index):
            raise StructuredContentInvalid() from None
    for command_match in _TEX_COMMAND.finditer(selected):
        command = command_match.group(1)
        if command.isalpha() and command not in _SAFE_TEX_COMMANDS:
            raise StructuredContentInvalid() from None

    environments: list[str] = []
    environment_ranges: set[int] = set()
    valid_environment_starts: set[int] = set()
    for match in _ENVIRONMENT.finditer(selected):
        valid_environment_starts.add(match.start())
        environment_ranges.update(range(match.start(), match.end()))
        action, name = match.groups()
        if name not in _SAFE_TEX_ENVIRONMENTS:
            raise StructuredContentInvalid() from None
        if action == "begin":
            environments.append(name)
            if len(environments) > limits.max_html_depth:
                raise StructuredContentInvalid() from None
        elif not environments or environments.pop() != name:
            raise StructuredContentInvalid() from None
    if environments:
        raise StructuredContentInvalid() from None
    all_environment_starts = {
        match.start()
        for match in re.finditer(r"\\(?:begin|end)(?![A-Za-z])", selected)
    }
    if all_environment_starts != valid_environment_starts:
        raise StructuredContentInvalid() from None

    stack: list[str] = []
    math_stack: list[str] = []
    pairs = {"}": "{", "]": "[", ")": "("}
    index = 0
    while index < len(selected):
        if index in environment_ranges:
            index += 1
            continue
        if selected.startswith("\\(", index):
            math_stack.append("\\("); index += 2; continue
        if selected.startswith("\\)", index):
            if not math_stack or math_stack.pop() != "\\(":
                raise StructuredContentInvalid() from None
            index += 2; continue
        if selected.startswith("\\[", index):
            math_stack.append("\\["); index += 2; continue
        if selected.startswith("\\]", index):
            if not math_stack or math_stack.pop() != "\\[":
                raise StructuredContentInvalid() from None
            index += 2; continue
        character = selected[index]
        if character == "$" and not _is_escaped(selected, index):
            marker = "$$" if selected.startswith("$$", index) else "$"
            if math_stack and math_stack[-1] == marker:
                math_stack.pop()
            else:
                math_stack.append(marker)
            index += len(marker); continue
        if character in "{[(" and not _is_escaped(selected, index):
            stack.append(character)
        elif character in "}])" and not _is_escaped(selected, index):
            if not stack or stack.pop() != pairs[character]:
                raise StructuredContentInvalid() from None
        index += 1
    if stack or math_stack:
        raise StructuredContentInvalid() from None

    previous = None
    repetitions = 0
    for token in _TOKEN.findall(selected):
        if token == previous:
            repetitions += 1
        else:
            previous = token
            repetitions = 1
        if repetitions > limits.max_latex_repetition:
            raise StructuredContentInvalid() from None
    for character in set(selected):
        if not character.isspace() and character * (limits.max_latex_repetition + 1) in selected:
            raise StructuredContentInvalid() from None
    return selected
