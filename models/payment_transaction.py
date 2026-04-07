import ipaddress
import logging
import re
from urllib.parse import urlencode, urlsplit

try:
    import requests as req_lib
except ImportError:
    req_lib = None

from odoo import _, fields, models
from odoo.exceptions import ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paycomet REST API endpoints
# ---------------------------------------------------------------------------
PAYCOMET_FORM_URL = "https://rest.paycomet.com/v1/form"
PAYCOMET_OPERATION_INFO_URL = "https://rest.paycomet.com/v1/payments/{order}/info"

# ---------------------------------------------------------------------------
# Paycomet payment method IDs (methodId field in /v1/form)
# ---------------------------------------------------------------------------
PAYCOMET_METHOD_CARD = 1
PAYCOMET_METHOD_INSTANT_CREDIT = 33

# ---------------------------------------------------------------------------
# Local error code map — avoids blocking API calls in the payment flow.
# Source: https://docs.paycomet.com/es/api/error-codes
# is_cancel=True → use _set_canceled; False → use _set_error
# ---------------------------------------------------------------------------
PAYCOMET_ERROR_CODES = {
    # Cancelaciones / abandonos
    1115: ("Operación cancelada.", True),
    1129: ("El pago fue cancelado por el usuario.", True),
    # Datos de tarjeta
    1003: ("Datos de tarjeta incorrectos. Comprueba el número, fecha y CVV.", False),
    1004: ("Tarjeta no válida o caducada.", False),
    1005: ("Fondos insuficientes.", False),
    1006: ("Tarjeta bloqueada o restringida.", False),
    1010: ("Operación denegada por el banco emisor.", False),
    1015: ("Número de intentos excedido. Inténtalo más tarde.", False),
    # Importe
    1110: ("El importe mínimo requerido no se ha alcanzado.", False),
    1111: ("El importe supera el máximo permitido.", False),
    1112: ("Importe no válido.", False),
    # 3D Secure / autenticación
    1123: ("Autenticación 3D Secure fallida.", False),
    1124: ("Tiempo de espera agotado durante la autenticación 3DS.", False),
    1125: ("El banco emisor no soporta 3D Secure.", False),
    # Técnicos / comunicación
    1000: ("Error interno de Paycomet. Inténtalo de nuevo.", False),
    1001: ("Terminal no encontrado o inactivo.", False),
    1002: ("Credenciales de terminal incorrectas.", False),
    1050: ("Referencia de pedido duplicada.", False),
    1053: ("La orden ya fue procesada.", False),
    # Instant Credit
    9050: ("Solicitud de crédito rechazada por el proveedor financiero.", False),
    9051: ("Documentación requerida para el crédito no disponible.", False),
    9055: ("Límite de crédito superado.", False),
}

# ISO 3166-1 numeric codes keyed by alpha-2
ISO_3166_NUMERIC_BY_ALPHA2 = {
    'AD': '020', 'AE': '784', 'AT': '040', 'AU': '036',
    'BE': '056', 'BR': '076', 'CA': '124', 'CH': '756',
    'CL': '152', 'CN': '156', 'CO': '170', 'CZ': '203',
    'DE': '276', 'DK': '208', 'EC': '218', 'EE': '233',
    'EG': '818', 'ES': '724', 'FI': '246', 'FR': '250',
    'GB': '826', 'GR': '300', 'HK': '344', 'HR': '191',
    'HU': '348', 'ID': '360', 'IE': '372', 'IL': '376',
    'IN': '356', 'IT': '380', 'JP': '392', 'KR': '410',
    'LT': '440', 'LU': '442', 'LV': '428', 'MA': '504',
    'MX': '484', 'MY': '458', 'NL': '528', 'NO': '578',
    'NZ': '554', 'PE': '604', 'PH': '608', 'PL': '616',
    'PT': '620', 'RO': '642', 'RS': '688', 'RU': '643',
    'SA': '682', 'SE': '752', 'SG': '702', 'SI': '705',
    'SK': '703', 'TH': '764', 'TR': '792', 'TW': '158',
    'UA': '804', 'US': '840', 'UY': '858', 'VE': '862',
    'VN': '704', 'ZA': '710',
}


