# Lubbock DBA feasibility prototype

This is an isolated, read-only experiment. It is not registered in the application and does not restore the withdrawn County Clerk UI or APIs. No source crawl, scheduler, queue import, account change or match decision is implemented here.

On October 28, 2026, the public EagleWeb browser search was used interactively for recording dates October 19–28 and both assumed-name types. Its complete single-page index reported 28 records. The 28 actual linked detail pages were visited sequentially through ordinary public browser navigation. Each displayed `#presentation` HTML block, page text, original source URL and index identity were retained locally. No private endpoints, authentication bypass or restricted image retrieval were used. This was one bounded browser-assisted sample, not an approved or demonstrated unattended connector.

`prototype.py` has **no EagleWeb network client**. It reads those local captures, validates source URL, document identity, filing type and required date/name fields, parses all owner rows and separately labeled business/mailing addresses, and retains explicit missing fields. It queries existing district-scoped Supabase account data using the existing production CAD matcher, account adapter and read-only indexed name-context RPC. Production matching behavior is not changed. Strong unambiguous address-plus-name results become account leads; partial-name or address-only suggestions remain review findings. Real-property and excluded inactive accounts are ineligible. Multiple full owners are compared separately, and conflicting account outcomes remain ambiguous. Same-name filings in the sample are cross-referenced without inferring a business closure.

The latest completed account import's ID, date, row count and supplied as-of date are included with the results. An absent as-of date is shown as absent. No reliable match means only that these currently loaded records did not provide enough evidence, not that a new taxable account should be created.

## Reproduce the offline comparison

Requires project dependencies plus `beautifulsoup4` (already present in the development environment). Supply existing server environment variables or an explicit local environment file. Never commit credentials or captured county/account records.

```
python -m prototypes.lubbock_dba.prototype \
  --captures /absolute/path/to/local-captures \
  --district-id <configured-Lubbock-district-UUID> \
  --output /absolute/path/to/results \
  --env-file /absolute/path/to/server.env
python -m prototypes.lubbock_dba.report /absolute/path/to/results
python -m pytest tests/test_lubbock_dba_prototype.py -q
```

Capture directory: `index.json` holds the index's rendered text, original HTML table and original record references. Each `<document-number>.json` holds the corresponding URL, rendered presentation HTML and body text. Raw captures and generated evidence are local artifacts, not repository fixtures. There is a 30-filing cap. Missing files, incomplete index metadata and mismatched layouts must be reported rather than treated as a successful unattended run.

Outputs: `results.json` (complete extracted evidence, source hashes and account comparisons), `filings.csv` (flat review/export), and `report.html` (standalone searchable report with original-record links). The report uses only local data and no external scripts or resources.

The public indexed pages in this sample supplied full owner names for all 28 records and business addresses for the 26 assumed names. Both abandonments omitted a business address. No separate mailing address was shown. Document images remained restricted to the Clerk's office. Source field labels/spellings are preserved, including unusual or potentially erroneous labels; they are not silently swapped or corrected.

To move beyond this prototype, confirm permission/support for the chosen retrieval method and test bounded request rate, session expiry, pagination and totals reconciliation, changed/corrected records, replay deduplication and failures. Existing production `EagleWebConnector.retrieve_window()` remains disabled. No emails or permission requests were sent.

## Observed result

All 28 indexed details in the bounded sample were retrieved and parsed: 26 assumed-name certificates and two abandonments. Comparing them with the existing account data produced three strong account leads, ten additional identity/location review leads, two abandonment leads, and thirteen filings without a reliable match. The three strong leads are computer suggestions, not appraiser-confirmed links. A fourth same-address result had only a partial owner-name match and was deliberately kept in review. The sample also contains a same-name abandonment/new-filing pair with changed ownership information.

These results demonstrate useful extraction and account-comparison feasibility. They do not measure precision/recall against appraiser-labeled ground truth, prove a filing is a newly operating business, establish that unmatched accounts do not exist, or demonstrate unattended daily collection.
