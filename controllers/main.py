import hashlib
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTML page served inside the Paycomet iframe after urlOk / urlKo redirect.
# Breaks out of the iframe and navigates the parent window to /payment/status.
# ---------------------------------------------------------------------------
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
        var dest = {dest!r};
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

    # =========================================================================
    # /payment/jetframe/return — browser return from Paycomet (urlOk / urlKo)
    # =========================================================================

    @http.route(
        '/payment/jetframe/return',
        type='http',
        auth='public',
        methods=['GET', 'POST'],
        csrf=False,
        save_session=False,
    )
    def jetframe_return(self, **data):
        """
        Called by Paycomet when the user completes (or abandons) the hosted form.

        This URL is loaded inside our iframe overlay.  We respond with an HTML
        page that uses JavaScript to break out of the iframe and navigate the
        parent window to /payment/status.

        Paycomet appends its own params to urlOk/urlKo (Order, AuthCode,
        ErrorCode, Response, etc.) in addition to our custom params
        (reference, order, status).
        """
        _logger.info(
            "Paycomet JET browser return: reference=%s order=%s status=%s data=%s",
            data.get('reference'), data.get('order'), data.get('status'), data,
        )

        try:
            # Normalise Paycomet URL params so _process_notification_data can
            # read them regardless of casing.
            notification = _normalise_paycomet_params(data)
            request.env['payment.transaction'].sudo()._handle_notification_data(
                'jetframe', notification
            )
        except Exception:
            _logger.exception(
                "Paycomet JET: browser return processing failed — reference=%s",
                data.get('reference'),
            )

        html = _BREAKOUT_HTML.format(dest='/payment/status')
        return request.make_response(
            html,
            headers=[('Content-Type', 'text/html; charset=utf-8')],
        )

    # =========================================================================
    # /payment/jetframe/notify — server-to-server (IPN) notification
    # =========================================================================

    @http.route(
        '/payment/jetframe/notify',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
        save_session=False,
    )
    def jetframe_notify(self, **data):
        """
        Server-to-server (IPN/webhook) notification from Paycomet.

        Paycomet POSTs here asynchronously when the payment status changes.
        This is the authoritative channel — it includes full error details and
        fires even when the user closes the browser before returning.

        Paycomet signs the notification with:
        SHA-512(PAYCOMET-API-TOKEN + terminal + Order + Amount + Currency + Response)
        All values are strings, concatenated without separators.
        """
        _logger.info(
            "Paycomet JET S2S notify: order=%s response=%s data=%s",
            data.get('Order'), data.get('Response'), data,
        )

        # --- Signature verification (reject if invalid, skip if absent) -------
        received_sig = (data.get('Signature') or data.get('signature') or '').strip()
        if received_sig and not self._jetframe_verify_s2s_signature(data, received_sig):
            _logger.warning(
                "Paycomet JET: S2S signature INVALID for order=%s — rejecting.",
                data.get('Order'),
            )
            return request.make_response(
                'INVALID_SIGNATURE',
                headers=[('Content-Type', 'text/plain')],
                status=400,
            )

        # Normalise casing and add our `status` key
        notification = _normalise_paycomet_params(data)

        try:
            request.env['payment.transaction'].sudo()._handle_notification_data(
                'jetframe', notification
            )
        except Exception:
            _logger.exception(
                "Paycomet JET: S2S processing failed — data=%s", notification,
            )

        # Paycomet expects HTTP 200 "OK" to stop retrying
        return request.make_response(
            'OK',
            headers=[('Content-Type', 'text/plain')],
        )

    # =========================================================================
    # Internal helpers
    # =========================================================================

    @staticmethod
    def _jetframe_verify_s2s_signature(data, received_sig):
        """
        Verify the SHA-512 signature on a Paycomet S2S notification.

        Formula (Paycomet docs):
        SHA-512(api_token + terminal + Order + Amount + Currency + Response)

        All fields are cast to str and concatenated without separator.
        The signature in the notification is uppercase hex.
        """
        provider = request.env['payment.provider'].sudo().search(
            [('code', '=', 'jetframe'), ('state', '!=', 'disabled')],
            limit=1,
        )
        if not provider or not provider.paycomet_api_key:
            _logger.warning(
                "Paycomet JET: cannot verify S2S signature — no active provider found."
            )
            return True  # Fail open when provider is not configured

        sig_input = ''.join([
            str(provider.paycomet_api_key),
            str(provider.paycomet_terminal_id or ''),
            str(data.get('Order') or ''),
            str(data.get('Amount') or ''),
            str(data.get('Currency') or ''),
            str(data.get('Response') or ''),
        ])
        expected = hashlib.sha512(sig_input.encode('utf-8')).hexdigest().upper()
        return received_sig.upper() == expected


def _normalise_paycomet_params(data):
    """
    Normalise Paycomet notification/return params to our internal convention.

    Paycomet uses PascalCase in S2S (Order, Amount, Response, ErrorCode).
    Our urlOk/urlKo carry lowercase custom params (reference, order, status).

    After this function:
    - `status` is always 'ok' or 'ko' (from our param or from Paycomet Response)
    - `order` is always set (from our param or from Paycomet Order)
    - `errorCode` is always set when available
    """
    n = dict(data)

    # Map PascalCase Paycomet fields to camelCase if our keys are absent
    if 'Order' in n and 'order' not in n:
        n['order'] = n['Order']

    if 'ErrorCode' in n and 'errorCode' not in n:
        n['errorCode'] = n['ErrorCode']

    if 'Amount' in n and 'amount' not in n:
        n['amount'] = n['Amount']

    if 'Currency' in n and 'currency' not in n:
        n['currency'] = n['Currency']

    # Derive `status` from Paycomet `Response` if not already set
    if 'status' not in n and 'Response' in n:
        response_val = (n['Response'] or '').strip().upper()
        n['status'] = 'ok' if response_val == 'OK' else 'ko'

    return n