def _parse_error_code(raw):
    """Return integer error code or None."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _resolve_error(error_code):
    """
    Return (user_message: str, is_cancel: bool) for a Paycomet error code.
    Never makes network calls — uses the local map only.
    """
    code = _parse_error_code(error_code)
    if code is None:
        return None, False
    msg, is_cancel = PAYCOMET_ERROR_CODES.get(code, (None, False))
    if not msg:
        msg = _("Pago rechazado (código Paycomet: %s).") % code
    return msg, is_cancel


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    paycomet_order = fields.Char(
        string="Paycomet Order Ref",
        readonly=True,
        copy=False,
        help="Referencia de orden enviada a Paycomet (máx. 12 chars alfanuméricos).",
    )

    # =========================================================================
    # Odoo hook: rendering values (called during payment initiation)
    # =========================================================================

    def _get_specific_rendering_values(self, processing_values):
        self.ensure_one()
        values = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'jetframe':
            return values

        challenge_url = self._jetframe_get_challenge_url(processing_values)
        if not challenge_url:
            raise ValidationError(_("Paycomet no devolvió una URL de formulario válida."))

        values['form_url'] = challenge_url
        return values

    # =========================================================================
    # Core: call /v1/form and return the challenge URL
    # =========================================================================

    def _jetframe_get_challenge_url(self, processing_values=None):
        """
        Server-side call to Paycomet /v1/form.

        Returns the challengeUrl string or raises ValidationError.

        Both CARD and INSTANT_CREDIT use /v1/form (JetFrame hosted form).
        The difference is the `methods` list sent in the payload.

        NEVER call /v1/payments here — that is the direct charge API (requires
        a stored token) and has nothing to do with JetFrame.
        """
        self.ensure_one()
        if req_lib is None:
            raise ValidationError(
                _("La dependencia Python 'requests' no está instalada en el servidor.")
            )

        provider = self.provider_id
        if not provider.paycomet_api_key:
            raise ValidationError(_("Configura la API Key de Paycomet en el proveedor de pago."))

        try:
            terminal_id = int(provider.paycomet_terminal_id)
        except (TypeError, ValueError):
            raise ValidationError(_("El Terminal ID de Paycomet debe ser un número entero."))

        if self.amount <= 0:
            raise ValidationError(_("El importe del pago debe ser mayor que cero."))

        base_url = self._jetframe_public_base_url()
        order_ref = self._jetframe_build_order()
        self.sudo().write({'paycomet_order': order_ref})

        amount_cents = self._jetframe_amount_in_cents()

        url_ok = "{base}/payment/jetframe/return?{qs}".format(
            base=base_url,
            qs=urlencode({'reference': self.reference, 'order': order_ref, 'status': 'ok'}),
        )
        url_ko = "{base}/payment/jetframe/return?{qs}".format(
            base=base_url,
            qs=urlencode({'reference': self.reference, 'order': order_ref, 'status': 'ko'}),
        )
        url_notify = "{base}/payment/jetframe/notify".format(base=base_url)

        method_id = self._jetframe_payment_method_id(processing_values)
        client_ip = self._jetframe_client_ip()

        # -----------------------------------------------------------------
        # /v1/form payload — used for ALL payment methods (card AND credit)
        # operationType 1 = Authorization + Capture (debit)
        # -----------------------------------------------------------------
        payment_payload = {
            'terminal': terminal_id,
            'order': order_ref,
            'amount': amount_cents,
            'currency': self.currency_id.name,
            'methods': [method_id],          # [1] = card, [33] = instant credit
            'excludedMethods': [],
            'secure': 1,                     # Force 3DS
            'userInteraction': 1,            # User is present in browser
            'urlOk': url_ok,
            'urlKo': url_ko,
            'urlNotification': url_notify,
            'productDescription': (self.reference or '')[:255],
            'merchantData': self._jetframe_merchant_data(),
        }
        if client_ip:
            payment_payload['originalIp'] = client_ip

        payload = {
            'operationType': 1,
            'language': 'es',
            'payment': payment_payload,
        }

        _logger.info(
            "Paycomet JET /v1/form request: ref=%s order=%s amount=%s method=%s",
            self.reference, order_ref, amount_cents, method_id,
        )

        try:
            resp = req_lib.post(
                PAYCOMET_FORM_URL,
                json=payload,
                headers=self._jetframe_api_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except req_lib.exceptions.RequestException as exc:
            _logger.error(
                "Paycomet JET: error de red llamando /v1/form ref=%s: %s",
                self.reference, exc,
            )
            raise ValidationError(
                _("Error de comunicación con Paycomet. Inténtalo de nuevo.")
            )

        if not isinstance(data, dict):
            _logger.error(
                "Paycomet JET: respuesta /v1/form no es JSON ref=%s body=%s",
                self.reference, data,
            )
            raise ValidationError(
                _("Paycomet devolvió una respuesta inválida. Contacta con soporte.")
            )

        error_code = _parse_error_code(data.get('errorCode', 0))
        challenge_url = self._jetframe_extract_challenge_url(data)

        if challenge_url and error_code == 0:
            _logger.info(
                "Paycomet JET: challengeUrl obtenida ref=%s url=%s",
                self.reference, challenge_url[:60] + '...',
            )
            return challenge_url

        # Error from Paycomet — surface a useful message
        error_msg, _ = _resolve_error(error_code)
        error_from_api = data.get('errorDescription') or ''
        final_msg = error_from_api or error_msg or _("Error desconocido de Paycomet.")

        _logger.error(
            "Paycomet JET: /v1/form retornó error ref=%s code=%s desc=%s body=%s",
            self.reference, error_code, final_msg, data,
        )
        raise ValidationError(_("Paycomet: %s") % final_msg)

    # =========================================================================
    # Helpers — computation & formatting
    # =========================================================================

    def _jetframe_public_base_url(self):
        """
        Return the public HTTPS base URL for Odoo.

        In test mode (provider.state == 'test') HTTP is also accepted to
        allow development without a TLS tunnel.
        """
        self.ensure_one()
        base_url = (self.provider_id.get_base_url() or '').strip().rstrip('/')
        if not base_url:
            raise ValidationError(
                _("Configura la URL base pública de Odoo (Ajustes → web.base.url).")
            )

        parsed = urlsplit(base_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValidationError(_("La URL base pública de Odoo no es válida: %s") % base_url)

        is_production = self.provider_id.state == 'enabled'
        if is_production and parsed.scheme != 'https':
            raise ValidationError(_(
                "Paycomet requiere HTTPS en producción. "
                "Actualiza Ajustes → Parámetros técnicos → web.base.url a https://..."
            ))

        if parsed.scheme != 'https':
            _logger.warning(
                "Paycomet JET: URL base no es HTTPS (%s). Aceptado en modo test, "
                "pero Paycomet puede rechazar la llamada.",
                base_url,
            )

        return base_url

    def _jetframe_build_order(self):
        """
        Build a Paycomet order reference from self.reference.

        Paycomet rules:
        - Only alphanumeric [A-Z0-9]
        - 4–12 characters
        """
        self.ensure_one()
        ref = re.sub(r'[^A-Za-z0-9]', '', (self.reference or '').upper())

        if ref and len(ref) >= 4:
            return ref[:12]

        # Fallback: TX + zero-padded transaction ID (always >= 4 chars, always unique)
        tx_id = str(self.id or 0).zfill(10)
        if ref:
            return (ref + tx_id)[:12]
        return ('TX' + tx_id)[:12]

    def _jetframe_amount_in_cents(self):
        """Convert self.amount to Paycomet integer cents string."""
        self.ensure_one()
        currency = self.currency_id
        decimals = currency.decimal_places if currency else 2
        if decimals == 0:
            return str(int(round(self.amount)))
        return str(int(round(self.amount * (10 ** decimals))))

    def _jetframe_payment_method_id(self, processing_values=None):
        """Return the Paycomet methodId integer for the selected payment method."""
        self.ensure_one()
        code = ''
        if processing_values:
            code = processing_values.get('payment_method_code') or ''
            if not code and processing_values.get('payment_method_id'):
                pm = self.env['payment.method'].browse(
                    processing_values['payment_method_id']
                )
                code = pm.code if pm.exists() else ''
        if not code and self.payment_method_id:
            code = self.payment_method_id.code or ''
        code = code.strip().lower()

        if code in ('credit', 'instant_credit'):
            return PAYCOMET_METHOD_INSTANT_CREDIT
        return PAYCOMET_METHOD_CARD

    def _jetframe_client_ip(self):
        """Return the real client IP, respecting reverse-proxy headers."""
        self.ensure_one()
        if not (request and request.httprequest):
            return None
        r = request.httprequest
        for header in ('X-Forwarded-For', 'X-Real-IP'):
            value = (r.headers.get(header) or '').strip()
            if value:
                candidate = value.split(',')[0].strip()
                try:
                    ipaddress.ip_address(candidate)
                    return candidate
                except ValueError:
                    continue
        try:
            ipaddress.ip_address(r.remote_addr or '')
            return r.remote_addr
        except ValueError:
            return None

    def _jetframe_api_headers(self):
        self.ensure_one()
        return {
            'PAYCOMET-API-TOKEN': self.provider_id.paycomet_api_key,
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

    def _jetframe_extract_challenge_url(self, payload):
        """
        Find the challengeUrl in a Paycomet /v1/form response.

        The URL is at the top level in current API versions:
        {"errorCode": 0, "challengeUrl": "https://jetframe.paycomet.com/..."}
        """
        if not isinstance(payload, dict):
            return None
        # Check top-level first, then common nested keys
        for container in (payload, payload.get('payment', {}), payload.get('data', {})):
            if not isinstance(container, dict):
                continue
            for key in ('challengeUrl', 'challengeURL', 'challenge_url'):
                url = container.get(key)
                if isinstance(url, str) and url.strip():
                    parsed = urlsplit(url.strip())
                    if parsed.scheme in ('http', 'https') and parsed.netloc:
                        return url.strip()
        return None

    def _jetframe_merchant_data(self):
        """Build the merchantData object for the /v1/form payload."""
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id
        parts = (partner.name or '').split()
        customer = {
            'id': str(partner.id),
            'name': (parts[0] if parts else '')[:254],
            'surname': (' '.join(parts[1:]) if len(parts) > 1 else partner.name or '')[:254],
        }
        if partner.email:
            customer['email'] = partner.email.strip()

        billing = {}
        country_numeric = self._jetframe_country_numeric(partner.country_id)
        if country_numeric:
            billing['billAddrCountry'] = country_numeric
        for field, key, maxlen in (
            ('city', 'billAddrCity', 50),
            ('street', 'billAddrLine1', 50),
            ('street2', 'billAddrLine2', 50),
            ('zip', 'billAddrPostCode', 16),
        ):
            value = getattr(partner, field, None) or ''
            if value:
                billing[key] = value[:maxlen]

        result = {'customer': customer}
        if billing:
            result['billing'] = billing
        return result

    def _jetframe_country_numeric(self, country):
        if not country:
            return None
        code = (country.code or '').strip().upper()
        if code.isdigit() and len(code) == 3:
            return code
        return ISO_3166_NUMERIC_BY_ALPHA2.get(code)

    # =========================================================================
    # Operation info — only called when strictly necessary
    # =========================================================================

    def _jetframe_get_operation_info(self, order_ref, attempts=2, delay_ms=500):
        """
        Query Paycomet for the current operation state.

        Only call this when the browser return does NOT carry enough data to
        determine the final state (i.e., KO without an error code).
        Keep attempts low — this runs synchronously in the request cycle.
        """
        self.ensure_one()
        if not order_ref or req_lib is None:
            return {}

        terminal_id = self.provider_id.paycomet_terminal_id
        if not terminal_id:
            return {}

        endpoint = PAYCOMET_OPERATION_INFO_URL.format(order=order_ref)
        payload = {'payment': {'terminal': int(terminal_id), 'order': order_ref}}

        import time
        for attempt in range(1, attempts + 1):
            try:
                resp = req_lib.post(
                    endpoint,
                    json=payload,
                    headers=self._jetframe_api_headers(),
                    timeout=10,
                )
                data = resp.json()
                _logger.debug(
                    "Paycomet JET operationInfo: ref=%s order=%s attempt=%s/%s resp=%s",
                    self.reference, order_ref, attempt, attempts, data,
                )
                if isinstance(data, dict):
                    return data.get('payment', data)
            except Exception as exc:
                _logger.warning(
                    "Paycomet JET operationInfo error: ref=%s attempt=%s/%s: %s",
                    self.reference, attempt, attempts, exc,
                )
            if attempt < attempts:
                time.sleep(delay_ms / 1000.0)

        return {}

    # =========================================================================
    # Odoo hooks: transaction creation / post-processing
    # =========================================================================

    def _create_payment(self, **extra_create_values):
        self.ensure_one()
        if self.provider_code != 'jetframe':
            return super()._create_payment(**extra_create_values)

        provider = self.provider_id

        # Ensure journal has a usable inbound payment method line
        if not provider.journal_id:
            journal = self.env['account.journal'].search(
                [('company_id', '=', provider.company_id.id), ('type', '=', 'bank')],
                limit=1,
            )
            if journal:
                provider.journal_id = journal

        pml = (
            provider.journal_id.inbound_payment_method_line_ids.filtered(
                lambda l: l.payment_provider_id == provider
            )[:1]
            or provider.journal_id.inbound_payment_method_line_ids[:1]
        )
        if not pml:
            raise ValidationError(_(
                "El diario del proveedor Paycomet JET no tiene ninguna línea de método "
                "de pago entrante configurada. Ve a Contabilidad → Diarios → ← diario → "
                "Pagos entrantes y añade una línea."
            ))

        extra_create_values.setdefault('payment_method_line_id', pml.id)
        return super()._create_payment(**extra_create_values)

    # =========================================================================
    # Odoo hooks: notification handling
    # =========================================================================

    def _get_tx_from_notification_data(self, provider_code, notification_data):
        if provider_code != 'jetframe':
            return super()._get_tx_from_notification_data(provider_code, notification_data)

        # 1. Try by Odoo reference (injected into urlOk/urlKo as ?reference=...)
        reference = (notification_data.get('reference') or '').strip()
        if reference:
            tx = self.search(
                [('reference', '=', reference), ('provider_code', '=', 'jetframe')],
                limit=1,
            )
            if tx:
                return tx

        # 2. Fallback: try by Paycomet order ref
        order_ref = (
            notification_data.get('order')
            or notification_data.get('Order')
            or ''
        ).strip()
        if order_ref:
            tx = self.search(
                [('paycomet_order', '=', order_ref), ('provider_code', '=', 'jetframe')],
                limit=1,
            )
            if tx:
                return tx

        raise ValidationError(
            _("Paycomet JET: no se encontró la transacción. reference=%s order=%s")
            % (reference, order_ref)
        )

    def _process_notification_data(self, notification_data):
        if self.provider_code != 'jetframe':
            return super()._process_notification_data(notification_data)

        status = (notification_data.get('status') or '').strip().lower()
        order_ref = (
            notification_data.get('order')
            or notification_data.get('Order')
            or self.paycomet_order
            or ''
        ).strip()

        _logger.info(
            "Paycomet JET notification: ref=%s order=%s status=%s tx_state=%s data=%s",
            self.reference, order_ref, status, self.state, notification_data,
        )

        # Guard: do not re-process terminal states
        if self.state in ('done', 'cancel', 'error'):
            _logger.info(
                "Paycomet JET: tx ref=%s already in terminal state '%s', ignoring.",
                self.reference, self.state,
            )
            return

        if status == 'ok':
            self._jetframe_handle_ok(notification_data, order_ref)
        elif status == 'ko':
            self._jetframe_handle_ko(notification_data, order_ref)
        else:
            # Unexpected status — set error so the transaction doesn't stay in draft
            _logger.warning(
                "Paycomet JET: unexpected status '%s' for ref=%s", status, self.reference,
            )
            self._set_error(_("Paycomet: estado desconocido recibido ('%s').") % status)

    def _jetframe_handle_ok(self, notification_data, order_ref):
        """Process a successful payment notification (status=ok)."""
        self.ensure_one()
        is_instant_credit = (self._jetframe_payment_method_id() == PAYCOMET_METHOD_INSTANT_CREDIT)

        if not is_instant_credit:
            # Standard card: urlOk only fires after bank authorization.
            # S2S notify is authoritative but browser return can arrive first — accept it.
            self._set_done(
                state_message=_("Pago autorizado y confirmado por Paycomet.")
            )
            return

        # Instant Credit: provider confirmation is asynchronous.
        # Query operationInfo to get the real state before deciding.
        op = self._jetframe_get_operation_info(order_ref, attempts=3, delay_ms=800)
        op_state = _parse_error_code(op.get('state'))

        if op_state == 1:
            self._set_done(
                state_message=_("Crédito confirmado por Paycomet Instant Credit.")
            )
        elif op_state == 2:
            self._set_pending(
                state_message=_(
                    "Pago en estado pendiente según Paycomet Instant Credit. "
                    "Se confirmará automáticamente cuando el proveedor financiero lo autorice."
                )
            )
        else:
            # Unknown or no op info — leave as pending for manual review
            _logger.warning(
                "Paycomet JET: Instant Credit op_state=%s desconocido ref=%s — marcando pending",
                op_state, self.reference,
            )
            self._set_pending(
                state_message=_(
                    "Retorno de Instant Credit recibido. Pendiente de confirmación final."
                )
            )

    def _jetframe_handle_ko(self, notification_data, order_ref):
        """Process a failed or cancelled payment notification (status=ko)."""
        self.ensure_one()

        # Error code may come from Paycomet URL params (browser return) or S2S body
        error_code = (
            notification_data.get('ErrorCode')
            or notification_data.get('errorCode')
            or notification_data.get('error_code')
        )

        user_msg, is_cancel = _resolve_error(error_code)

        # If we don't have an error code from the notification (e.g., browser return
        # without extra Paycomet params), call operationInfo for more detail.
        # Keep attempts low — this is in the user's request cycle.
        if not error_code and order_ref:
            op = self._jetframe_get_operation_info(order_ref, attempts=2, delay_ms=400)
            op_state = _parse_error_code(op.get('state'))

            if op_state == 1:
                # operationInfo says OK despite KO return — race condition, accept as done
                _logger.info(
                    "Paycomet JET: operationInfo confirms state=1 despite KO return ref=%s",
                    self.reference,
                )
                self._set_done(
                    state_message=_("Pago confirmado por operationInfo (retorno KO ignorado).")
                )
                return

            # Enrich error info from operationInfo
            error_code = error_code or op.get('errorCode') or op.get('ErrorCode')
            user_msg, is_cancel = _resolve_error(error_code)

        final_msg = "Paycomet: " + (
            user_msg
            or notification_data.get('errorDescription')
            or _("Pago rechazado.")
        )

        _logger.info(
            "Paycomet JET: payment KO ref=%s error_code=%s is_cancel=%s msg=%s",
            self.reference, error_code, is_cancel, final_msg,
        )

        if is_cancel:
            self._set_canceled(state_message=final_msg)
        else:
            self._set_error(final_msg)
