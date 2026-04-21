from app.pipeline import _parse_money
from app.targeted_parser import TargetedRenditionParser


def test_parse_money_keeps_cents_and_repairs_split_leading_digit():
    assert _parse_money("$ 1 84,724.43") == 184724.43
    assert _parse_money("$ 9,000.00") == 9000.0
    assert _parse_money("34,798.73") == 34798.73


def test_attachment_summary_uses_labeled_total_not_largest_bad_parse():
    text = """
    Lubbock Office Good Faith Estimate of Market Value
    Rheometer $ 4,500.00
    200 Cubic Ft. Ribbon Blender $ 9,000.00
    Ram 1500 2019 - 4x4 Crew Cab - OM $ 30,250.00
    16' Bumper Hitch Parts Trailer VIN...0467 $ 7,175.70
    34' Racing Trailer VIN...2882 $ 9,000.00
    Modified Trailer w/Blower S/N...2305 $ 34,798.73
    Excel PCX $ 90,000.00
    Total Fixed Assets $ 1 84,724.43
    """

    result = TargetedRenditionParser().parse_attachment_summary([text])

    assert result["attachment_summary_present"] is True
    assert result["best_attachment_total"] == 184724.43
    assert 9000000.0 not in result["attachment_total_candidates"]
