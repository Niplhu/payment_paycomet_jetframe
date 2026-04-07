import logging

from odoo import _, http
from odoo.addons.payment.controllers.post_processing import PaymentPostProcessing
from odoo.exceptions import RedirectWarning
from odoo.http import request

_logger = logging.getLogger(__name__)

_BREAKOUT_HTML = """\
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>Procesando pago...</title>
    <style>
        body {{ font-family: system-ui, sans-serif; display: flex;
               align-items: center; justify-content: center;
               min-height: 100vh; margin: 0; background: #f8f9fa; }}
        .msg {{ color: #6c757d; font-size: .9rem; text-align: center; }}
        .spinner {{ width: 28px; height: 28px; border: 3px solid #dee2e6;
                   border-top-color: #6c757d; border-radius: 50%;
                   animation: spin .8s linear infinite; margin: 0 auto 12px; }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    </style>
</head>
<body>
    <div class="msg">
        <div class="spinner"></div>
        <p>Procesando, por favor espere&hellip;</p>
    </div>
    <script>
        var dest = '/payment/status';
        try {{
            if (window !== window.top) {{
                window.top.location.href = dest;
            }} else {{
                window.location.href = dest;
            }}
        }} catch (e) {{
            window.location.href = dest;
        }}
    </script>
</body>
</html>"""


class PaycometJetController(http.Controller):

    @http.route('/payment/jetframe/return', type='http', auth='public', methods=['GET', 'POST'], csrf=False)
    def jetframe_return(self, **data):
        _logger.info(
            "Paycomet return: reference=%s status=%s full_data=%s",
            data.get('reference'),
            data.get('status'),
            data,
        )
        try:
            request.env['payment.transaction'].sudo()._handle_notification_data('jetframe', dict(data))
        except Exception:
            _logger.exception(
                "Paycomet return processing failed: reference=%s order=%s",
                data.get('reference'),
                data.get('order'),
            )
        return request.make_response(
            _BREAKOUT_HTML,
            headers=[('Content-Type', 'text/html; charset=utf-8')],
        )


class PaycometJetPostProcessing(PaymentPostProcessing):

    @http.route('/payment/status/poll', type='json', auth='public')
    def poll_status(self, **kwargs):
        monitored_tx = self._get_monitored_transaction()
        if not monitored_tx or monitored_tx.provider_code != 'jetframe':
            return super().poll_status(**kwargs)

        try:
            return super().poll_status(**kwargs)
        except RedirectWarning as warning:
            request.env.cr.rollback()
            _logger.exception(
                "Paycomet JET: redirect warning in payment/status/poll for tx %s: %s",
                monitored_tx.reference,
                warning,
            )
            if monitored_tx.state not in ('done', 'cancel'):
                monitored_tx._set_error(_(
                    "El pago fue autorizado, pero falta configuracion contable del proveedor. "
                    "Contacta con administracion para revisar diario y metodos de pago."
                ))
            monitored_tx.is_post_processed = True
            return {
                'provider_code': monitored_tx.provider_code,
                'state': monitored_tx.state,
                'landing_route': monitored_tx.landing_route,
            }
