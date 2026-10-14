# Verified scope — browser-box wizard v5

## Executed unit tests

- 56 Discovery API tests passed with EXPLICIT synthetic fixtures only. They exercise flexible criteria, encoded URL/ID preservation, normal page 1–10 bounds, strict result schema and observed provenance, source-address/name association, unambiguous provider prose wrapping, queue/serialization, private persistence/restart/retention, dedup, cancellation, subprocess time/output bounds, error redaction and process-local schema/dispatch policy.
- 25 existing appliance unit tests passed separately. Synthetic fixtures are not proof of actual lead discovery.

## Actual live deployment and model/browser job

- API is image-integrated under Supervisor in the live VPS session; the existing global HTTPS Basic Auth protects /api/v1 without an additional raw host port. Browser/provider profile volumes and original user authentication were retained.
- Actual HTTPS validation used the exported appliance CA and matching hostname, NOT disabled certificate checking. This proves CA-scoped chain validation, NOT publicly trusted ACME TLS.
- Missing/wrong credentials returned 401; authenticated OpenAPI/docs returned 200; cross-origin writes returned 403; invalid unrestricted input returned 422; actual job submission returned 202 and polling returned 200.
- A real LinkedIn discovery job requested 1 professional profile and returned 1 accepted profile, status succeeded, missing_count 0, rejected_count 0. This used the existing configured model and actual browser/web tool observations, NOT a synthetic backend or fabricated response. Actual names/profile data and job identifiers are intentionally excluded from reusable source/Brain.
- Verbatim observed facts and profile association were validated. This is observed provenance, NOT independent truth/freshness/identity/legal-eligibility certification; human review remains necessary.
- A generated temporary diagnostic HTTPS account was removed in finally and its credentials then returned 401. The user's existing password was not read/reset. The API retains no plaintext auth secret.
- Installed tool-policy check returned only browser_back, browser_get_images, browser_navigate, browser_scroll, browser_snapshot, web_extract and web_search. No raw API/CDP or Docker socket was exposed.
- VNC health and an actual 1440×900 / 5,184,000-byte frame over WebSocket still passed after API deployment. Pixel contents were neither logged nor retained.

## Corrections found by real tests

- Provider prose surrounding one otherwise valid JSON object initially caused schema failure. Exactly one unambiguous object can now be extracted and validated with the same strict schema; multiple objects, unknown fields and fabricated facts remain rejected.
- A proposed lead with a fabricated combined quote was rejected. The task prompt now requests exact separate quotes. A fetched profile's document address plus its exact observed name quote is legitimate source metadata for association; an unrelated-page name quote is not. The later real request passed without rejected/padded profiles.
- An isolated combined wizard test encountered a 10-second TLS WebSocket-auth handshake timeout under contention. Its timeout was raised to 30 seconds without dropping assertions; the final retest outcome is appended separately, not assumed here.

## Limits

- A 15-profile production run, repeat-day throughput, peak load and exact cost per usable lead were NOT verified. Requested count is a target; partial/blocked/failed outcomes remain honest.
- Login/captcha/access restrictions stop the job; no bypass, outreach sends or recurring discovery schedule is installed.
- Actual lead/credential/session data remains private. API retention is 30 days; Hermes's own traces/provider records require their separate retention policy.
- Publicly trusted ACME, other-server/provider-firewall portability, AMD64 and host power-loss persistence are not claimed. Skills/tool allowlists are not an OS/DNS sandbox; the worker lock does not coordinate simultaneous human/other-agent browser use.

## Final clean reinstall check

- `bash setup.sh --self-test` from a fresh 37-file credential/state-free extraction exited 0. Actual local HTTPS/Basic Auth, shared Chromium control, fresh-cookie persistence across restart, complete authenticated TLS WebSocket framebuffer, and the custom-port public profile bound ONLY to loopback all passed. Test containers were stopped; the live API/session remained running. No model login or public test deployment was performed by this fixture.
- The earlier final-stage fixture failure was a hardcoded `local` edge hostname after switching to the `public` profile. The fixture now selects exactly one actually running local/public service; authentication and frame assertions are unchanged.
