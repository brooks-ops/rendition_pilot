"""DBA leads: High requires an exact normalized identity and situs match."""
from app.comptroller.address_normalizer import normalize_address
from app.comptroller.matching import normalize_name


def filing_priority(*, evidence, account=None, record_id=None, ambiguous=False):
    evidence = evidence or {}
    filing = evidence.get('filing') or {}
    comparison = evidence.get('comparison') or {}
    detail = comparison.get('candidate_details') or {}
    if not detail and record_id:
        detail = next((c for c in evidence.get('account_candidates', [])
                       if (c.get('id') or c.get('property_id')) == record_id), {})
    address = filing.get('business_address') or {}
    street = ' '.join(filter(None, [address.get('line1'), address.get('line2')]))
    source = normalize_address(street, zip_code=address.get('zip'))
    target = normalize_address(detail.get('situs_address_raw'), zip_code=detail.get('situs_zip'))
    names = [filing.get('assumed_name')]
    if filing.get('owner_name_complete'):
        names.extend(filing.get('owners') or [filing.get('owner_name')])
    source_names = {normalize_name(n) for n in names if normalize_name(n)}
    target_names = {normalize_name(detail.get(k)) for k in ('owner_name','business_name','dba_name') if normalize_name(detail.get(k))}
    same_account = detail.get('real_account_number') == account
    same_pid = (detail.get('property_id') or detail.get('id')) == record_id
    exact = (bool(account and record_id) and not ambiguous and same_account and same_pid
             and bool(source.base_address) and source.base_address == target.base_address
             and source.unit == target.unit and bool(source.zip5) and source.zip5 == target.zip5
             and bool(source_names.intersection(target_names)))
    if exact:
        return 0, 'HIGH', 'Exact CAD account + address + DBA/owner match'
    has_evidence = bool(account or detail or evidence.get('account_candidates'))
    if has_evidence:
        return 1, 'MEDIUM', 'Possible account match — identity or address needs verification'
    return 2, 'LOW', 'No reliable existing BPP account match'
