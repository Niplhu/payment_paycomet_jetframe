"""
Pre-migration script for payment_paycomet_jetframe 2.2.0

Adds the paycomet_api_url and paycomet_is_instant_credit columns to the
database BEFORE Odoo's ORM boots, so that any field-recompute triggered
during the upgrade flush does not fail with UndefinedColumn.

Odoo executes pre-migrate.py before loading models and computing anything,
so this is the correct and safe place to add new columns.
"""


def migrate(cr, version):
    # paycomet_api_url — configurable REST API base URL (payment.provider)
    cr.execute("""
        ALTER TABLE payment_provider
        ADD COLUMN IF NOT EXISTS paycomet_api_url VARCHAR
        DEFAULT 'https://rest.paycomet.com'
    """)

    # Backfill existing Paycomet providers with the production default
    cr.execute("""
        UPDATE payment_provider
        SET paycomet_api_url = 'https://rest.paycomet.com'
        WHERE code = 'jetframe'
          AND (paycomet_api_url IS NULL OR paycomet_api_url = '')
    """)

    # paycomet_is_instant_credit — flag on payment.transaction
    cr.execute("""
        ALTER TABLE payment_transaction
        ADD COLUMN IF NOT EXISTS paycomet_is_instant_credit BOOLEAN
        DEFAULT FALSE
    """)
