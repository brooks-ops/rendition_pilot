from __future__ import annotations

import pytest

from app.comptroller import admin, intelligence, service
from tests.comptroller_fakes import FakeSupabase


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    monkeypatch.setattr(service, "_request_json", fake.request_json)
    monkeypatch.setattr(intelligence, "_request_json", fake.request_json)
    monkeypatch.setattr(admin, "_request_json", fake.request_json)
    return fake


def add_intelligence_item(fake_supabase, **overrides):
    row = {
        "id": f"intel-{len(fake_supabase.intelligence_items) + 1}",
        "district_id": "district-1",
        "jurisdiction_id": "jur-lubbock",
        "signal_type": "new_business",
        "source": "tx_comptroller_open_data",
        "source_record_id": "1:1",
        "status": "NEW",
        "classification": "NO_ACCOUNT_FOUND",
        "priority": "HIGH",
        "confidence": "UNMATCHED",
        "is_ambiguous": False,
        "business_name": "JOE'S SPORTS BAR",
        "legal_name": "JOE LAMONT DOLLAR",
        "source_city": "LUBBOCK",
        "first_detected_at": "2026-08-15T00:00:00+00:00",
        "match_reason": "No RenditionPilot record met the minimum name-similarity threshold.",
        "match_signals": {"business_dba_name": "NO MATCH"},
        "recommended_action": "Review for possible new BPP account.",
        "created_at": "2026-08-15T00:00:00+00:00",
    }
    row.update(overrides)
    fake_supabase.intelligence_items[row["id"]] = row
    return row


def add_closure_review(fake_supabase, **overrides):
    row = {
        "id": f"review-{len(fake_supabase.closure_reviews) + 1}",
        "district_id": "district-1",
        "comptroller_business_name": "OLD RESTAURANT",
        "comptroller_city": "LUBBOCK",
        "workflow_status": "PENDING_REVIEW",
        "match_confidence": "UNMATCHED",
        "match_ambiguous": False,
        "first_detected_at": "2026-08-10T00:00:00+00:00",
        "created_at": "2026-08-10T00:00:00+00:00",
    }
    row.update(overrides)
    fake_supabase.closure_reviews[row["id"]] = row
    return row


# -- unified listing -----------------------------------------------------


def test_queue_merges_both_sources(fake_supabase):
    add_intelligence_item(fake_supabase)
    add_closure_review(fake_supabase)

    items = intelligence.list_intelligence_queue("district-1")

    signal_types = {item.signal_type for item in items}
    assert signal_types == {"new_business", "sales_tax_inactive"}


def test_items_from_both_sources_show_a_human_readable_source_label(fake_supabase):
    """Spec: "say where it was found -- helps with discovery." Both signal
    types come from the same Comptroller feed today, so both must show the
    same label -- new_business reads its own `source` column;
    comptroller_closure_reviews has no such column (it's only ever had one
    source) so intelligence.py supplies the known constant instead."""

    add_intelligence_item(fake_supabase)
    add_closure_review(fake_supabase)

    items = intelligence.list_intelligence_queue("district-1")

    for item in items:
        assert item.source == "tx_comptroller_open_data"
        assert item.source_label == "Texas Comptroller (Sales Tax)"


def test_unmapped_source_falls_back_to_the_raw_value_not_blank(fake_supabase):
    add_intelligence_item(fake_supabase, source="future_dba_filing_feed")
    items = intelligence.list_intelligence_queue("district-1")
    assert items[0].source_label == "future_dba_filing_feed"


def test_mailing_address_change_items_appear_in_the_unified_queue(fake_supabase):
    add_intelligence_item(
        fake_supabase, signal_type="mailing_address_change", matched_record_id="pid-1", classification="LIKELY_MAILING_ADDRESS_CHANGE",
        mailing_address_current="123 MAIN STREET LUBBOCK TX 79401", mailing_address_observed="PO BOX 900 LUBBOCK TX 79401",
    )

    all_items = intelligence.list_intelligence_queue("district-1")
    filtered = intelligence.list_intelligence_queue("district-1", signal_type="mailing_address_change")

    assert {i.signal_type for i in all_items} == {"mailing_address_change"}
    assert len(filtered) == 1
    assert filtered[0].mailing_address_current == "123 MAIN STREET LUBBOCK TX 79401"
    assert filtered[0].mailing_address_observed == "PO BOX 900 LUBBOCK TX 79401"


