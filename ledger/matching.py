from .storage import invoice_by_key


def find_invoice(db, payment):
    """Match a payment to an invoice by identity only.

    BUSINESS_RULES.md: "A new payment may be attached only to an invoice
    with both the same customer ID and invoice number. An amount alone
    does not establish identity." Amount is intentionally not used here.
    """
    exact = invoice_by_key(db, payment['customer_id'], payment['invoice_number'])
    return exact['id'] if exact else None
