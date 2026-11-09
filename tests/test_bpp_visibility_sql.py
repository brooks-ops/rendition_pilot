"""Opt-in local verification of the direct-table bypass prevention."""
import re
import pytest
from tests.test_mailing_sql_concurrency import sql,DB
pytestmark=pytest.mark.skipif(not re.fullmatch(r'rp_mailing_phase1_test_[a-zA-Z0-9_]+',DB),reason='Requires explicit local disposable mailing database')


def test_intelligence_tables_require_backend_service_role():
    result=sql("""select c.relname,c.relrowsecurity,
      has_table_privilege('anon',c.oid,'SELECT'),has_table_privilege('authenticated',c.oid,'SELECT'),
      has_table_privilege('authenticated',c.oid,'UPDATE'),has_table_privilege('service_role',c.oid,'SELECT')
      from pg_class c join pg_namespace n on n.oid=c.relnamespace
      where n.nspname='public' and c.relname in ('bpp_intelligence_items','comptroller_closure_reviews') order by c.relname;""")
    assert result.splitlines()==['bpp_intelligence_items|t|f|f|f|t','comptroller_closure_reviews|t|f|f|f|t']
