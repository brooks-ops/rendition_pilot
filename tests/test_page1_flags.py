from app.targeted_parser import TargetedRenditionParser


def test_parse_page_1_flags_detects_section_3_and_section_5_checked_boxes():
    text = """
    SECTION 3: Affirmation of Prior Year Rendition
    ☑ By checking this box, I affirm that the information contained in the most recent rendition statement filed in
    SECTION 4: Business Information
    SECTION 5: Market Value
    Select your property's total market value:
    Under $20,000
    ☑ $20,000 or more
    Select your property's total market value:
    $125,000 or less ☑ More than $125,000
    SECTION 6: Affirmation and Signature
    """

    result = TargetedRenditionParser().parse_page_1_flags(text)

    assert result["section_3_present"] is True
    assert result["section_3_prior_year_checked"] is True
    assert result["section_5_present"] is True
    assert result["section_5_under_20k_checked"] is False
    assert result["section_5_20k_or_more_checked"] is True
    assert result["section_5_125k_or_less_checked"] is False
    assert result["section_5_more_than_125k_checked"] is True
