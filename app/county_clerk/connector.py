"""Provider contract. Manual PDF import is enabled; unattended retrieval is not."""
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
    def retrieve_window(self,start,end):
        # Public manual viewing is not authorization for unattended collection.
        raise RetrievalNotAuthorized('Unattended EagleWeb retrieval is disabled. An approved access method, authorization and integration tests are required first. Import a printable-results PDF instead.')
