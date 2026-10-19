# SPDX-License-Identifier: MIT-0
"""Publication-boundary gates C, D and E refuse credential shapes and unnamed suppressions.

The script scans every tracked text file, this one included, so every credential-shaped value and
every suppression marker below is built at runtime from parts: the file itself carries none. Each
refusal compares the whole finding list, and each is paired with a positive control that the same
rule lets through.
"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
from fictional_tokens import fictional_jwt

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

JWT_FINDING = "x.py:1: carries an encoded JWT literal; build it from readable parts"
SECRET_FINDING = (
    "x.py:1: carries a generated-looking secret literal; spell a fictional value as words"
)
NOSEC_FINDING = "x.py:1: nosec must name only reviewed Bandit test IDs"
SEMGREP_MARKER_FINDING = "x.py:1: `nosemgrep` must name namespaced rule IDs"


@pytest.fixture(scope="module")
def boundary() -> ModuleType:
    """Load the hyphenated script as a module so `_findings` is called directly."""
    spec = importlib.util.spec_from_file_location(
        "check_publication_boundary", ROOT / "scripts" / "check-publication-boundary.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestGateBMicrocentAmounts:
    """A declared microcent column's cell is an amount; every other 12-digit value is refused."""

    FIXTURE = "tests/fixtures/generated/amc-reach-frequency.csv"
    HEADER = "event_date_utc,campaign_id_string,impressions,spend,currency_iso_code"

    @staticmethod
    def _digits() -> str:
        # Built from parts, so this file carries no 12-digit literal of its own.
        return "".join(("366", "089", "170", "000"))

    def test_a_cell_of_the_declared_column_passes(self, boundary: ModuleType) -> None:
        row = f"2026-10-05,A0SYNTHETIC00000021,24252,{self._digits()},USD"
        assert boundary._findings(self.FIXTURE, f"{self.HEADER}\n{row}") == []

    def test_the_same_value_in_another_column_or_file_is_refused(
        self, boundary: ModuleType
    ) -> None:
        value = self._digits()
        finding = f"carries a 12-digit account-shaped value {value}"
        other_column = f"2026-10-05,A0SYNTHETIC00000021,{value},1,USD"
        assert boundary._findings(self.FIXTURE, f"{self.HEADER}\n{other_column}") == [
            f"{self.FIXTURE}:2: {finding}"
        ]
        row = f"2026-10-05,A0SYNTHETIC00000021,24252,{value},USD"
        other_file = "tests/fixtures/generated/amc-dsp-impressions.csv"
        assert boundary._findings(other_file, f"{self.HEADER}\n{row}") == [
            f"{other_file}:2: {finding}"
        ]
        assert boundary._findings("x.md", f"account {value}") == [f"x.md:1: {finding}"]


class TestGateCEncodedJwt:
    def test_an_encoded_jwt_literal_is_refused(self, boundary: ModuleType) -> None:
        assert boundary._findings("x.py", "token = " + json.dumps(fictional_jwt())) == [JWT_FINDING]

    @pytest.mark.parametrize(
        "source",
        [
            'fictional_jwt("claims", signature="signature")',
            're.compile(r"\\beyJ[A-Za-z0-9_-]+")',
        ],
    )
    def test_readable_parts_and_regex_sources_pass(self, boundary: ModuleType, source: str) -> None:
        assert boundary._findings("x.py", source) == []


class TestGateDGeneratedSecret:
    def test_a_generated_looking_secret_is_refused(self, boundary: ModuleType) -> None:
        value = "".join(("Ab1", "Cd2", "Ef3", "Gh4", "Ij5", "Kl6", "Mn7"))

        assert boundary._findings("x.py", f'k = "{value}"') == [SECRET_FINDING]

    def test_a_base64url_secret_is_refused(self, boundary: ModuleType) -> None:
        value = "-".join(("Ab1Cd2", "Ef3_Gh4", "Ij5Kl6Mn7"))

        assert boundary._findings("x.mjs", f"const k = '{value}';") == [
            SECRET_FINDING.replace("x.py", "x.mjs")
        ]

    @pytest.mark.parametrize(
        "source",
        [
            'k = "Fictional-Agent-Secret-For-Tests"',
            'k = "Stage1LifecycleHeadRecordV1"',
            f'k = "{"ab12" * 16}"',
            'k = "PutResourcePolicyAndMore"',
            'k = "adlytics-dev-evaluation-execution-v4"',
        ],
        ids=[
            "words",
            "one-inner-digit-run",
            "lowercase-hex-digest",
            "camel-case-identifier",
            "lowercase-resource-name",
        ],
    )
    def test_words_identifiers_and_digests_pass(self, boundary: ModuleType, source: str) -> None:
        assert boundary._findings("x.py", source) == []


