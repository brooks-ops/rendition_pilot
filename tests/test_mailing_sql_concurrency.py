"""Opt-in integration checks against LOCAL disposable PostgreSQL only.

MAILING_TEST_DATABASE=rp_mailing_phase1_test_fresh_20261102 pytest -q tests/test_mailing_sql_concurrency.py
Prepare with tests/sql/mailing_phase1_fixture.sql and the phase-one migration.
"""
import json
import os
import re
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

DB=os.environ.get('MAILING_TEST_DATABASE','')
pytestmark=pytest.mark.skipif(not re.fullmatch(r'rp_mailing_phase1_test_[a-zA-Z0-9_]+',DB),reason='Requires explicitly named local disposable mailing database')


def sql(statement):
    result=subprocess.run(['psql','-h','/tmp','-d',DB,'-v','ON_ERROR_STOP=1','-Atq'],input=statement,text=True,capture_output=True,check=True)
    return result.stdout.strip()


@pytest.fixture
def county():
    district=str(uuid.uuid4());jurisdiction=str(uuid.uuid4())
    sql(f"insert into districts(id,name,slug) values('{district}','Concurrent mailing test','{district}'); insert into jurisdictions(id,district_id,name,slug,county_name) values('{jurisdiction}','{district}','Concurrent test','{jurisdiction}','Test');")
    try:yield district,jurisdiction
    finally:
        sql(f"delete from mailing_comparison_states where jurisdiction_id='{jurisdiction}';delete from bpp_intelligence_items where jurisdiction_id='{jurisdiction}';delete from mailing_address_observations where jurisdiction_id='{jurisdiction}';delete from mailing_scan_runs where jurisdiction_id='{jurisdiction}';delete from jurisdictions where id='{jurisdiction}';delete from districts where id='{district}';")


def test_simultaneous_scans_cannot_create_duplicate_open_comparisons(county):
    district,jurisdiction=county
    payload=json.dumps(dict(district_id=district,source_taxpayer_id='T1',classification='LIKELY_MAILING_ADDRESS_CHANGE',
        priority='HIGH',confidence='HIGH',matched_record_id=str(uuid.uuid4()),matched_account_number='P1',
        mailing_address_current='100 MAIN ST',mailing_address_observed='200 MAIN ST',evidence={'comparison_version':2}))
    barrier=Barrier(2)
    def write():
        barrier.wait()
        return sql(f"begin;select mailing_save_comparison('{jurisdiction}','T1:P1','same',true,$p${payload}$p$);select pg_sleep(0.2);commit;")
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:write(),range(2)))
    actions=[json.loads(r.splitlines()[0])['action'] for r in results]
    assert sorted(actions)==['CREATED','UPDATED']
    assert sql(f"select count(*) from bpp_intelligence_items where jurisdiction_id='{jurisdiction}' and status in ('NEW','IN_REVIEW')")=='1'


def test_simultaneous_observations_are_not_duplicated(county):
    district,jurisdiction=county
    payload=json.dumps(dict(jurisdiction_id=jurisdiction,account_identifier_type='comptroller_taxpayer',
        account_identifier='T1',source='test',normalized_full_address='100 MAIN STREET',raw_address_line='100 Main St'))
    barrier=Barrier(2)
    def write():
        barrier.wait()
        return sql(f"begin;select mailing_record_observation($p${payload}$p$);select pg_sleep(0.2);commit;")
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:write(),range(2)))
    assert sorted(json.loads(r.splitlines()[0])['added'] for r in results)==[False,True]
    assert sql(f"select count(*) from mailing_address_observations where jurisdiction_id='{jurisdiction}'")=='1'
