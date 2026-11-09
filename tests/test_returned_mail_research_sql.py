"""Opt-in LOCAL database integration checks; never uses cloud credentials."""
import json,re,uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from tests.test_returned_mail_concurrency import sql,DB,payload,county
pytestmark=pytest.mark.skipif(not re.fullmatch(r'rp_mailing_phase1_test_[a-zA-Z0-9_]+',DB),reason='Requires explicit local disposable mailing database')


def test_research_sql_state_machine_and_permissions():
    sql((Path(__file__).parent/'sql/returned_mail_research.sql').read_text())


def test_concurrent_workers_cannot_research_same_scan(county):
    d,j,actor=county
    result=json.loads(sql(f"select returned_mail_commit('{d}','{j}','{actor}',$p${payload(d)}$p$);").stdout.strip())
    document=result['document_id'];barrier=Barrier(2)
    def claim():
        barrier.wait()
        return sql(f"begin;select returned_mail_claim_research('{d}','{document}');select pg_sleep(0.1);commit;").stdout.splitlines()[0]
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:claim(),range(2)))
    assert sum(bool(x and x!='null') for x in results)==1
    assert sql(f"select count(*) from returned_mail_research_runs where document_id='{document}'").stdout.strip()=='1'