class TestGateESuppressionMarkers:
    @pytest.mark.parametrize(
        "source",
        ["q = 1  # no" + "sec B680", "q = 1  # no" + "sec"],
        ids=["misspelt-id", "bare"],
    )
    def test_a_nosec_naming_no_reviewed_id_is_refused(
        self, boundary: ModuleType, source: str
    ) -> None:
        assert boundary._findings("x.py", source) == [NOSEC_FINDING]

    @pytest.mark.parametrize(
        "source",
        ["# no" + "semgrep", "# no" + "semgrep: regex_dos"],
        ids=["bare", "not-namespaced"],
    )
    def test_a_nosemgrep_naming_no_namespaced_id_is_refused(
        self, boundary: ModuleType, source: str
    ) -> None:
        assert boundary._findings("x.py", source) == [SEMGREP_MARKER_FINDING]

    @pytest.mark.parametrize(
        ("rel", "source"),
        [
            ("x.py", "q = f'…'  # noqa: S608 — see the note above  # no" + "sec B608"),
            ("x.py", "# no" + "semgrep: a.b.c, d.e.f  # noqa: E501"),
            ("x.ts", "// no" + "semgrep: bosco/unsafe-inline-csp-configuration"),
            ("x.md", "q = 1  # no" + "sec"),
        ],
        ids=["reviewed-bandit-id", "two-namespaced-ids", "typescript-comment", "markdown-quote"],
    )
    def test_named_markers_and_markdown_quotes_pass(
        self, boundary: ModuleType, rel: str, source: str
    ) -> None:
        assert boundary._findings(rel, source) == []

    @pytest.mark.parametrize(
        "case",
        [
            ("x.ts", "/* no" + "semgrep */", SEMGREP_MARKER_FINDING.replace("x.py", "x.ts")),
            ("x.py", "# NO" + "SEMGREP", SEMGREP_MARKER_FINDING),
            ("x.py", "q = 1  # No" + "Sec", NOSEC_FINDING),
        ],
        ids=["bare-block-form", "bare-uppercase-semgrep", "bare-mixed-case-nosec"],
    )
    def test_another_case_or_a_block_comment_is_still_checked(
        self, boundary: ModuleType, case: tuple[str, str, str]
    ) -> None:
        """O-MARKER-FORMS-001: a marker spelled another way is held to the same rule."""
        rel, source, finding = case
        assert boundary._findings(rel, source) == [finding]

    @pytest.mark.parametrize(
        ("rel", "source"),
        [
            ("x.ts", "/* no" + "semgrep: a.b */"),
            ("x.py", "# NO" + "SEMGREP: a.b"),
            ("x.py", "q = 1  # NO" + "SEC B608"),
        ],
        ids=["named-block-form", "named-uppercase-semgrep", "named-uppercase-nosec"],
    )
    def test_another_case_or_a_block_comment_passes_when_named(
        self, boundary: ModuleType, rel: str, source: str
    ) -> None:
        assert boundary._findings(rel, source) == []

    @pytest.mark.parametrize(
        "source",
        [
            'q = "SELECT 1 -- no' + 'semgrep"',
            "const page = `<!-- no" + "semgrep -->`;",
            " * no" + "semgrep",
            "foo();// no" + "semgrep",
            "x=1# no" + "semgrep",
            "a=b;/* no" + "semgrep */",
            "SELECT 1;-- no" + "semgrep",
            "<div><!-- no" + "semgrep -->",
            "x=1#no" + "semgrep",
            "eval(a)  # a no" + "semgrep must name a namespaced rule",
            "eval(b)  # see no" + "semgrep docs",
            'found.append(f"{where}: no' + 'semgrep must name namespaced rule IDs")',
            "q = 1  # NO" + "SEM",
            "q = 1  # no" + "semantic",
        ],
        ids=[
            "bare-sql-dash-dash",
            "bare-html-comment",
            "bare-jsdoc-continuation",
            "glued-line-comment",
            "glued-hash-comment",
            "glued-block-comment",
            "glued-sql-dash-dash",
            "glued-html-comment",
            "glued-with-no-space",
            "prose-in-a-comment",
            "prose-naming-docs",
            "prose-after-a-colon",
            "short-uppercase-form",
            "word-starting-with-the-marker",
        ],
    )
    def test_a_marker_after_whitespace_or_a_leader_is_still_checked(
        self, boundary: ModuleType, source: str
    ) -> None:
        """O-MARKER-LEADERS-001: Semgrep honours its marker after any space, with no leader."""
        assert boundary._findings("x.ts", source) == [
            SEMGREP_MARKER_FINDING.replace("x.py", "x.ts")
        ]

    @pytest.mark.parametrize(
        "source",
        [
            "    -- no" + "semgrep: a.b",
            "const page = `<!-- no" + "semgrep: a.b -->`;",
            'raise ValueError("no' + 'semgrep is not allowed here")',
            "# a `no" + "semgrep` must name a namespaced rule",
            "assert_no" + "semgrep_finding()",
        ],
        ids=[
            "named-sql-dash-dash",
            "named-html-comment",
            "prose-at-a-string-start",
            "prose-in-backticks",
            "inside-an-identifier",
        ],
    )
    def test_a_named_marker_or_a_quoted_marker_word_passes(
        self, boundary: ModuleType, source: str
    ) -> None:
        assert boundary._findings("x.ts", source) == []