def test_queue_scoped_to_district(fake_supabase):
    add_intelligence_item(fake_supabase, district_id="district-1")
    add_intelligence_item(fake_supabase, district_id="district-other")

    items = intelligence.list_intelligence_queue("district-1")

    assert len(items) == 1
    assert items[0].raw["district_id"] == "district-1"


def test_filter_by_signal_type(fake_supabase):
    add_intelligence_item(fake_supabase)
    add_closure_review(fake_supabase)

    items = intelligence.list_intelligence_queue("district-1", signal_type="new_business")

    assert len(items) == 1
    assert items[0].signal_type == "new_business"


def test_filter_by_status(fake_supabase):
    add_intelligence_item(fake_supabase, status="NEW")
    add_intelligence_item(fake_supabase, status="RESOLVED")

    items = intelligence.list_intelligence_queue("district-1", status="NEW")

    assert len(items) == 1
    assert items[0].status == "NEW"


def test_closure_review_workflow_status_maps_to_shared_lifecycle(fake_supabase):
    add_closure_review(fake_supabase, workflow_status="CONFIRMED_CLOSURE")
    add_closure_review(fake_supabase, workflow_status="NOT_CLOSED")
    add_closure_review(fake_supabase, workflow_status="PENDING_REVIEW")

    items = intelligence.list_intelligence_queue("district-1")
    statuses = sorted(item.status for item in items)

    assert statuses == ["DISMISSED", "NEW", "RESOLVED"]


def test_filter_by_city_case_insensitive(fake_supabase):
    add_intelligence_item(fake_supabase, source_city="Lubbock")

    items = intelligence.list_intelligence_queue("district-1", city="LUBBOCK")

    assert len(items) == 1


# -- summary counts -----------------------------------------------------


def test_summary_counts(fake_supabase):
    add_intelligence_item(fake_supabase, status="NEW", priority="HIGH")
    add_intelligence_item(fake_supabase, status="IN_REVIEW", priority="MEDIUM")
    add_closure_review(fake_supabase, workflow_status="CONFIRMED_CLOSURE")
    add_closure_review(fake_supabase, workflow_status="PENDING_REVIEW")

    summary = intelligence.get_queue_summary("district-1")

    assert summary["new"] == 2  # 1 bpp_intelligence_items NEW + 1 closure PENDING_REVIEW->NEW
    assert summary["high_priority"] == 1
    assert summary["needs_review"] == 1
    assert summary["resolved"] == 1
    assert summary["total"] == 4


# -- actions: investigate/resolve/dismiss route to the right table -----------


def test_investigate_intelligence_item(fake_supabase):
    row = add_intelligence_item(fake_supabase)

    item = intelligence.investigate_item(intelligence.SOURCE_TABLE_INTELLIGENCE, row["id"], assigned_to="user-1")

    assert item.status == "IN_REVIEW"
    assert fake_supabase.intelligence_items[row["id"]]["assigned_to"] == "user-1"


def test_investigate_closure_review(fake_supabase):
    row = add_closure_review(fake_supabase)

    item = intelligence.investigate_item(intelligence.SOURCE_TABLE_CLOSURE_REVIEW, row["id"])

    assert fake_supabase.closure_reviews[row["id"]]["workflow_status"] == "OTHER_NEEDS_RESEARCH"
    assert item.status == "IN_REVIEW"


def test_resolve_intelligence_item_sets_resolution(fake_supabase):
    row = add_intelligence_item(fake_supabase)

    item = intelligence.resolve_item(
        intelligence.SOURCE_TABLE_INTELLIGENCE, row["id"],
        resolution="NEW_ACCOUNT_NEEDED", resolution_notes="Confirmed new business", reviewed_by="user-1",
    )

    assert item.status == "RESOLVED"
    assert fake_supabase.intelligence_items[row["id"]]["resolution"] == "NEW_ACCOUNT_NEEDED"
    assert fake_supabase.intelligence_items[row["id"]]["resolution_notes"] == "Confirmed new business"
    assert fake_supabase.intelligence_items[row["id"]]["reviewed_by"] == "user-1"
    assert fake_supabase.intelligence_items[row["id"]]["reviewed_at"] is not None


def test_resolve_closure_review_maps_resolution_to_workflow_status(fake_supabase):
    row = add_closure_review(fake_supabase)

    intelligence.resolve_item(
        intelligence.SOURCE_TABLE_CLOSURE_REVIEW, row["id"],
        resolution="BUSINESS_CLOSED", resolution_notes="Verified closed", reviewed_by="user-1",
    )

    assert fake_supabase.closure_reviews[row["id"]]["workflow_status"] == "CONFIRMED_CLOSURE"
    assert fake_supabase.closure_reviews[row["id"]]["reviewer_notes"] == "Verified closed"


