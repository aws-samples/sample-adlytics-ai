# SPDX-License-Identifier: MIT-0
"""The row-ceiling probe's local legs and decision rules, on fictional data (ADR-143)."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest

from adapters.jobs.preset_artifact_publisher import bakeability
from adapters.outbound.athena.execution import StatementOutcome

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_bake_row_ceiling as probe  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-143")]


def test_the_probe_refuses_to_run_without_an_output_path() -> None:
    with pytest.raises(SystemExit) as raised:
        probe.main([])
    assert raised.value.code == 2


def test_p2_needs_every_target_input_explicitly(tmp_path: Path, capsys: Any) -> None:
    """Paired: without --local-only, a missing workgroup refuses before any AWS session."""

    def no_session(**_arguments: object) -> object:
        raise AssertionError("no AWS session may open")

    output = tmp_path / "out.json"
    code = probe.main(
        ["--output", str(output), "--profile", "p", "--environment", "dev"], no_session
    )
    assert code == 2 and not output.exists()
    assert "--bake-workgroup" in capsys.readouterr().out


class _Sts:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": "000000000000"}


class _Session:
    def client(self, _name: str) -> object:
        return _Sts()


def test_a_profile_that_resolves_elsewhere_writes_nothing(tmp_path: Path) -> None:
    output = tmp_path / "out.json"
    arguments = [
        "--output", str(output), "--profile", "fictional", "--environment", "dev",
        "--region", "us-east-1", "--expected-account", "111111111111",
        "--bake-workgroup", "wg-bake", "--interactive-workgroup", "wg-chat",
        "--database", "db",
    ]  # fmt: skip

    def session(**_arguments: object) -> _Session:
        return _Session()

    assert probe.main(arguments, session) == 2
    assert not output.exists()


def _execution(  # noqa: PLR0913, PLR0917 -- one execution's four recorded fields
    step: str, kind: str, reused: bool, prefix: str | None
) -> dict[str, Any]:
    return {
        "step": step,
        "workgroup_kind": kind,
        "reused_previous_result": reused,
        "location_prefix": prefix,
    }


def _p2c(cross_reused: bool, second_reused: bool, prefix: str = "bake") -> list[dict[str, Any]]:
    return [
        _execution("cross_interactive", "interactive", False, "interactive"),
        _execution("cross_bake", "bake", cross_reused, prefix),
        _execution("control_bake_first", "bake", False, "bake"),
        _execution("control_bake_second", "bake", second_reused, "bake"),
    ]


def test_p2c_classifies_documented_undocumented_and_inconclusive_reuse() -> None:
    assert probe.classify_p2c(_p2c(cross_reused=False, second_reused=True)) == "a_as_documented"
    assert probe.classify_p2c(_p2c(cross_reused=True, second_reused=True)) == (
        "b_not_as_documented"
    )
    assert probe.classify_p2c(_p2c(False, True, prefix="interactive")) == "b_not_as_documented"
    # The control didn't reuse, so a non-reuse across workgroups proves nothing.
    assert probe.classify_p2c(_p2c(cross_reused=False, second_reused=False)) == "inconclusive"


def test_the_stop_rule_fires_on_each_of_its_three_reasons_and_only_then() -> None:
    passed = {"passed": True}
    documented = {"outcome": "a_as_documented"}
    assert probe.stop_rule(20_000, passed, documented) == {"stop": False, "reasons": []}
    assert probe.stop_rule(None, passed, documented)["stop"]
    assert probe.stop_rule(20_000, {"passed": False}, documented)["stop"]
    assert probe.stop_rule(20_000, passed, {"outcome": "inconclusive"})["stop"]
    assert probe.stop_rule(10_000, None, None) == {"stop": False, "reasons": []}


class _Athena:
    """Pages one fictional result: the header, then each row's cells as `VarCharValue`s."""

    def __init__(self, rows: list[list[str | None]]) -> None:
        self.rows = rows

    def get_query_results(self, **_arguments: str) -> dict[str, object]:
        header = {"Data": [{"VarCharValue": f"c{index}"} for index in range(8)]}
        data = [
            {"Data": [{} if cell is None else {"VarCharValue": cell} for cell in row]}
            for row in self.rows
        ]
        return {"ResultSet": {"Rows": [header, *data]}}


class _S3:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def get_object(self, **_arguments: str) -> dict[str, object]:
        return {"Body": io.BufferedReader(io.BytesIO(self.body))}


_ROW: list[str | None] = [None, "", 'a "quoted" word', "a,b,c", "one\ntwo", "café", "1.0", "t"]
_CSV = (
    b'"c0","c1","c2","c3","c4","c5","c6","c7"\n'
    b',"","a ""quoted"" word","a,b,c","one\ntwo","caf\xc3\xa9","1.0","t"\n'
)


