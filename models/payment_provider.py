from odoo import fields, models


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[('jetframe', "Paycomet JET Frame")],
        ondelete={'jetframe': 'set default'}
    )

    paycomet_terminal_id = fields.Char(
        string="Terminal ID",
        required_if_provider='jetframe',
    )
    paycomet_api_key = fields.Char(
        string="API Key",
        required_if_provider='jetframe',
        groups='base.group_system',
    )

    def _get_default_payment_method_codes(self):
        self.ensure_one()
        if self.code != 'jetframe':
            return super()._get_default_payment_method_codes()
        return {'card', 'instant_credit'}
