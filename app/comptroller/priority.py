"""Evidence-based review order shared by both BPP queue sources."""
def match_priority(*, account, record_id=None, signals=None, reason=None, ambiguous=False, confidence=None):
    signals = signals or {}
    reason = reason or ''
    identified = bool(account and record_id)
    address = signals.get('address') in ('MATCH', 'NORMALIZED MATCH')
    strong_name = any(signals.get(k) == 'MATCH' for k in ('business_dba_name', 'legal_entity_name'))
    partial_name = any(signals.get(k) == 'PARTIAL MATCH' for k in ('business_dba_name', 'legal_entity_name'))
    # Historical closure rows store the generated match explanation rather
    # than structured signals. Only recognize the matcher’s explicit evidence.
    if reason.startswith('Imported CAD personal-property account: situs address and '):
        address = True
        strong_name |= 'strong CAD owner/business/DBA name match' in reason
        partial_name |= 'partial CAD name match' in reason
    if reason.startswith('Possible imported CAD BPP account(s) at this situs:'):
        address = True
    conflict = signals.get('zip') == 'NO MATCH' or signals.get('suite_unit') in ('NO MATCH', 'CONFLICT')
    if ambiguous or conflict:
        return 4, 'MEDIUM', 'Ambiguous or conflicting match — review required'
    if identified and address and strong_name and confidence in (None, 'HIGH') and signals.get('suite_unit') != 'MISSING':
        return 0, 'HIGH', 'PID + address + DBA/owner match'
    if identified and address and strong_name:
        return 1, 'MEDIUM', 'PID + address + DBA/owner; details need verification'
    if identified and address and partial_name:
        return 1, 'MEDIUM', 'PID + address + partial DBA/owner match'
    if identified and address:
        return 2, 'MEDIUM', 'PID + address; DBA/owner needs verification'
    if identified or strong_name or partial_name:
        return 3, 'MEDIUM', 'Partial account or name evidence'
    return 5, 'LOW', 'No matching account identified'
