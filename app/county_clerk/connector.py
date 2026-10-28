"""County-specific source contract and operator opt-in."""
from datetime import date,timedelta
from urllib.parse import urlparse

class RetrievalNotAuthorized(RuntimeError):pass

def next_window(source,*,through=None):
    through=through or date.today()
    last=source.get('last_complete_recording_date')
    if not last:return {'start':None,'end':through.isoformat(),'requires_initial_range':True}
    start=date.fromisoformat(last)-timedelta(days=source.get('overlap_days',7))
    return {'start':start.isoformat(),'end':through.isoformat(),'requires_initial_range':False}

class EagleWebConnector:
    def __init__(self,source):self.source=source
    def record_reference(self,url):
        source=urlparse(self.source['source_url']);target=urlparse(url or '')
        return url if target.scheme=='https' and target.hostname==source.hostname and target.path.endswith('/viewDoc.jsp') else None
    def retrieve_window(self,start,end,directory=None,*,heartbeat=None):
        from .public_client import PublicClient
        if not public_enabled(self.source):
            raise RetrievalNotAuthorized('Public collection is not enabled for this county.')
        if directory is None:raise ValueError('A private capture directory is required')
        from .detail_cache import load
        return PublicClient(heartbeat=heartbeat).collect(start,end,directory,cached=load(self.source,start,end))


def public_enabled(source):
    return bool(source.get('automation_enabled') and source.get('automation_authorized')
                and source.get('operator_authorized_at') and source.get('operator_authorization_basis')
                and source.get('retrieval_method')=='public_guest_html'
                and source.get('county')=='LUBBOCK' and source.get('state')=='TX'
                and source.get('source_url')=='https://erecord.lubbockcounty.gov/recorder/web/splash.jsp')
