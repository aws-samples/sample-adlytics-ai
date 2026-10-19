# SPDX-License-Identifier: MIT-0
"""The exposure fence is byte-identical in every pack that declares it (plan D16).

The fence is the text between `/* exposure-set start */` and `/* exposure-set end */`, which the
2026-10-09 probes compiled (`validation/sql/bo/exposure_fence.sql`, read here as text; runtime code
never imports `validation`). Packs 5, 6 and 7 and their variants declare it now; packs 10 and 11
join by declaring the same markers, and this test covers them with no change.

Pack 6 reads pack 5's window and outcome CTEs byte for byte; pack 7's window CTEs are pack 6's keyed
by (user_id, ad_product_type), and differ from them only in those key columns (design §5.5).
"""

from __future__ import annotations

import pathlib

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-101")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKS = ROOT / "contracts/amc/query-packs"
PROBE_FENCE = ROOT / "validation/sql/bo/exposure_fence.sql"
START = "/* exposure-set start */"
END = "/* exposure-set end */"


def fenced(text: str) -> str:
    """Return the text from the start marker to the end marker, both included, or raise."""
    if text.count(START) != 1 or text.count(END) != 1:
        raise ValueError("a fenced pack declares exactly one start and one end marker.")
    begin = text.index(START)
    end = text.index(END) + len(END)
    if end <= begin:
        raise ValueError("the fence's end marker comes before its start.")
    return text[begin:end]


def _declaring() -> dict[str, str]:
    return {
        path.name: path.read_text()
        for path in sorted(PACKS.glob("*.sql"))
        if START in path.read_text() or END in path.read_text()
    }


def test_every_pack_declaring_the_fence_carries_the_probe_compiled_text_byte_for_byte() -> None:
    probe = fenced(PROBE_FENCE.read_text())
    declaring = _declaring()
    assert sorted(declaring) == [
        "amc-exposure-frequency-channel-coarse.sql",
        "amc-exposure-frequency-channel.sql",
        "amc-exposure-frequency-platform-coarse.sql",
        "amc-exposure-frequency-platform.sql",
        "amc-exposure-groups-touch-only.sql",
        "amc-exposure-groups.sql",
        "amc-segment-buyer-profile-inmarket.sql",
        "amc-segment-buyer-profile-lifestyle.sql",
    ]
    for name, text in declaring.items():
        assert fenced(text) == probe, name
    # The fence holds its one campaign_country line (D16): the exact pack text, not the
    # structural check the Sandbox ran without it.
    assert probe.count("AND campaign_country = CUSTOM_PARAMETER('dsp_campaign_country')") == 1


def test_a_one_byte_drift_inside_the_fence_is_detected() -> None:
    """Paired control: the comparison is exact, so a changed space or a dropped line fails it."""
    probe = fenced(PROBE_FENCE.read_text())
    pack = (PACKS / "amc-exposure-groups.sql").read_text()
    drifted = pack.replace("impressions > 0 OR spend > 0", "impressions > 0 OR  spend > 0", 1)
    assert drifted != pack
    assert fenced(drifted) != probe
    country = "    AND campaign_country = CUSTOM_PARAMETER('dsp_campaign_country')\n"
    dropped = pack.replace(country, "")
    assert fenced(dropped) != probe
    # A text outside the fence may change freely: the window CTEs carry the expanded gap form.
    outside = pack.replace("GROUP BY g.exposure_source_set", "GROUP BY  g.exposure_source_set")
    assert fenced(outside) == probe
    with pytest.raises(ValueError, match="exactly one"):
        fenced(pack.replace(END, ""))


def test_the_pack_uses_the_probe_compiled_gap_form_and_never_the_unary_minus() -> None:
    """Design §5.5's sign-independent gap compiles only as ABS(SECONDS_BETWEEN(...)) (D16).

    Packs 10-11 read the fence and no window, so they carry no gap; every window pack does.
    """
    for name, text in _declaring().items():
        if not name.startswith("amc-segment-buyer-profile-"):
            assert text.count("ABS(SECONDS_BETWEEN(") >= 2, name
        assert "GAP_SECONDS(" not in text, name
        assert "-SECONDS_BETWEEN" not in text, name
        assert "--" not in text, name  # the client joins lines, so only /* */ comments


def _cte(text: str, name: str) -> str:
    """Return one CTE's text, from `name AS (` to the line closing it."""
    begin = text.index(f"\n{name} AS (") + 1
    end = text.index("\n)", begin) + len("\n)")
    return text[begin:end]


