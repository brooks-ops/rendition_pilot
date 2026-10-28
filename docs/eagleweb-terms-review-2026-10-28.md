# Lubbock EagleWeb published-notice review — October 28, 2026

Outcome: **automated retrieval permission remains unconfirmed**. No unattended
collection was enabled and no new source filings were collected during this
review. This is an evidence record, not a legal determination that automation
is prohibited or that separate written permission is legally required.

## Official pages reviewed

- https://erecord.lubbockcounty.gov/recorder/web/splash.jsp
  The notice describes public-service access and disclaims accuracy/authenticity
  and liability. It explains redaction and that the online database is not the
  official repository. No automated-access permission or prohibition was found
  in the displayed disclaimer.
- https://erecord.lubbockcounty.gov/recorder/web/login.jsp
  Public login is offered for Official Records, Assumed Names and Marriages.
  This establishes a public search path, not an explicit unattended-use policy.
- https://erecord.lubbockcounty.gov/recorder/web/help.jsp
  Documents date-range and document-type searches. No automated-use policy found.
- https://erecord.lubbockcounty.gov/recorder/web/about.jsp
  Identifies EagleWeb as EagleRecorder's browser interface and Tyler as provider.
  No automated-use policy found.
- https://erecord.lubbockcounty.gov/recorder/eagleweb/fraudGuardSignup.jsp
  Its displayed disclaimer addresses availability, accuracy, warranties and
  liability. It does not explicitly authorize or prohibit automated collection.
  Name-specific fraud alerts are not a feed of all newly recorded DBAs.

## Limitations and technical observations

- GET https://erecord.lubbockcounty.gov/robots.txt returned HTTP 404 with a
  generic recorder redirect page. No robots directives were returned. A missing
  robots file is neither a grant of permission nor proof of a prohibition.
- The County Clerk link from EagleWeb points to
  https://www.co.lubbock.tx.us/department/?fDD=2-0 and returned HTTP 403 in the
  research browser. https://www.lubbockcounty.gov/ and its County Clerk page
  also returned HTTP 403. No attempt was made to evade those responses.
- Search-engine results from unofficial court-reference sites and unrelated
  OpenTexas terms were excluded; they do not govern the county's EagleWeb portal.
- No Clerk/provider message was sent, no agreement accepted, and no source
  credentials, API/export authorization or unattended method were obtained.

## Activation decision

The user authorized activation **if allowed**, and the project's prior explicit
requirement was not to enable unattended EagleWeb collection until authorized
access and a reliable method are confirmed. Published notices reviewed here do
not resolve that condition. Keep the connector's hard gate and current quarterly
BLOCKED status. Existing captured filings, BPP queue and CAD matching are unchanged.
Do not describe the missing policy as an explicit ban or claim a working weekly
collector exists. The quarterly task currently creates blocked collection plans only.
