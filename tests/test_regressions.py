"""Regression tests for the defects found and fixed during this repair.

Each test documents the failing-before / passing-after behaviour it guards.
Run with: python -m unittest discover -s tests -v
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ledger import storage, reporting, importing

ROOT = Path(__file__).resolve().parent.parent


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = storage.connect(Path(self.tmp.name) / 'demo.sqlite3')
        storage.seed(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    # --- Bug 1: payment matching must use identity, never amount alone ---
    def test_payment_never_matches_by_amount_across_customers(self):
        """BEFORE FIX: a payment for MAPLE/INV-200 (1250.00) was silently
        attached to HARBOR/INV-100 instead, because HARBOR/INV-100 also has
        amount 1250.00 and matching checked amount before identity.
        AFTER FIX: the payment must land only on the invoice with the
        matching (customer_id, invoice_number) pair.
        """
        result = importing.import_csv(
            self.db,
            'payment_id,customer_id,invoice_number,amount\nPAY-201,MAPLE,INV-200,1250.00\n',
            'payments')
        self.assertEqual(result['imported'], 1)
        rows = {(r['customer_id'], r['invoice_number']): r for r in reporting.invoices(self.db)}
        self.assertEqual(rows[('MAPLE', 'INV-200')]['paid'], 1250.00)
        self.assertEqual(rows[('HARBOR', 'INV-100')]['paid'], 0)

    def test_payment_with_no_identity_match_stays_unmatched_even_if_amount_matches(self):
        """Self-designed extra case: a payment whose amount happens to equal
        an existing invoice's amount, but whose invoice_number does not
        exist for that customer, must be recorded as unmatched -- not
        silently attached to the invoice that happens to share the amount.
        """
        result = importing.import_csv(
            self.db,
            'payment_id,customer_id,invoice_number,amount\nPAY-X,HARBOR,INV-DOES-NOT-EXIST,1250.00\n',
            'payments')
        self.assertEqual(result['imported'], 1)
        overview = reporting.overview(self.db)
        self.assertEqual(len(overview['unmatched_payments']), 1)
        self.assertEqual(overview['unmatched_payments'][0]['payment_id'], 'PAY-X')
        harbor_100 = next(r for r in reporting.invoices(self.db) if r['invoice_number'] == 'INV-100')
        self.assertEqual(harbor_100['paid'], 0)

    # --- Bug 2: re-importing an invoice must skip, not duplicate ---
    def test_reimporting_identical_invoice_skips_not_duplicates(self):
        """BEFORE FIX: retrying an identical invoice import created a second
        row every time, inflating invoice_count and outstanding on every
        retry. AFTER FIX: an identical re-import is skipped and counts are
        unchanged.
        """
        before = len(reporting.invoices(self.db))
        csv_text = 'customer_id,invoice_number,amount,due_date\nHARBOR,INV-100,1250.00,2026-09-01\n'
        r1 = importing.import_csv(self.db, csv_text, 'invoices')
        r2 = importing.import_csv(self.db, csv_text, 'invoices')
        self.assertEqual(r1['skipped'], 1)
        self.assertEqual(r2['skipped'], 1)
        self.assertEqual(len(reporting.invoices(self.db)), before)

    def test_reimporting_same_identity_different_amount_is_rejected(self):
        """Reusing an invoice identity with different details must reject
        the row and preserve the original (BUSINESS_RULES.md)."""
        result = importing.import_csv(
            self.db,
            'customer_id,invoice_number,amount,due_date\nHARBOR,INV-100,999.00,2026-09-01\n',
            'invoices')
        self.assertEqual(result['rejected'], 1)
        original = next(r for r in reporting.invoices(self.db) if r['invoice_number'] == 'INV-100')
        self.assertEqual(original['amount'], 1250.00)

    # --- Bug 3: a bad data row must not discard good rows ---
    def test_one_bad_row_does_not_abort_the_whole_import(self):
        """BEFORE FIX: normalize() ran on every row before the per-row
        try/except, so one invalid row raised an uncaught ValueError and
        the whole import failed with HTTP 400 -- even the valid rows around
        it were never written. AFTER FIX: only the bad row is rejected.
        """
        mixed = (ROOT / 'samples' / 'invoices-mixed.csv').read_text()
        result = importing.import_csv(self.db, mixed, 'invoices')
        self.assertEqual(result['imported'], 2)
        self.assertEqual(result['rejected'], 1)
        self.assertEqual(result['errors'][0]['line'], 3)
        numbers = {r['invoice_number'] for r in reporting.invoices(self.db)}
        self.assertIn('INV-103', numbers)
        self.assertIn('INV-203', numbers)

    # --- Bug 4: status filter must return the requested status only ---
    def test_status_open_returns_only_open_invoices(self):
        """BEFORE FIX: status=open was mapped to 'paid' internally, so the
        open-invoice view showed paid invoices (or none), disagreeing with
        the overview counts.
        """
        open_rows = reporting.invoices(self.db, 'open')
        paid_rows = reporting.invoices(self.db, 'paid')
        self.assertTrue(all(r['status'] == 'open' for r in open_rows))
        self.assertTrue(all(r['status'] == 'paid' for r in paid_rows))
        overview = reporting.overview(self.db)
        self.assertEqual(len(open_rows), overview['summary']['open_count'])

    # --- Bug 5: export and screen must agree on rounding ---
    def test_export_matches_computed_balance(self):
        """BEFORE FIX: export_csv truncated (int(x*100)/100) instead of
        rounding, so a float artifact like 0.09999999999999964 exported as
        0.09 while the app's own rounded balance was 0.10.
        """
        importing.import_csv(
            self.db, 'customer_id,invoice_number,amount,due_date\nHARBOR,INV-999,10.10,2026-09-01\n', 'invoices')
        importing.import_csv(
            self.db, 'payment_id,customer_id,invoice_number,amount\nP-999,HARBOR,INV-999,10.00\n', 'payments')
        row = next(r for r in reporting.invoices(self.db) if r['invoice_number'] == 'INV-999')
        self.assertEqual(row['balance'], 0.10)
        csv_out = reporting.export_csv(self.db)
        line = next(l for l in csv_out.splitlines() if 'INV-999' in l)
        self.assertIn('0.10', line)
        self.assertNotIn('0.09', line)

    # --- Improvement: freshness indicator ---
    def test_overview_reports_generated_at_timestamp(self):
        """Improvement: /api/overview now includes summary.generated_at so
        the owner can tell the register they're viewing is current."""
        summary = reporting.overview(self.db)['summary']
        self.assertIn('generated_at', summary)
        self.assertTrue(summary['generated_at'].endswith('Z'))