def _run(body: bytes) -> Any:
    run = probe._Run(_Athena([_ROW]), _S3(body), "db")  # pyright: ignore[reportPrivateUsage]

    def execute(_sql: str, _workgroup: str, _reuse: int) -> tuple[str, StatementOutcome]:
        return "execution-1", StatementOutcome(output_location="s3://fictional/bake/x.csv")

    run.execute = execute  # type: ignore[method-assign]
    return run


def test_p2a_parity_passes_when_both_readers_agree_and_reports_a_differing_cell() -> None:
    """Paired: the identical result passes; a NULL written as a quoted empty string fails."""
    agreed = probe.p2a_parity(_run(_CSV), "wg-bake")
    assert agreed["passed"] and agreed["location_prefix"] == "bake"
    differing = probe.p2a_parity(_run(_CSV.replace(b"\n,", b'\n"",', 1)), "wg-bake")
    assert not differing["passed"]
    assert differing["differing_cells"] == [{"row": 0, "cell": 0, "paged": "None", "object": "''"}]


def test_p1_measures_every_term_through_the_real_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """At a fictional size, with the two render fixtures stubbed to fixed fictional peaks."""

    def retail(products: int) -> int:
        return 10 * products

    monkeypatch.setattr(probe, "measure_retail_render", retail)

    def record(campaigns: int) -> int:
        return 20 * campaigns

    monkeypatch.setattr(probe, "measure_campaign_record", record)
    measured = probe.run_p1(sizes=(200, 400), runs=1)
    # ADR-152: Retail renders at every size, never only those under a row ceiling.
    assert [
        item["rows"] for item in measured["render_samples"] if item["render"] == "retail_health"
    ] == [
        200,
        400,
    ]
    assert "retail_render_sizes" not in measured
    terms = measured["terms_bytes_per_row"]
    assert terms["B_raw"] > 0 and terms["B_shaped"] > 0 and terms["B_kept"] > 0
    assert (terms["B_render"], terms["B_record"]) == (10.0, 20.0)
    # ADR-152: the Campaigns bake renders at every size too.
    assert [
        item["rows"] for item in measured["render_samples"] if item["render"] == "campaigns_bake"
    ] == [200, 400]
    assert {sample["shape"] for sample in measured["samples"]} == {
        "campaigns_compare",
        "audiences_grouped",
    }


def test_the_derivation_takes_the_largest_candidate_that_fits_every_row() -> None:
    small = bakeability.BakeMemoryTerms(1, 1, 1, 1, 1, "fictional")
    large = bakeability.BakeMemoryTerms(10_000, 10_000, 1, 10_000, 1, "fictional")
    assert probe.derive(small, 1_024)["derived"] == 50_000
    derived = probe.derive(large, 1_024)
    assert derived["derived"] is None
    assert set(derived["candidates"]) == {str(item) for item in probe.CANDIDATES}
    assert all(not row["fits"] for row in derived["candidates"].values())


@pytest.mark.covers("ADR-152")
def test_sizes_are_comma_separated_positive_counts_defaulting_to_10k_and_100k() -> None:
    assert probe._sizes("10000,100000") == (10_000, 100_000)  # pyright: ignore[reportPrivateUsage]
    assert probe.P1_SIZES == (10_000, 100_000)
    for text in ("", "10k", "0,5"):
        with pytest.raises(Exception, match="--sizes"):
            probe._sizes(text)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.covers("ADR-152")
def test_each_p2_ranked_case_is_the_composed_statement_over_generated_facts() -> None:
    """Every value binds; the generated facts open the statement; no key list is built."""
    cases = probe.ranked_cases(keys=2_000)
    assert [case.name for case in cases] == [
        "retail_top_50_count_and_sums",
        "retail_movers_by_abs_change",
        "gateway_partitioned_positive",
        "gateway_withheld_population",
    ]
    for case in cases:
        composed = probe.ranked_case_sql(case)
        assert composed.sql.count("?") == len(composed.parameters)
        assert '"ranked" AS (SELECT' in composed.sql
        assert "amazon_asin:P000001" not in composed.sql
    top, movers, gateway, withheld = (probe.ranked_expected(case) for case in cases)
    assert (top[""]["count"], len(top[""]["keys"])) == (2_000, 50)
    assert set(top[""]["sums"]) == {"retail_revenue_shipped", "units_shipped"}
    assert len(movers[""]["keys"]) == 3 and movers[""]["count"] < 2_000
    assert set(gateway) == {"ch_a|ap_a", "ch_b|ap_b"}
    assert all(len(item["keys"]) == 10 and not item["suppressed"] for item in gateway.values())
    assert all(item["suppressed"] for item in withheld.values())
