# Handover

- Name: Hrithick Ram M
- Email used for this application: hrithick2503@gmail.com
- Chosen track: Track A — Repair the Register
- Why this track: It plays to debugging/correctness work I do regularly (an LLM guardrail SDK, a k8s observability platform), and it has a boundable, verifiable scope (six planted defects, existing tests) rather than open-ended estimation.
- Approximate total time, including setup and handover: ~3.5 hours
- Working window: Sunday, 11:00 AM – 1:00 AM (investigation, fixing, testing, and documenting the handover)

## Run and verify

No new dependencies. Python 3.10+, stdlib only.

```
cd track-a
python -m unittest discover -s tests -v   # 15 tests: 5 original smoke + 10 new
python restore_fixture.py --replace       # loads the owner's real data into .local/
python app.py                             # http://127.0.0.1:8000
```

Open the browser, or hit the API directly, e.g.:
```
curl -s http://127.0.0.1:8000/api/export
curl -s -X POST --data-binary @samples/invoices-mixed.csv "http://127.0.0.1:8000/api/import?kind=invoices"
```

## What I delivered

Investigated the four reported symptoms and found their root causes reading `ledger/matching.py`, `storage.py`, `importing.py`, `reporting.py`, and `web/app.js`. Fixed all six seeded defects (BUSINESS_RULES.md confirmed six):

1. **`matching.py`** — payments were matched by amount *before* identity, so a payment could land on the wrong customer's invoice whenever two invoices shared an amount. Fixed to match on `(customer_id, invoice_number)` only, per spec.
2. **`storage.insert_invoice`** — had no duplicate check at all, so every retry of an import created a new row and inflated totals (this is the "retrying moves the numbers" symptom). Now skips an identical re-import and rejects a changed one, mirroring the payment logic that was already correct.
3. **`importing.import_csv`** — validated every row eagerly before the per-row try/except, so one bad row raised uncaught and discarded the whole file. Moved validation inside the loop so only the bad row is rejected.
4. **`reporting.invoices`** — `status=open` was mapped to `'paid'` internally (typo), so the open-invoice view disagreed with the overview. Fixed the mapping.
5. **`reporting.export_csv`** — truncated (`int(x*100)/100`) instead of rounding, so a float artifact could export as `0.09` while the app's own balance showed `0.10`. Now rounds once in `invoices()` and both screen and export read the same value.
6. **`web/app.js` `submitImport`** — never checked `response.ok`; it always displayed "Import complete" even on a 400, and never showed real counts or rejected-row reasons. Now shows imported/skipped/rejected counts and per-row reasons, and reports failure honestly.

Improvement beyond the required repairs: added `summary.generated_at` (UTC) to `/api/overview`, surfaced in the UI as "Last refreshed: …". Additive field only. Directly addresses the trust problem in the brief — the owner needs to know the numbers on screen are current, especially right after the reliability bugs above.

## Evidence and limits

`tests/test_regressions.py` (10 tests) — each documents its failing-before/passing-after behaviour in its docstring:
- Bug 1: `test_payment_never_matches_by_amount_across_customers` — before the fix this payment landed on `HARBOR/INV-100`; after, it lands only on `MAPLE/INV-200`.
- **Self-designed extra case** (not in the supplied samples): `test_payment_with_no_identity_match_stays_unmatched_even_if_amount_matches` — a payment whose amount coincidentally matches an invoice but whose invoice_number doesn't exist for that customer must stay unmatched, not attach by amount. Expected unmatched before running; confirmed unmatched after.
- Bug 2: `test_reimporting_identical_invoice_skips_not_duplicates` and `test_reimporting_same_identity_different_amount_is_rejected`.
- Bug 3: `test_one_bad_row_does_not_abort_the_whole_import`, run against the supplied `samples/invoices-mixed.csv`.
- Bug 4: `test_status_open_returns_only_open_invoices`.
- Bug 5: `test_export_matches_computed_balance`.
- Improvement: `test_overview_reports_generated_at_timestamp`.

`tests/test_regressions.py::FixturePreservationTests` — restores `fixtures/existing-register.sqlite3`, checks every value against `fixtures/expected-records.json` (including the two `KEEP-700` invoices with the same invoice_number but different customers, and the `KEEP-U1` unmatched payment), then imports a new invoice+payment, reconnects (simulating a restart), and re-checks that the original nine invoices are untouched and the new one persisted correctly.

I also ran the live HTTP server manually and confirmed with `curl`: `status=bogus` → 400 with a clear error; the mixed CSV → 200 with `imported:2, rejected:1`; a wrong-header file → 400 for the whole file (intentional — the header check is structural, not a per-row concern); `/api/export` output matches the on-screen balance exactly.

Known limits: I did not add a dependency file (none needed). I did not touch due-date aging or multi-currency — both explicitly out of scope in BUSINESS_RULES.md. I have not load-tested large CSVs; row-by-row validation is O(n) and fine for the sizes here but would need batching for very large files. Next highest-value step in a real project: add an idempotency key or dry-run/preview mode for imports, so the owner can see what an import *will* do before committing it — the six bugs here were all trust problems, and a preview step would catch the next one before it reaches production data.

## Tools and judgment

I used Claude (via the chat interface I'm working in) to read the codebase, form hypotheses about each of the four reported symptoms, and draft the fixes and tests.

1. **Suggestion**: initially considered fixing the amount-matching bug by making amount a tie-breaker instead of removing it. **My decision**: BUSINESS_RULES.md states plainly "An amount alone does not establish identity" — I rejected the tie-breaker approach and matched on identity only, then verified against the `KEEP-700` fixture case (same invoice_number, two different customers, different amounts) which specifically exercises this.
2. **Suggestion**: for the export-rounding bug, an early draft rounded independently in both `export_csv` and `invoices()`. **My decision**: I rejected the duplicated rounding logic and instead rounded once in `invoices()` so export and the API/UI can never drift apart again — checked by asserting the export string and the computed `balance` come from the same rounded value in `test_export_matches_computed_balance`.
3. **Suggestion**: for the improvement, a first idea was adding aging/overdue flags. **My decision**: rejected it — BUSINESS_RULES.md explicitly puts due-date aging out of scope — and instead added the `generated_at` freshness field, which is additive, in-scope, and directly answers "can the owner trust what's on screen."

All fixes and tests were run and their output inspected before inclusion above; nothing here is an unverified claim.