def test_dismiss_intelligence_item(fake_supabase):
    row = add_intelligence_item(fake_supabase)

    item = intelligence.dismiss_item(intelligence.SOURCE_TABLE_INTELLIGENCE, row["id"], resolution_notes="Not real", reviewed_by="user-1")

    assert item.status == "DISMISSED"
    assert fake_supabase.intelligence_items[row["id"]]["resolution"] == "FALSE_MATCH"


def test_dismiss_closure_review_maps_to_not_closed(fake_supabase):
    row = add_closure_review(fake_supabase)

    intelligence.dismiss_item(intelligence.SOURCE_TABLE_CLOSURE_REVIEW, row["id"], resolution_notes=None, reviewed_by="user-1")

    assert fake_supabase.closure_reviews[row["id"]]["workflow_status"] == "NOT_CLOSED"


def test_resolving_never_touches_appraisal_fields(fake_supabase):
    """The whole point of this queue: only its own status/resolution/notes/
    reviewer fields are ever written -- never property value, ownership,
    account status, BPP records, or exemptions."""

    row = add_intelligence_item(fake_supabase)
    allowed_keys = {"status", "resolution", "resolution_notes", "reviewed_by", "reviewed_at", "assigned_to"}

    intelligence.resolve_item(
        intelligence.SOURCE_TABLE_INTELLIGENCE, row["id"], resolution="OTHER", resolution_notes="note", reviewed_by="u1",
    )

    patch_calls = [c for c in fake_supabase.calls if c["method"] == "PATCH" and c["url"].endswith("bpp_intelligence_items")]
    assert patch_calls
    for call in patch_calls:
        assert set(call["json_payload"].keys()) <= allowed_keys


def test_unknown_source_table_raises(fake_supabase):
    with pytest.raises(intelligence.IntelligenceQueueError):
        intelligence.get_intelligence_item("not_a_real_table", "x")



def test_inactive_queue_orders_full_matches_before_unmatched(fake_supabase):
    weak=add_closure_review(fake_supabase,created_at='2026-10-23')
    full=add_closure_review(fake_supabase,created_at='2026-01-01',match_confidence='HIGH',matched_account_id='cad-2',matched_account_number='P2',match_reason='Imported CAD personal-property account: situs address and strong CAD owner/business/DBA name match')
    items=intelligence.list_intelligence_queue('district-1',signal_type='sales_tax_inactive')
    assert items[0].id==full['id'] and items[0].priority=='HIGH'
    assert items[0].priority_reason=='PID + address + DBA/owner match'
    assert items[-1].id==weak['id'] and items[-1].priority=='LOW'


def test_new_business_priority_and_order_remain_unchanged(fake_supabase):
    newest=add_intelligence_item(fake_supabase,created_at='2026-10-23',priority='HIGH')
    older=add_intelligence_item(fake_supabase,created_at='2026-01-01',priority='LOW')
    items=intelligence.list_intelligence_queue('district-1',signal_type='new_business')
    assert [(i.id,i.priority) for i in items]==[(newest['id'],'HIGH'),(older['id'],'LOW')]



def test_mailing_matched_account_with_updated_address_is_highest(fake_supabase):
    linked=add_intelligence_item(fake_supabase,signal_type='mailing_address_change',priority='MEDIUM',confidence='HIGH',classification='LIKELY_MAILING_ADDRESS_CHANGE',mailing_address_current='100 OLD ST',mailing_address_observed='200 NEW ST',matched_account_number='P1',matched_record_id='pid-1',created_at='2026-01-01')
    unmatched=add_intelligence_item(fake_supabase,signal_type='mailing_address_change',priority='HIGH',confidence='UNMATCHED',created_at='2026-10-23')
    items=intelligence.list_intelligence_queue('district-1',signal_type='mailing_address_change')
    assert [i.id for i in items]==[linked['id']]
    assert [i.priority for i in items]==['HIGH']
    assert 'updated mailing' in items[0].priority_reason
    assert unmatched['id'] not in {i.id for i in intelligence.list_intelligence_queue('district-1')}


