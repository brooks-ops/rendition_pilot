"""Assignment visibility for authenticated BPP reviewers; admin scope stays district-wide."""
from app.comptroller import intelligence


class AssignmentAccessError(ValueError):
    pass


def assignment_filter(context, requested=None):
    if context.role=='admin':
        return context.user_id if requested=='ME' else requested
    if not context.user_id:
        raise AssignmentAccessError('A signed-in appraiser is required.')
    # Caller-supplied filters never widen a regular appraiser's scope.
    return context.user_id


def can_view_item(context,item):
    return item.district_id==context.district_id and (context.role=='admin' or bool(context.user_id and item.raw.get('assigned_to')==context.user_id))


def account_evidence_allowed(context,account,document_item_id):
    """Own evidence and historical scans supporting a currently assigned account.

    Previous work on an account does not grant access to someone else's new
    assignment. Editing still needs the scan's own item and reservation.
    """
    if context.role=='admin':return True
    if not context.user_id:return False
    if document_item_id:
        item=intelligence.get_intelligence_item(intelligence.SOURCE_TABLE_INTELLIGENCE,document_item_id)
        if item and can_view_item(context,item):return True
    from app.bpp_assignments import request
    rows=request('GET','bpp_active_assignment_work',params={'select':'id','district_id':'eq.'+context.district_id,
        'assigned_to':'eq.'+context.user_id,'work_key':'eq.account:'+account.strip().upper(),'limit':1})
    return bool(rows)
