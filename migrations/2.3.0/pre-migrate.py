"""
Pre-migration for payment_paycomet_jetframe 2.3.0

Adds columns introduced in 2.2.0 that may not exist if the previous upgrade
failed before the schema sync ran (leaving the DB at version 2.2.0 without
the actual columns).

pre-migrate.py runs via direct cursor BEFORE the ORM loads any model,
so it is guaranteed to execute before any SELECT on payment_provider.
"""


def migrate(cr, version):
    # ── payment.provider ─────────────────────────────────────────────────────
    cr.execute("""
        ALTER TABLE payment_provider
        ADD COLUMN IF NOT EXISTS paycomet_api_url VARCHAR
        DEFAULT 'https://rest.paycomet.com'
    """)

    # Backfill existing Paycomet provider records
    cr.execute("""
        UPDATE payment_provider
           SET paycomet_api_url = 'https://rest.paycomet.com'
         WHERE code = 'jetframe'
           AND (paycomet_api_url IS NULL OR paycomet_api_url = '')
    """)

    # ── payment.transaction ───────────────────────────────────────────────────
    cr.execute("""
        ALTER TABLE payment_transaction
        ADD COLUMN IF NOT EXISTS paycomet_is_instant_credit BOOLEAN
        DEFAULT FALSE
    """)