def test_mailing_linked_account_without_update_is_not_high(fake_supabase):
    add_intelligence_item(fake_supabase,signal_type='mailing_address_change',priority='HIGH',matched_record_id='pid-1',confidence='HIGH')
    assert intelligence.list_intelligence_queue('district-1',signal_type='mailing_address_change')==[]


def test_possible_mailing_match_stays_medium(fake_supabase):
    add_intelligence_item(fake_supabase,signal_type='mailing_address_change',priority='HIGH',matched_record_id='pid-1',confidence='MEDIUM',classification='POSSIBLE_MAILING_ADDRESS_CHANGE',mailing_address_current='100 OLD ST',mailing_address_observed='200 NEW ST')
    assert intelligence.list_intelligence_queue('district-1',signal_type='mailing_address_change')[0].priority=='MEDIUM'


def test_sales_tax_ids_and_location_numbers_are_preserved_for_both_sources(fake_supabase):
    add_intelligence_item(fake_supabase,source_taxpayer_id='01000000001',source_location_number='00001')
    add_intelligence_item(fake_supabase,signal_type='mailing_address_change',source_taxpayer_id='01000000002',matched_record_id='pid-2',classification='LIKELY_MAILING_ADDRESS_CHANGE',mailing_address_current='100 OLD ST',mailing_address_observed='200 NEW ST')
    add_closure_review(fake_supabase,comptroller_taxpayer_id='01000000003',comptroller_location_number='00003')
    items=intelligence.list_intelligence_queue('district-1')
    by_tax={i.sales_tax_id:i for i in items}
    assert set(by_tax)=={'01000000001','01000000002','01000000003'}
    assert by_tax['01000000001'].sales_tax_location_number=='00001'
    assert by_tax['01000000003'].sales_tax_location_number=='00003'
    assert by_tax['01000000002'].sales_tax_location_number is None
    for i in items:
        detail=intelligence.get_intelligence_item(i.source_table,i.id)
        assert detail.sales_tax_id==i.sales_tax_id


def test_missing_sales_tax_id_is_not_invented(fake_supabase):
    add_intelligence_item(fake_supabase)
    add_closure_review(fake_supabase)
    assert all(i.sales_tax_id is None for i in intelligence.list_intelligence_queue('district-1'))


def test_all_signals_sort_by_priority_before_recency(fake_supabase):
    newest_low=add_intelligence_item(fake_supabase,created_at='2026-10-28',priority='LOW')
    old_high=add_intelligence_item(fake_supabase,created_at='2026-01-01',priority='HIGH')
    medium=add_intelligence_item(fake_supabase,created_at='2026-10-27',priority='MEDIUM')
    dba=add_intelligence_item(fake_supabase,created_at='2026-10-29',signal_type='county_clerk_dba',priority='HIGH',evidence={'filing':{'assumed_name':'UNKNOWN SHOP'}})
    items=intelligence.list_intelligence_queue('district-1')
    assert [i.priority for i in items]==['HIGH','MEDIUM','LOW','LOW']
    assert items[0].id==old_high['id'] and items[1].id==medium['id']
    assert next(i for i in items if i.id==dba['id']).priority=='LOW'


@pytest.mark.parametrize('closure',[False,True])
def test_reopen_preserves_previous_completion_and_followup_notes(fake_supabase,closure):
    if closure:
        row=add_closure_review(fake_supabase,workflow_status='CONFIRMED_CLOSURE',reviewer_notes='Verified closure',reviewed_by='reviewer',reviewed_at='2026-10-28')
        table=intelligence.SOURCE_TABLE_CLOSURE_REVIEW
    else:
        row=add_intelligence_item(fake_supabase,status='RESOLVED',resolution='ACCOUNT_EXISTS',resolution_notes='Verified account',reviewed_by='reviewer',reviewed_at='2026-10-28')
        table=intelligence.SOURCE_TABLE_INTELLIGENCE
    original=intelligence.get_intelligence_item(table,row['id'])
    reopened=intelligence.reopen_item(original,'appraiser@cad.org')
    assert reopened.status=='NEW' and original.resolution in reopened.resolution_notes
    assert 'reviewer' in reopened.resolution_notes and '2026-10-28' in reopened.resolution_notes
    assert '[Reopened review]' in reopened.resolution_notes
    followup=intelligence.retain_review_history(reopened,'Corrected the outcome')
    assert original.resolution_notes in followup and 'Corrected the outcome' in followup
    with pytest.raises(intelligence.IntelligenceQueueError):intelligence.reopen_item(reopened,'appraiser@cad.org')
