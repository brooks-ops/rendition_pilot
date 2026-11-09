"""Local disposable PostgreSQL only; requires returned-mail migration/fixture."""
import json,os,re,subprocess,uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
DB=os.environ.get('MAILING_TEST_DATABASE','')
pytestmark=pytest.mark.skipif(not re.fullmatch(r'rp_mailing_phase1_test_[a-zA-Z0-9_]+',DB),reason='Requires explicit local disposable mailing database')

def sql(statement,check=True):
    return subprocess.run(['psql','-h','/tmp','-d',DB,'-v','ON_ERROR_STOP=1','-Atq'],input=statement,text=True,capture_output=True,check=check)

@pytest.fixture
def county():
    d=str(uuid.uuid4());j=str(uuid.uuid4());actor=str(uuid.uuid4())
    sql(f"insert into districts(id,name,slug) values('{d}','Return concurrency','{d}');insert into jurisdictions(id,district_id,name,slug,county_name) values('{j}','{d}','Return','{j}','Test');")
    try:yield d,j,actor
    finally:sql(f"delete from returned_mail_review_events where district_id='{d}';delete from returned_mail_documents where district_id='{d}';delete from bpp_work_claims where district_id='{d}';delete from bpp_intelligence_items where district_id='{d}';delete from jurisdictions where id='{j}';delete from districts where id='{d}';")

def payload(d,char='a'):
    return json.dumps(dict(account_number='P123456',filename='test.pdf',relative_path='P123456/test.pdf',sha256=char*64,
        storage_path=d+'/P123456/'+char*64+'.pdf',byte_count=100,content_type='application/pdf',page_count=1,
        ocr_text='RETURN TO SENDER',ocr_status='READ',ocr_warnings=[],suggested_reason='RETURN_TO_SENDER',printed_accounts=[]))

def test_concurrent_duplicate_scans_create_one_document(county):
    d,j,actor=county;barrier=Barrier(2)
    def write():
        barrier.wait()
        result=sql(f"begin;select returned_mail_commit('{d}','{j}','{actor}',$p${payload(d)}$p$);select pg_sleep(0.2);commit;")
        return json.loads(result.stdout.splitlines()[0])
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:write(),range(2)))
    assert sorted(r['duplicate'] for r in results)==[False,True]
    assert sql(f"select count(*) from returned_mail_documents where district_id='{d}'").stdout.strip()=='1'

def test_new_scan_racing_completion_cannot_hide_pending_evidence(county):
    d,j,actor=county
    first=json.loads(sql(f"select returned_mail_commit('{d}','{j}','{actor}',$p${payload(d)}$p$)").stdout)
    item=first['item_id'];doc=first['document_id']
    sql(f"insert into bpp_work_claims(district_id,work_key,actor_id) values('{d}','account:P123456','{actor}');select returned_mail_review('{d}','{actor}','{doc}',0,'{{\"review_status\":\"REVIEWED\",\"return_reason\":\"UNKNOWN\"}}');")
    barrier=Barrier(2)
    def complete():
        barrier.wait()
        return sql(f"begin;select returned_mail_resolve('{d}','{actor}','{item}','OTHER','Reviewed');select pg_sleep(0.2);commit;",check=False)
    def arrive():
        barrier.wait()
        return sql(f"begin;select returned_mail_commit('{d}','{j}','{actor}',$p${payload(d,'b')}$p$);select pg_sleep(0.2);commit;")
    with ThreadPoolExecutor(max_workers=2) as pool:
        one=pool.submit(complete);two=pool.submit(arrive);completion=one.result();arrival=two.result()
    assert arrival.returncode==0
    assert completion.returncode==0 or 'Review each returned-mail scan' in completion.stderr
    assert sql(f"select count(*) from returned_mail_documents d join bpp_intelligence_items i on i.id=d.intelligence_item_id where d.district_id='{d}' and d.review_status='PENDING' and i.status in ('RESOLVED','DISMISSED')").stdout.strip()=='0'