_WINDOW = ("firsts", "w_touches")
_KEYED = (
    ("SELECT user_id, MIN(touch_hour)", "SELECT user_id, ad_product_type, MIN(touch_hour)"),
    ("GROUP BY user_id\n)", "GROUP BY user_id, ad_product_type\n)"),
    (
        "ON f.user_id = t.user_id\n",
        "ON f.user_id = t.user_id AND f.ad_product_type = t.ad_product_type\n",
    ),
)
"""The only edits that turn pack 6's window CTEs into pack 7's: the key gains the ad product."""


def _window(text: str) -> str:
    return "\n".join(_cte(text, cte) for cte in _WINDOW)


def _keyed_by_ad_product(text: str) -> str:
    for old, new in _KEYED:
        if text.count(old) != 1:
            raise ValueError(f"{old!r} must appear exactly once.")
        text = text.replace(old, new)
    return text


def test_pack_6_reads_pack_5_s_window_and_outcome_ctes_byte_for_byte() -> None:
    pack5 = (PACKS / "amc-exposure-groups.sql").read_text()
    for name in (
        "amc-exposure-frequency-platform.sql",
        "amc-exposure-frequency-platform-coarse.sql",
    ):
        text = (PACKS / name).read_text()
        for cte in (*_WINDOW, "exposed", "grouped", "outcome_events", "outcomes"):
            assert _cte(text, cte) == _cte(pack5, cte), (name, cte)


def test_pack_7_s_window_ctes_differ_from_pack_6_s_only_in_the_key_columns() -> None:
    """Design §5.5: pack 7 keys each window by (user_id, ad_product_type); nothing else moves."""
    pack6 = (PACKS / "amc-exposure-frequency-platform.sql").read_text()
    for name in (
        "amc-exposure-frequency-channel.sql",
        "amc-exposure-frequency-channel-coarse.sql",
    ):
        text = (PACKS / name).read_text()
        assert _window(text) == _keyed_by_ad_product(_window(pack6)), name
        assert _window(text) != _window(pack6), name


def test_a_window_cte_drift_beyond_the_key_columns_is_detected() -> None:
    """Paired control: any other change to pack 7's window, even one byte, fails the comparison."""
    pack6 = (PACKS / "amc-exposure-frequency-platform.sql").read_text()
    pack7 = (PACKS / "amc-exposure-frequency-channel.sql").read_text()
    keyed = _keyed_by_ad_product(_window(pack6))
    assert _window(pack7) == keyed
    drifted = pack7.replace("< 1209600\n),\ngrouped_p", "<= 1209600\n),\ngrouped_p", 1)
    assert drifted != pack7
    assert _window(drifted) != keyed
    # Dropping a key column is detected too: the window would no longer be per ad product.
    unkeyed = pack7.replace(" AND f.ad_product_type = t.ad_product_type", "", 1)
    assert unkeyed != pack7
    assert _window(unkeyed) != keyed


def test_every_bucket_case_is_written_in_full_with_no_else_and_one_scheme() -> None:
    """Design §5.5: no ELSE, so a NULL bucket can only mean a shopper outside the fence."""
    fine = ("'1'", "'2'", "'3'", "'4'", "'5'", "'6'", "'7'", "'8'", "'9'", "'10_plus'")
    coarse = ("'1'", "'2'", "'3'", "'4_6'", "'7_9'", "'10_plus'")
    for pack in ("platform", "channel"):
        for scheme, labels in (("", fine), ("-coarse", coarse)):
            text = (PACKS / f"amc-exposure-frequency-{pack}{scheme}.sql").read_text()
            case = text[text.index("CASE WHEN g.imps = 1") : text.index("AS frequency_bucket")]
            assert "ELSE" not in case, (pack, scheme)
            found = sorted((label for label in {*fine, *coarse} if label in case), key=case.index)
            assert tuple(found) == labels, (pack, scheme)
            literal = "'coarse' AS bucket_scheme" if scheme else "'fine' AS bucket_scheme"
            assert text.count(literal) == 1, (pack, scheme)


def test_pack_7_ships_without_its_credit_branch() -> None:
    """Plan D12: criterion 6b wasn't run, so no credit rows, model keys or credited measures."""
    for name in (
        "amc-exposure-frequency-channel.sql",
        "amc-exposure-frequency-channel-coarse.sql",
    ):
        text = (PACKS / name).read_text()
        assert "UNION ALL" in text  # the fence's two touch tables, and nothing else
        assert text.count("UNION ALL") == 1, name
        for absent in ("credit", "attribution_model_key", "conversion_id AS", "'linear_v1'"):
            assert absent not in text, (name, absent)
        assert text.count("'window' AS row_kind") == 1, name
