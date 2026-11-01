# NDMA SACHET official alert evidence (service only)

This feature retrieves published official Indian disaster alerts without
changing the Streamlit dashboard, Gemini tools, evidence-v1 schema, or existing
weather/terrain calculations. No credentials or paid APIs are required.

## Interfaces and attribution

- [Official All-India RSS](https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml)
  supplies links to CAP XML. Only linked numeric-identifier
  `https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile` requests are allowed.
  Never construct identifiers or use unofficial scraping.
- [Official agency integration guide](https://sachet.ndma.gov.in/docs/Integration_Guide_For_Agencies.pdf)
  requires ETag caching, `If-None-Match`, and correct HTTP 200/304 handling.
- [NDMA SACHET portal](https://sachet.ndma.gov.in/) distributes alerts from
  multiple authorities. Attribute the issuer using CAP `sender` and each
  `info.senderName`, not just NDMA. Preserve original text and source links.
  RSS declares public-domain content; this is not a blanket license for every
  linked asset. No endorsement by NDMA or an issuing authority is implied.
  Source authenticity here relies on the official HTTPS endpoint and normal
  TLS verification, not independent verification of XML digital signatures.

## Usage

```python
from services.sachet import fetch_sachet_alerts

evidence = fetch_sachet_alerts(latitude, longitude)
# Do not display evidence['alerts'] as local alerts.
local_alerts = evidence['active_matching_alerts']
# Always display retrieval/matching status, warnings, and limitations alongside it.
```

The function validates finite numeric WGS84 coordinates before any request
(latitude -90..90, longitude -180..180; booleans and strings are rejected).
It returns a defensive JSON-serializable dictionary. Invalid caller inputs
raise `ValueError`; source, HTTP, and XML failures produce explicit report
states. There is no fallback observational or alert data.

For independent lifecycle/cache control, pass `client=SachetClient()` and reuse
that client across investigations. The module default client is reused within
one process. `clear_cache()` explicitly discards its cache. A lock serializes
fetches sharing a client. This is not a shared/multi-worker rate limiter.

## Response schema 1.0

| Field | Meaning |
| --- | --- |
| `schema_version` | `"1.0"`. |
| `source`, `source_url`, `integration_guide_url` | NDMA SACHET and official feed/guide URLs. |
| `requested_coordinates` | Numeric `latitude`, `longitude` in WGS84. |
| `evaluated_at` | Timezone-aware ISO 8601 UTC evaluation timestamp. |
| `retrieval_status` | `available` = requested snapshot retrieved/parsed; `partial` = any missing/rejected/truncated CAP or ambiguous lifecycle; `unavailable` = RSS could not be retrieved/parsed. Does not express alert applicability. |
| `matching_status` | Values described below; independent of HTTP success. |
| `location_match` | `matched`, `not_matched`, or `unresolved`. Applies only to verified active claims, not to district-name similarity. |
| `active_matching_alerts` | Deduplicated, active, Actual/Public alerts with verified inline polygon containment and resolved lifecycle. Empty on incomplete retrieval snapshots. May contain independent resolved matches when another reference component is unresolved; always inspect warnings and coverage. |
| `alerts` | Parsed, reconciled messages for inspection, including inactive, expired, nonpublic/test, superseded, unmatched, and unresolved entries. Never treat this entire collection as local. |
| `retrievals` | Each successful resource's `source_url`, `retrieved_at`, `validated_at`, `etag` (possibly null), and `cache_status` (`downloaded`, `ttl_hit`, `revalidated`). |
| `coverage` | `rss_items` (null if feed unavailable), `processed_messages`, `complete_snapshot` (retrieval/parsing), and `lifecycle_resolved` (no unresolved reference component). Completeness is relative to this RSS response only, never a claim that historical predecessors were all retrieved. |
| `warnings` | Structured `{code, message}` list. Lifecycle diagnostics also include `source_url`, `identifier`, and normalized `reference` (or null for propagated uncertainty). Codes include `source_unavailable`, `invalid_item`, `truncated_feed`, `message_unavailable`, `lifecycle_unresolved`, `validity_unresolved`, `location_unresolved`, `cross_sender_authority_unresolved`, `non_chronological_reference`, `reference_identity_mismatch`, `linked_lifecycle_uncertainty`, and `referenced_message_not_in_snapshot`. No raw HTTP response/error bodies are included. |
| `limitations` | Interpretation and coverage limits, always retained. |

`matching_status` has these explicit meanings:

| Value | Meaning |
| --- | --- |
| `source_unavailable` | Feed unavailable; no current alert evidence established. |
| `incomplete_source` | CAP budget/failure/invalid XML or conflicting duplicate identity prevents claiming an authoritative matching list. |
| `active_matching_alerts` | One or more verified active polygon matches. Other unresolved records still remain in `alerts`; this is not exhaustive local coverage. |
| `location_matching_unresolved` | Active alert(s) exist but no authoritative coordinate match can be established. |
| `alert_validity_unresolved` | No verified active matches and some eligible messages lack usable expiry/validity information. |
| `alert_lifecycle_unresolved` | No verified active matches and a reference component has unresolved authority, chronology, or identity. |
| `no_verified_active_matching_alerts` | No active verified matches in this snapshot; **not** “no official alerts” or “safe.” |

Each CAP alert preserves `identifier`, `sender`, `sent`, `sent_original`,
`status`, `msgType`, `scope`, `source_url`, `references_original`, normalized
`references`, and all `info` sections. Added fields are `active`,
`location_match`, `lifecycle_issues` (structured code/reference diagnostics), and
`lifecycle` (`active`, `superseded`, `unresolved`, or `inactive_or_unverifiable`). `sender` is the CAP
issuing authority identifier; `info.senderName` is the readable authority name
when provided. Optional fields remain null instead of being invented.

Each `info` retains language, senderName, event, severity, urgency, certainty,
headline, description, instruction, effective/onset/expires (normalized and
original strings), and areas. Each area retains description, LGD district codes,
raw inline polygon strings, and an unsupported-circle indicator. Section fields
`active`, `temporal_status`, and `location_match` prevent combining a match in
one language/area with validity from an unrelated section. `temporal_status`
is `active`, `expired`, `pending`, `unresolved`, or `ineligible`.

All timestamps used for decisions must contain a timezone; valid source
offsets normalize to UTC and original timestamp strings are preserved. Missing
effective time defaults to CAP sent time; onset is retained as hazard timing,
not substituted for issuance/expiry. Missing expiry or inconsistent validity
ordering prevents an active claim. Exact expiry is inactive. Issuance, source
retrieval, conditional validation, and evaluation times are distinct.

## Geographic and lifecycle safeguards

- No free-text matching, district-name geocoding, nearest-district assumption,
  or coordinate-to-LGD inference. LGD codes are preserved as metadata only.
- Only closed, finite, nondegenerate, non-self-intersecting inline CAP rings
  (at most 256 edges) are considered. Point containment includes boundaries.
  Dateline-spanning/polar/unsupported/invalid geometry remains unresolved.
  Areas without a usable inline polygon remain unresolved. A reliable positive
  ring match can establish applicability; a negative conclusion requires no
  unresolved area in that active section.
- Real SACHET messages may link separate polygon files. This version deliberately
  does **not** download them, parse circles, or maintain an authoritative district
  boundary dataset. Therefore many real alerts will be unresolved. Do not add
  apparent geographic precision by guessing from `areaDesc` or LGD proximity.
- CAP identity is `(sender, identifier, sent)`. Duplicate RSS links are fetched
  once; identical CAP identities are deduplicated across URLs; conflicting
  identity content makes the snapshot partial. Multilingual sections are
  retained under one alert rather than becoming multiple local-alert cards.
- Actual/Public Cancel and Update messages suppress their referenced older
  same-sender identities, independent of RSS order. Cancellation tombstones
  do not expire simply because an old information section expired. An expired
  update never restores its predecessor. Test/private/system messages do not
  cancel public actual alerts or become active local evidence.
- CAP permits references to the identified originator; it does not establish
  a trust policy authorizing one authority to supersede another. This client
  has no authority-delegation registry. Cross-sender references are therefore
  retained as **unresolved**, not rejected as malformed CAP or automatically
  trusted. Non-earlier reference times and mismatched timestamps for a known
  sender/identifier are unresolved too. All connected identities (including
  subsequent updates) are quarantined from active claims. Independent alerts
  remain available. The report is partial, with lifecycle_resolved=false;
  complete_snapshot can still be true if retrieval/parsing was complete.
- If a chronologically valid same-sender reference's exact predecessor is absent
  and no conflicting same-sender/identifier is present, preserve its identity
  as a tombstone and report `referenced_message_not_in_snapshot`. Do not invent
  or fetch the absent predecessor. Its absence alone does not invalidate the
  newer authoritative message or create a claim of complete history.
- A missing update/cancellation may change applicability. Consequently any
  truncation, failed CAP retrieval, or malformed CAP withholds the top-level
  matching list, while retaining diagnostics and successfully parsed evidence.
  In particular, a malformed reference with no reliable target could refer to
  any retained alert, so withholding local claims is safer than guessing that
  another message is independent. This differs from a syntactically valid
  reference whose involved identities can be isolated.

### Live lifecycle compatibility diagnosis (1 November 2026)

An approved bounded read of three official documents established this pattern:

- Current source: `FetchXMLFile?identifier=1793518105564029` on the official host.
- Current CAP identity: sender `Maharashtra-SDMA`, identifier
  `IN-1793518105564029_29`, msgType `Update`, sent
  `2026-11-01T13:02:29+05:30` (07:32:29 UTC).
- Original reference:
  `IMD-Mumbai,IN-1793518105564029_46,2026-11-01T12:57:48+05:30`
  (07:27:48 UTC). The predecessor was absent from the three-document sample.

The old rule raised a global error because the senders differed, despite correct
chronological ordering and valid reference parsing. The enclosing service then
lost all reconciled messages, including unrelated Chennai/Mumbai alerts. The
correction preserves reference metadata, quarantines that authority transition,
and retains independent evidence without claiming delegated authority.

The [OASIS CAP 1.2 standard, section 3.2.1](https://docs.oasis-open.org/emergency/cap/v1.2/CAP-v1.2-os.html)
defines whitespace-separated `sender,identifier,sent` reference tuples and
Update/Cancel semantics. It does not resolve this source-specific authority
relationship. No further live requests are made by regression tests. The
observed metadata is used in an offline regression; its other fields/polygons
are explicitly synthetic fixtures, never real local-alert evidence.

## Transport, caching, and XML safety

Default limits are 10 seconds per blocking request/read, a 30-second network
deadline checked between reads, 1 MB per document, and at most 20 unique CAP
links per investigation. This is a bounded network budget, not a hard wall-clock
guarantee across lock waits/parsing/OS scheduling. The TTL is 300 seconds. Cache
storage is bounded to 128 documents and 8 MB of XML. There is no retry loop.

Within TTL, cached XML is reused and expiry is still reevaluated. After TTL,
the server-provided ETag is sent in `If-None-Match`. HTTP 304 uses cached XML
and advances validation time without inventing a new download timestamp.
HTTP 200 replaces cached XML/ETag. A 304 without usable cache is a failure.
Failed revalidation never serves stale alerts as current. Upstream changes
or cancellations may take up to the TTL to appear; inspect validation times.
ETag conditional requests require the server to have supplied an ETag.

RSS and CAP links must use HTTPS on the exact official host/path; identifiers
must be numeric and extra query parameters are rejected. Redirects are never
followed. XML must be bounded UTF-8, with no DTD or entity declarations; those
are rejected before ElementTree parsing. Element counts are capped. This
prevents external entity retrieval and custom entity expansion without a new
dependency. Treat returned issuer text as untrusted content if a later UI/LLM
integration is approved: do not execute it, render raw HTML, or treat it as
application instructions.

## Validation and remaining limits

Run offline tests from the repository root:

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
git diff --check
```

`tests/test_sachet.py` uses synthetic realistic RSS/CAP fixtures and mocked HTTP
only inside tests, with live sockets blocked. It covers active and expired
alerts, updates/cancellations, language duplication, malformed/unsafe XML,
invalid timestamps, budgets, URL restrictions, HTTP failures, 304 revalidation,
cache expiry/failures, and matching/mismatch/unresolved outcomes.

This service has no SLA, numeric published source quota, persisted alert archive,
or shared Cloud cache. A bounded feed may omit earlier alerts/cancellations;
`complete_snapshot` does not establish historical completeness. Separate polygons,
LGD mapping, global geodesic geometry, retry scheduling, and dashboard/agent
integration require separate review. No live API request is part of automated
tests. Scientific screening limitations and original official issuer authority
must remain visible; the service does not generate emergency instructions.

## Suggested PR description

Add isolated NDMA SACHET RSS/CAP evidence service with bounded allowlisted HTTP,
ETag/TTL caching, safe XML parsing, multilingual provenance, and update/cancel/
expiry reconciliation. Return schema 1.0 with explicit source availability,
snapshot completeness, active verified polygon matches, unresolved applicability,
warnings, and limitations. No district-name/proximity matching or fabricated
alerts. Add offline fixture tests and service/schema documentation.

No changes to weather/terrain calculations, evidence-v1, Gemini, or Streamlit.
Separate polygon downloads and authoritative coordinate-to-LGD mapping remain
deferred, so many real alerts will remain unresolved. Include actual final test
counts and validation results when requesting commit/PR approval.
