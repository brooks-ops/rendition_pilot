"""Local disposable PostgreSQL checks; never cloud data."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from tests.test_mailing_sql_concurrency import sql,DB
import re
pytestmark=pytest.mark.skipif(not re.fullmatch(r'rp_mailing_phase1_test_[a-zA-Z0-9_]+',DB),reason='Requires local disposable mailing database')

@pytest.fixture
def allocation():
    d,j,a,b=(str(uuid.uuid4()) for _ in range(4))
    roster=json.dumps([{'id':a},{'id':b}])
    sql(f"insert into districts(id,name,slug) values('{d}','Allocation concurrency','{d}');insert into jurisdictions(id,district_id,name,slug,county_name) values('{j}','{d}','Allocation','{j}','Test');insert into district_users(district_id,user_id,email,role) values('{d}','{a}','a@example.invalid','admin'),('{d}','{b}','b@example.invalid','appraiser');select bpp_configure_assignments('{d}','{a}',true,'{roster}');")
    try:yield d,j,a,b
    finally:sql(f"delete from bpp_assignment_events where district_id='{d}';delete from bpp_assignment_settings where district_id='{d}';delete from bpp_work_claims where district_id='{d}';delete from comptroller_closure_reviews where district_id='{d}';delete from bpp_intelligence_items where district_id='{d}';delete from district_users where district_id='{d}';delete from jurisdictions where id='{j}';delete from districts where id='{d}';")


def test_concurrent_new_accounts_are_balanced(allocation):
    d,j,a,b=allocation;barrier=Barrier(2)
    def insert(number):
        barrier.wait()
        return sql(f"insert into bpp_intelligence_items(jurisdiction_id,district_id,signal_type,source,source_record_id,classification,matched_account_number) values('{j}','{d}','new_business','concurrency','{number}','TEST','P{number}');")
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(insert,[1,2]))
    assert sql(f"select count(distinct assigned_to) from bpp_intelligence_items where district_id='{d}'")=='2'


def test_concurrent_related_sources_keep_one_owner(allocation):
    d,j,a,b=allocation;barrier=Barrier(2)
    def insert(source):
        barrier.wait()
        if source=='closure':return sql(f"insert into comptroller_closure_reviews(district_id,matched_account_number) values('{d}','P1');")
        return sql(f"insert into bpp_intelligence_items(jurisdiction_id,district_id,signal_type,source,source_record_id,classification,matched_account_number) values('{j}','{d}','new_business','concurrency','1','TEST','P1');")
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(insert,['bpp','closure']))
    assert sql(f"select count(distinct assigned_to) from bpp_active_assignment_work where district_id='{d}'")=='1'
