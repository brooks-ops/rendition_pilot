import pytest
from fastapi import HTTPException

from backend.main import (
    CadDistrictInfoRequest,
    CadAdminRequest,
    CadOnboardingRequest,
    CadScheduleRequest,
    CadScheduleRowRequest,
    normalize_depreciation_factor,
    validate_cad_onboarding_payload,
    validate_cad_schedule_payload,
)


def test_cad_schedule_validation_accepts_valid_rows():
    schedule = CadScheduleRequest(
        schedule_name="9 Year BPP Schedule",
        schedule_type="Machinery & Equipment",
        schedule_years=9,
        rows=[
            CadScheduleRowRequest(year_number=1, depreciation_percent=90),
            CadScheduleRowRequest(year_number=2, depreciation_percent=80),
        ],
    )

    validate_cad_schedule_payload(schedule)
    assert normalize_depreciation_factor(90) == 0.9
    assert normalize_depreciation_factor(0.75) == 0.75


def test_cad_schedule_validation_requires_year_rows():
    schedule = CadScheduleRequest(
        schedule_name="Empty Schedule",
        schedule_type="Custom",
        schedule_years=5,
        rows=[],
    )

    with pytest.raises(HTTPException) as exc:
        validate_cad_schedule_payload(schedule)

    assert "at least one year row" in exc.value.detail


def test_cad_onboarding_validation_requires_cad_name_admin_email_and_schedule():
    request = CadOnboardingRequest(
        district=CadDistrictInfoRequest(cad_name="Example CAD"),
        admin=CadAdminRequest(email="admin@examplecad.org"),
        schedules=[
            CadScheduleRequest(
                schedule_name="5 Year",
                schedule_years=5,
                rows=[CadScheduleRowRequest(year_number=1, depreciation_percent=75)],
            )
        ],
    )

    validate_cad_onboarding_payload(request)


def test_cad_onboarding_validation_rejects_missing_schedule():
    request = CadOnboardingRequest(
        district=CadDistrictInfoRequest(cad_name="Example CAD"),
        admin=CadAdminRequest(email="admin@examplecad.org"),
        schedules=[],
    )

    with pytest.raises(HTTPException) as exc:
        validate_cad_onboarding_payload(request)

    assert "at least one depreciation schedule" in exc.value.detail
