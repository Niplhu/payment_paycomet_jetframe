import logging

from odoo import _, http
from odoo.addons.payment.controllers.post_processing import PaymentPostProcessing
from odoo.exceptions import RedirectWarning
from odoo.http import request

_logger = logging.getLogger(__name__)


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
        return request.redirect('/payment/status')


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
