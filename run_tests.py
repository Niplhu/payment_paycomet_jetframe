"""Run payment_paycomet_jetframe tests and print output."""
import sys
import os

# Set argv before any odoo import
os.chdir(r'C:\Program Files\Odoo 18.0.20260301\server')
sys.path.insert(0, os.getcwd())
log_dir = os.path.dirname(__file__)
sys.argv = [
    'odoo-bin', 'server',
    '--addons-path=odoo/addons',
    '-d', 'odoo18',
    '--test-enable',
    '--test-tags=payment_paycomet_jetframe',
    '--stop-after-init',
    '--log-level=info',
]

try:
    import odoo
    odoo.cli.main()
except SystemExit as e:
    print('Exit code:', e.code, file=sys.__stderr__)
    sys.exit(e.code if e.code is not None else 0)
except Exception as e:
    print('Error:', e, file=sys.__stderr__)
    import traceback
    traceback.print_exc()
    sys.exit(1)