class FixturePreservationTests(unittest.TestCase):
    """Verifies the owner's existing register survives the repair and a restart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'clearledger.sqlite3'
        shutil.copy2(ROOT / 'fixtures' / 'existing-register.sqlite3', self.db_path)
        self.expected = json.loads((ROOT / 'fixtures' / 'expected-records.json').read_text())

    def tearDown(self):
        self.tmp.cleanup()

    def test_existing_register_matches_expected_records(self):
        db = storage.connect(self.db_path)
        summary = reporting.overview(db)['summary']
        self.assertEqual(summary['invoice_count'], self.expected['summary']['invoice_count'])
        self.assertEqual(summary['open_count'], self.expected['summary']['open_count'])
        self.assertAlmostEqual(summary['outstanding'], float(self.expected['summary']['outstanding']), places=2)
        db.close()

    def test_existing_register_survives_new_import_and_restart(self):
        db = storage.connect(self.db_path)
        importing.import_csv(
            db, 'customer_id,invoice_number,amount,due_date\nHARBOR,NEW-1,42.00,2026-09-20\n', 'invoices')
        importing.import_csv(
            db, 'payment_id,customer_id,invoice_number,amount\nNEW-P1,HARBOR,NEW-1,42.00\n', 'payments')
        db.close()

        # Simulate an app restart: reconnect fresh.
        db2 = storage.connect(self.db_path)
        rows = {(r['customer_id'], r['invoice_number']): r for r in reporting.invoices(db2)}
        for inv in self.expected['invoices']:
            row = rows[(inv['customer_id'], inv['invoice_number'])]
            self.assertEqual(row['id'], inv['id'])
            self.assertAlmostEqual(row['amount'], float(inv['amount']), places=2)
        new_row = rows[('HARBOR', 'NEW-1')]
        self.assertEqual(new_row['status'], 'paid')
        unmatched = {p['payment_id'] for p in reporting.overview(db2)['unmatched_payments']}
        self.assertIn('KEEP-U1', unmatched)
        db2.close()


if __name__ == '__main__':
    unittest.main()
