from odoo import api, fields, models

# ---------------------------------------------------------------------------
# Default Paycomet REST API base URLs
# The /v1/form path is appended in the transaction model.
# ---------------------------------------------------------------------------
PAYCOMET_API_URL_PRODUCTION = "https://rest.paycomet.com"
PAYCOMET_API_URL_TEST       = "https://rest.paycomet.com"   # same host, different terminal


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[('jetframe', "Paycomet JET Frame")],
        ondelete={'jetframe': 'set default'},
    )

    paycomet_terminal_id = fields.Char(
        string="Terminal ID",
        required_if_provider='jetframe',
        help="Número de terminal asignado en el back-office de Paycomet.",
    )

    paycomet_api_key = fields.Char(
        string="API Key",
        required_if_provider='jetframe',
        groups='base.group_system',
        help="API Key del terminal. Visible solo para administradores del sistema.",
    )

    paycomet_api_url = fields.Char(
        string="URL API Paycomet",
        required_if_provider='jetframe',
        default=PAYCOMET_API_URL_PRODUCTION,
        help=(
            "URL base de la API REST de Paycomet.\n"
            "• Producción: https://rest.paycomet.com\n"
            "• El terminal de test de Paycomet usa la misma URL base pero con "
            "credenciales (terminal + API Key) distintas.\n"
            "Modifica este campo solo si Paycomet te ha proporcionado "
            "una URL de API diferente (p. ej. para un entorno sandbox dedicado)."
        ),
    )

    # ------------------------------------------------------------------
    # Computed helper — returns the full /v1/form endpoint URL
    # Used by payment.transaction to call the JetFrame form API.
    # ------------------------------------------------------------------

    @api.depends('paycomet_api_url')
    def _compute_paycomet_form_url(self):
        for provider in self:
            base = (provider.paycomet_api_url or PAYCOMET_API_URL_PRODUCTION).rstrip('/')
            provider.paycomet_form_url = f"{base}/v1/form"

    paycomet_form_url = fields.Char(
        string="Endpoint /v1/form (calculado)",
        compute='_compute_paycomet_form_url',
        store=False,
        help="URL completa del endpoint /v1/form, calculada a partir de la URL base.",
    )

    @api.depends('paycomet_api_url')
    def _compute_paycomet_operation_info_url(self):
        for provider in self:
            base = (provider.paycomet_api_url or PAYCOMET_API_URL_PRODUCTION).rstrip('/')
            provider.paycomet_operation_info_url = f"{base}/v1/payments/{{order}}/info"

    paycomet_operation_info_url = fields.Char(
        string="Endpoint /v1/payments (calculado)",
        compute='_compute_paycomet_operation_info_url',
        store=False,
    )

    def _get_default_payment_method_codes(self):
        self.ensure_one()
        if self.code != 'jetframe':
            return super()._get_default_payment_method_codes()
        return {'card', 'instant_credit'}
