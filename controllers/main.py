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
    <!-- Fallback: meta-refresh si los scripts inline están bloqueados por CSP -->
    <meta http-equiv="refresh" content="4;url=/payment/status"/>
    <style>
        body { font-family: system-ui, sans-serif; display: flex;
               align-items: center; justify-content: center;
               min-height: 100vh; margin: 0; background: #f8f9fa; }
        .msg { color: #6c757d; font-size: .9rem; text-align: center; }
        .spinner { width: 28px; height: 28px; border: 3px solid #dee2e6;
                   border-top-color: #6c757d; border-radius: 50%;
                   animation: spin .8s linear infinite; margin: 0 auto 12px; }
        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
    <div class="msg">
        <div class="spinner"></div>
        <p>Procesando, por favor espere&hellip;</p>
    </div>
    <script>
        (function () {
            var dest = '/payment/status';

            /* 1. postMessage al frame padre (funciona aunque CSP bloquee
                  navegación directa desde un iframe con sandbox). */
            try {
                window.parent.postMessage(
                    {type: 'paycomet_jetframe_done', dest: dest},
                    window.location.origin
                );
            } catch (ignore) {}

            /* 2. Navegación directa — funciona en contexto same-origin o
                  cuando la página se sirve en el nivel superior (3DS redirect
                  de página completa). */
            try {
                if (window !== window.top) {
                    /* Estamos dentro del iframe — navegar la ventana raíz. */
                    window.top.location.href = dest;
                } else {
                    /* Contexto de nivel superior (retorno 3DS full-page). */
                    window.location.href = dest;
                }
            } catch (e) {
                /* Último recurso: navegar la ventana actual. */
                window.location.href = dest;
            }
        })();
    </script>
</body>
</html>"""


class PaycometJetController(http.Controller):

    @http.route('/payment/jetframe/status', type='json', auth='public')
    def jetframe_status(self, reference=None, order=None, **kwargs):
        tx = request.env['payment.transaction'].sudo()
        if reference:
            tx = tx.search([
                ('reference', '=', reference),
                ('provider_code', '=', 'jetframe'),
            ], limit=1)
        elif order:
            tx = tx.search([
                ('paycomet_order', '=', order),
                ('provider_code', '=', 'jetframe'),
            ], limit=1)
        else:
            tx = request.env['payment.transaction']

        if not tx:
            return {'found': False}

        return {
            'found': True,
            'state': tx.state,
            'state_message': tx.state_message,
            'reference': tx.reference,
            'landing_route': tx.landing_route,
        }

    @http.route('/payment/jetframe/return', type='http', auth='public', methods=['GET', 'POST'], csrf=False)
    def jetframe_return(self, **data):
        notification = _normalise_paycomet_params(data)
        _logger.info(
            "Paycomet return: reference=%s order=%s status=%s full_data=%s",
            notification.get('reference'),
            notification.get('order'),
            notification.get('status'),
            notification,
        )
        try:
            request.env['payment.transaction'].sudo()._handle_notification_data(
                'jetframe', notification,
            )
        except Exception:
            _logger.exception(
                "Paycomet return processing failed: reference=%s order=%s",
                notification.get('reference'),
                notification.get('order'),
            )
        return request.make_response(
            _BREAKOUT_HTML,
            headers=[('Content-Type', 'text/html; charset=utf-8')],
        )

    @http.route('/payment/jetframe/notify', type='http', auth='public', methods=['POST'], csrf=False, save_session=False)
    def jetframe_notify(self, **data):
        notification = _normalise_paycomet_params(data)
        _logger.info(
            "Paycomet notify: order=%s response=%s error=%s",
            notification.get('order'),
            notification.get('Response') or notification.get('response'),
            notification.get('errorCode'),
        )
        try:
            request.env['payment.transaction'].sudo()._handle_notification_data(
                'jetframe', notification,
            )
        except Exception:
            _logger.exception(
                "Paycomet notify processing failed: order=%s",
                notification.get('order'),
            )
        return request.make_response('OK', headers=[('Content-Type', 'text/plain')])


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


def _normalise_paycomet_params(data):
    notification = dict(data)

    if 'Order' in notification and 'order' not in notification:
        notification['order'] = notification['Order']

    if 'ErrorID' in notification and 'errorCode' not in notification:
        notification['errorCode'] = notification['ErrorID']

    if 'ErrorCode' in notification and 'errorCode' not in notification:
        notification['errorCode'] = notification['ErrorCode']

    if 'ErrorDescription' in notification and 'errorDescription' not in notification:
        notification['errorDescription'] = notification['ErrorDescription']

    if 'Amount' in notification and 'amount' not in notification:
        notification['amount'] = notification['Amount']

    if 'Currency' in notification and 'currency' not in notification:
        notification['currency'] = notification['Currency']

    if 'status' not in notification and 'Response' in notification:
        response_val = (notification['Response'] or '').strip().upper()
        notification['status'] = 'ok' if response_val == 'OK' else 'ko'

    return notification
