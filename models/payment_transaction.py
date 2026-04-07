import ipaddress
import logging
import re
import time
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
# Paycomet REST API endpoint paths (base URL is configured on payment.provider)
# ---------------------------------------------------------------------------
PAYCOMET_FORM_PATH            = "/v1/form"
PAYCOMET_PAYMENTS_PATH        = "/v1/payments"
PAYCOMET_OPERATION_INFO_PATH  = "/v1/payments/{order}/info"

# Fallback base URL used only when the provider record is not available
PAYCOMET_DEFAULT_BASE_URL = "https://rest.paycomet.com"

# ---------------------------------------------------------------------------
# Paycomet method IDs (sent in `methods` array of /v1/form payload)
# ---------------------------------------------------------------------------
PAYCOMET_METHOD_CARD = 1           # Standard credit/debit card (3DS)
PAYCOMET_METHOD_INSTANT_CREDIT = 33  # Sabadell Instant Credit financing

# Instant Credit sandbox docs indicate supported test amounts between
# 180 EUR and 800 EUR. Below that range the generated challenge can end up
# failing later inside instantcredit.net instead of returning a clean API error.
IC_MINIMUM_AMOUNT_EUR = 180.0
IC_SANDBOX_MAXIMUM_AMOUNT_EUR = 800.0

# Instant Credit is only available for Spanish residents.
# The billAddrCountry must be 724 (ISO 3166-1 numeric for Spain).
IC_REQUIRED_COUNTRY_NUMERIC = '724'

# ---------------------------------------------------------------------------
# Local error code map
# is_cancel=True  → _set_canceled (user abandoned intentionally)
# is_cancel=False → _set_error    (payment rejected, system error)
# Source: https://docs.paycomet.com/es/api/error-codes
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
    # 3D Secure
    1123: ("Autenticación 3D Secure fallida.", False),
    1124: ("Tiempo de espera agotado durante la autenticación 3DS.", False),
    1125: ("El banco emisor no soporta 3D Secure.", False),
    # Técnicos
    1000: ("Error interno de Paycomet. Inténtalo de nuevo.", False),
    1001: ("Terminal no encontrado o inactivo.", False),
    1002: ("Credenciales de terminal incorrectas.", False),
    1050: ("Referencia de pedido duplicada.", False),
    1053: ("La orden ya fue procesada.", False),
    # Instant Credit — códigos específicos de financiación Sabadell
    9001: ("Solicitud de financiación rechazada.", False),
    9002: ("Datos del solicitante incorrectos o incompletos.", False),
    9003: ("El NIF/NIE introducido no es válido.", False),
    9004: ("El IBAN introducido no es válido.", False),
    9005: ("El solicitante no cumple los requisitos de scoring.", False),
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
    Return (user_message: str, is_cancel: bool).
    Uses the local map only — no network calls.
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

    # -------------------------------------------------------------------------
    # Extra fields for Paycomet JET
    # -------------------------------------------------------------------------

    paycomet_order = fields.Char(
        string="Paycomet Order Ref",
        readonly=True,
        copy=False,
        help="Referencia de orden enviada a Paycomet (máx. 12 chars alfanuméricos).",
    )

    paycomet_is_instant_credit = fields.Boolean(
        string="Instant Credit",
        readonly=True,
        copy=False,
        default=False,
        help="Indica si esta transacción se procesó como financiación Instant Credit de Paycomet.",
    )

    # =========================================================================
    # Odoo hook: rendering values
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
    # Core: call /v1/form → challengeUrl
    # =========================================================================

    def _jetframe_get_challenge_url(self, processing_values=None):
        """
        Build the Paycomet request and return the challengeUrl.

        Card:
          - methods: [1], secure: 1 (forces 3DS)
          - endpoint: /v1/form (hosted method selector/form)
          - urlOk → DONE state

        Instant Credit:
          - methodId: 33, secure: 0 (IC has its own scoring auth, not 3DS)
          - endpoint: /v1/payments (direct payment by selected APM)
          - order ref MUST start with a digit
          - billAddrCountry is MANDATORY and must be 724 (Spain)
          - urlOk → PENDING state (financing decision is async)
          - S2S notify → DONE when Sabadell confirms
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

        # Determine payment method before building the rest of the payload
        method_id = self._jetframe_payment_method_id(processing_values)
        is_ic = (method_id == PAYCOMET_METHOD_INSTANT_CREDIT)

        # Persist the IC flag on the transaction so we can read it at notify time
        self.sudo().write({'paycomet_is_instant_credit': is_ic})

        # ── Instant Credit pre-flight validations ─────────────────────────────
        if is_ic:
            self._jetframe_validate_instant_credit()

        base_url = self._jetframe_public_base_url()
        order_ref = self._jetframe_build_order(is_instant_credit=is_ic)
        self.sudo().write({'paycomet_order': order_ref})

        amount_cents = self._jetframe_amount_in_cents()
        client_ip = self._jetframe_client_ip()

        url_ok = "{base}/payment/jetframe/return?{qs}".format(
            base=base_url,
            qs=urlencode({'reference': self.reference, 'order': order_ref, 'status': 'ok'}),
        )
        url_ko = "{base}/payment/jetframe/return?{qs}".format(
            base=base_url,
            qs=urlencode({'reference': self.reference, 'order': order_ref, 'status': 'ko'}),
        )
        url_notify = "{base}/payment/jetframe/notify".format(base=base_url)

        payment_payload = {
            'terminal': terminal_id,
            'order': order_ref,
            'amount': amount_cents,
            'currency': self.currency_id.name,
            # 3DS: ON for card, OFF for Instant Credit (IC uses own scoring/auth)
            'secure': 0 if is_ic else 1,
            'userInteraction': 1,
            'urlOk': url_ok,
            'urlKo': url_ko,
            'urlNotification': url_notify,
            'productDescription': (self.reference or '')[:255],
            'merchantData': self._jetframe_merchant_data(is_instant_credit=is_ic),
        }
        if client_ip:
            payment_payload['originalIp'] = client_ip

        if is_ic:
            payment_payload['methodId'] = method_id
            endpoint = self._jetframe_payments_url()
            payload = {'payment': payment_payload}
            log_label = '/v1/payments'
        else:
            payment_payload.update({
                'methods': [method_id],
                'excludedMethods': [],
            })
            endpoint = self._jetframe_form_url()
            payload = {
                'operationType': 1,   # 1 = Authorization + Capture
                'language': 'es',
                'payment': payment_payload,
            }
            log_label = '/v1/form'

        _logger.info(
            "Paycomet JET %s: ref=%s order=%s amount=%s method=%s(%s) is_ic=%s",
            log_label, self.reference, order_ref, amount_cents,
            method_id, 'instant_credit' if is_ic else 'card', is_ic,
        )
        _logger.info(
            "Paycomet JET %s endpoint: %s (base=%s)",
            log_label, endpoint, self.provider_id.paycomet_api_url,
        )

        try:
            resp = req_lib.post(
                endpoint,
                json=payload,
                headers=self._jetframe_api_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except req_lib.exceptions.RequestException as exc:
            _logger.error(
                "Paycomet JET: red error en %s ref=%s: %s", log_label, self.reference, exc,
            )
            raise ValidationError(
                _("Error de comunicación con Paycomet. Inténtalo de nuevo.")
            )

        if not isinstance(data, dict):
            _logger.error(
                "Paycomet JET: respuesta %s no es JSON ref=%s body=%s",
                log_label, self.reference, data,
            )
            raise ValidationError(
                _("Paycomet devolvió una respuesta inválida. Contacta con soporte.")
            )

        error_code = _parse_error_code(data.get('errorCode', 0))
        challenge_url = self._jetframe_extract_challenge_url(data)

        if challenge_url and error_code == 0:
            if is_ic:
                challenge_url = self._jetframe_ic_test_url(challenge_url)

            _logger.info(
                "Paycomet JET: challengeUrl ref=%s is_ic=%s url=%.80s",
                self.reference, is_ic, challenge_url,
            )
            return challenge_url

        error_msg, _ = _resolve_error(error_code)
        final_msg = data.get('errorDescription') or error_msg or _("Error desconocido de Paycomet.")
        _logger.error(
            "Paycomet JET: %s error ref=%s code=%s desc=%s body=%s",
            log_label,
            self.reference, error_code, final_msg, data,
        )
        raise ValidationError(_("Paycomet: %s") % final_msg)

    # =========================================================================
    # Instant Credit — specific validations
    # =========================================================================

    def _jetframe_validate_instant_credit(self):
        """
        Validate pre-conditions for Instant Credit before calling /v1/form.

        Raises ValidationError with a user-facing message if anything is wrong.
        These validations avoid an unnecessary API round-trip and give the user
        actionable feedback immediately.
        """
        self.ensure_one()

        # 1. Amount range
        if self.currency_id.name == 'EUR' and self.amount < IC_MINIMUM_AMOUNT_EUR:
            raise ValidationError(_(
                "La financiación Instant Credit requiere un importe mínimo de %(min)s €. "
                "El importe actual es %(amount)s €."
            ) % {
                'min': IC_MINIMUM_AMOUNT_EUR,
                'amount': self.amount,
            })

        if (
            self.provider_id.state != 'enabled'
            and self.currency_id.name == 'EUR'
            and self.amount > IC_SANDBOX_MAXIMUM_AMOUNT_EUR
        ):
            raise ValidationError(_(
                "En el entorno de pruebas de Instant Credit el importe debe estar entre "
                "%(min)s € y %(max)s €. El importe actual es %(amount)s €."
            ) % {
                'min': IC_MINIMUM_AMOUNT_EUR,
                'max': IC_SANDBOX_MAXIMUM_AMOUNT_EUR,
                'amount': self.amount,
            })

        # 2. Country — IC only available for Spain
        partner = self.partner_id.commercial_partner_id
        country_numeric = self._jetframe_country_numeric(partner.country_id)
        if not country_numeric:
            # Try company country as fallback
            country_numeric = self._jetframe_country_numeric(
                self.company_id.partner_id.country_id
            )

        if country_numeric != IC_REQUIRED_COUNTRY_NUMERIC:
            raise ValidationError(_(
                "La financiación Instant Credit solo está disponible para residentes en España. "
                "El país de facturación del cliente debe ser España."
            ))

        # 3. Billing address — required by Paycomet for IC scoring
        if not partner.street or not partner.city or not partner.zip:
            raise ValidationError(_(
                "Para pagar con financiación Instant Credit es necesario "
                "que el cliente tenga dirección de facturación completa "
                "(calle, ciudad y código postal)."
            ))

    # =========================================================================
    # Helpers — computation & formatting
    # =========================================================================

    def _jetframe_is_instant_credit(self):
        """
        Return True if this transaction is for Instant Credit.

        Reads from the persisted field paycomet_is_instant_credit so it works
        reliably at any point in the transaction lifecycle, including when the
        S2S notify arrives (where processing_values is not available).
        """
        self.ensure_one()
        return bool(self.paycomet_is_instant_credit)

    def _jetframe_payment_method_id(self, processing_values=None):
        """
        Return the Paycomet methodId integer for the selected payment method.

        Resolution order:
        1. processing_values['payment_method_code']
        2. processing_values['payment_method_id'] → payment.method record
        3. self.payment_method_id.code
        """
        self.ensure_one()
        code = ''

        if processing_values:
            code = (processing_values.get('payment_method_code') or '').strip().lower()
            if not code and processing_values.get('payment_method_id'):
                pm = self.env['payment.method'].browse(
                    processing_values['payment_method_id']
                )
                if pm.exists():
                    code = (pm.code or '').strip().lower()

        if not code and self.payment_method_id:
            code = (self.payment_method_id.code or '').strip().lower()

        if code in ('instant_credit', 'credit'):
            return PAYCOMET_METHOD_INSTANT_CREDIT
        return PAYCOMET_METHOD_CARD

    def _jetframe_build_order(self, is_instant_credit=False):
        """
        Build a valid Paycomet order reference from self.reference.

        Paycomet rules (both methods):
          - Only [A-Z0-9], 4–12 characters

        Instant Credit ADDITIONAL rule:
          - MUST start with a digit (no leading letters whatsoever)
          Reason: IC references are processed by Sabadell's backend, which
          requires numeric-only or digit-leading alphanumeric identifiers.
        """
        self.ensure_one()

        # Strip all non-alphanumeric characters
        ref = re.sub(r'[^A-Za-z0-9]', '', (self.reference or '').upper())

        if is_instant_credit:
            # Remove all leading letters — IC refs must start with a digit
            ref = re.sub(r'^[A-Z]+', '', ref)

        # Stable numeric suffix derived from the transaction ID (always unique)
        tx_suffix = str(self.id or 0).zfill(10)

        if ref and len(ref) >= 4:
            return ref[:12]

        if ref:
            combined = (ref + tx_suffix)[:12]
            if len(combined) >= 4:
                return combined

        # Full fallback
        if is_instant_credit:
            # Guaranteed digit-leading: just the zero-padded TX id
            return tx_suffix[:12]
        return ('TX' + tx_suffix)[:12]

    def _jetframe_amount_in_cents(self):
        """Convert self.amount to Paycomet integer cents string."""
        self.ensure_one()
        currency = self.currency_id
        decimals = currency.decimal_places if currency else 2
        if decimals == 0:
            return str(int(round(self.amount)))
        return str(int(round(self.amount * (10 ** decimals))))

    def _jetframe_public_base_url(self):
        """Return the public HTTPS base URL for Odoo (enforces HTTPS in production)."""
        self.ensure_one()
        base_url = (self.provider_id.get_base_url() or '').strip().rstrip('/')
        if not base_url:
            raise ValidationError(
                _("Configura la URL base pública de Odoo (Ajustes → web.base.url).")
            )
        parsed = urlsplit(base_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValidationError(
                _("La URL base pública de Odoo no es válida: %s") % base_url
            )
        is_production = self.provider_id.state == 'enabled'
        if is_production and parsed.scheme != 'https':
            raise ValidationError(_(
                "Paycomet requiere HTTPS en producción. "
                "Actualiza Ajustes → Parámetros técnicos → web.base.url a https://..."
            ))
        if parsed.scheme != 'https':
            _logger.warning(
                "Paycomet JET: URL base no es HTTPS (%s). "
                "Válido en test, pero Paycomet puede rechazarla.",
                base_url,
            )
        return base_url

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

    def _jetframe_ic_test_url(self, challenge_url):
        """
        Patch an Instant Credit challengeUrl to its test equivalent when the
        payment provider is NOT in production mode.

        Paycomet's test terminal returns the same instantcredit.net URL as
        production. Loading the production URL with a test token results in
        HTTP 500. The test endpoint is identical but has /test/ inserted after
        /api/ in the path:

          Production: https://api.instantcredit.net/api/transaction/{token}/…
          Test:       https://api.instantcredit.net/api/test/transaction/{token}/…

        We apply the patch only when provider.state != 'enabled' so production
        traffic is never modified.
        """
        self.ensure_one()
        if not challenge_url:
            return challenge_url

        is_production = self.provider_id.state == 'enabled'
        if is_production:
            return challenge_url

        # Insert /test/ if the URL matches the known IC production pattern
        IC_PROD = 'api.instantcredit.net/api/transaction/'
        IC_TEST = 'api.instantcredit.net/api/test/transaction/'

        if IC_PROD in challenge_url:
            patched = challenge_url.replace(IC_PROD, IC_TEST, 1)
            _logger.info(
                "Paycomet JET IC: URL patched to test endpoint ref=%s\n  from: %s\n  to:   %s",
                self.reference, challenge_url, patched,
            )
            return patched

        # URL doesn't match the expected pattern — return as-is and log a warning
        # so the developer can verify the correct test pattern with Paycomet.
        _logger.warning(
            "Paycomet JET IC: provider is in test mode but challengeUrl does not "
            "contain the expected pattern '%s'.\n  URL: %s\n  "
            "If your test IC endpoint has a different pattern, update "
            "_jetframe_ic_test_url() accordingly.",
            IC_PROD, challenge_url,
        )
        return challenge_url

    def _jetframe_form_url(self):
        """Return the full /v1/form endpoint URL from the provider configuration."""
        self.ensure_one()
        base = (
            self.provider_id.paycomet_api_url
            or PAYCOMET_DEFAULT_BASE_URL
        ).rstrip('/')
        return base + PAYCOMET_FORM_PATH

    def _jetframe_payments_url(self):
        """Return the full /v1/payments endpoint URL from the provider configuration."""
        self.ensure_one()
        base = (
            self.provider_id.paycomet_api_url
            or PAYCOMET_DEFAULT_BASE_URL
        ).rstrip('/')
        return base + PAYCOMET_PAYMENTS_PATH

    def _jetframe_operation_info_url(self, order_ref):
        """Return the full /v1/payments/{order}/info endpoint URL."""
        self.ensure_one()
        base = (
            self.provider_id.paycomet_api_url
            or PAYCOMET_DEFAULT_BASE_URL
        ).rstrip('/')
        return base + PAYCOMET_OPERATION_INFO_PATH.format(order=order_ref)

    def _jetframe_extract_challenge_url(self, payload):
        """Find the challengeUrl in a Paycomet /v1/form response dict."""
        if not isinstance(payload, dict):
            return None
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

    def _jetframe_merchant_data(self, is_instant_credit=False):
        """
        Build the merchantData object for /v1/form.

        For Instant Credit:
          - billing.billAddrCountry is MANDATORY (must be 724 for Spain)
          - billing address fields are also included when available
          - customer.phone is included when available (helps IC scoring)

        For card:
          - billing is sent when available but not strictly required
        """
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id

        # Customer block
        parts = (partner.name or '').split()
        customer = {
            'id': str(partner.id),
            'name': (parts[0] if parts else '')[:254],
            'surname': (' '.join(parts[1:]) if len(parts) > 1 else partner.name or '')[:254],
        }
        if partner.email:
            customer['email'] = partner.email.strip()
        if is_instant_credit:
            # PAYCOMET documents homePhone/mobilePhone/workPhone.
            mobile_phone = re.sub(r'[^0-9+]', '', partner.mobile or '')[:20]
            home_phone = re.sub(r'[^0-9+]', '', partner.phone or '')[:20]
            if mobile_phone:
                customer['mobilePhone'] = mobile_phone
            elif home_phone:
                customer['homePhone'] = home_phone

        # Billing block
        billing = {}
        country_numeric = self._jetframe_country_numeric(partner.country_id)
        if not country_numeric and is_instant_credit:
            # For IC, fallback to company country (already validated above)
            country_numeric = self._jetframe_country_numeric(
                self.company_id.partner_id.country_id
            )
        if country_numeric:
            billing['billAddrCountry'] = country_numeric

        for attr, key, maxlen in (
            ('street',  'billAddrLine1',   50),
            ('street2', 'billAddrLine2',   50),
            ('city',    'billAddrCity',    50),
            ('zip',     'billAddrPostCode', 16),
        ):
            value = (getattr(partner, attr, None) or '').strip()
            if value:
                billing[key] = value[:maxlen]

        if partner.state_id and partner.state_id.name:
            billing['billAddrState'] = partner.state_id.name.strip()[:50]

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
    # Operation info — used only for Instant Credit OK confirmation
    # =========================================================================

    def _jetframe_get_operation_info(self, order_ref, attempts=2, delay_ms=600):
        """
        Query Paycomet /v1/payments/{order}/info for the current operation state.

        Only used for Instant Credit, where the confirmation is asynchronous
        and we need to know the real state at the moment urlOk fires.

        Response `payment.state` values:
          1 = Confirmed (done)
          2 = Pending
          3 = Rejected/Error
        """
        self.ensure_one()
        if not order_ref or req_lib is None:
            return {}

        terminal_id = self.provider_id.paycomet_terminal_id
        if not terminal_id:
            return {}

        endpoint = self._jetframe_operation_info_url(order_ref)
        payload = {'payment': {'terminal': int(terminal_id), 'order': order_ref}}

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
                    "Paycomet JET operationInfo: ref=%s order=%s attempt=%d/%d resp=%s",
                    self.reference, order_ref, attempt, attempts, data,
                )
                if isinstance(data, dict):
                    return data.get('payment', data)
            except Exception as exc:
                _logger.warning(
                    "Paycomet JET operationInfo error: ref=%s attempt=%d/%d: %s",
                    self.reference, attempt, attempts, exc,
                )
            if attempt < attempts:
                time.sleep(delay_ms / 1000.0)

        return {}

    # =========================================================================
    # Odoo hook: payment creation (ensures journal line is present)
    # =========================================================================

    def _create_payment(self, **extra_create_values):
        self.ensure_one()
        if self.provider_code != 'jetframe':
            return super()._create_payment(**extra_create_values)

        provider = self.provider_id

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
                "de pago entrante configurada. Ve a Contabilidad → Diarios → diario → "
                "Pagos entrantes y añade una línea para Paycomet JET."
            ))

        extra_create_values.setdefault('payment_method_line_id', pml.id)
        return super()._create_payment(**extra_create_values)

    # =========================================================================
    # Odoo hooks: notification handling
    # =========================================================================

    def _get_tx_from_notification_data(self, provider_code, notification_data):
        if provider_code != 'jetframe':
            return super()._get_tx_from_notification_data(provider_code, notification_data)

        # 1. By Odoo reference (injected into urlOk/urlKo as ?reference=…)
        reference = (notification_data.get('reference') or '').strip()
        if reference:
            tx = self.search(
                [('reference', '=', reference), ('provider_code', '=', 'jetframe')],
                limit=1,
            )
            if tx:
                return tx

        # 2. Fallback: by Paycomet order ref
        order_ref = (
            notification_data.get('order') or notification_data.get('Order') or ''
        ).strip()
        if order_ref:
            tx = self.search(
                [('paycomet_order', '=', order_ref), ('provider_code', '=', 'jetframe')],
                limit=1,
            )
            if tx:
                return tx

        raise ValidationError(
            _("Paycomet JET: transacción no encontrada. reference=%s order=%s")
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
        is_ic = self._jetframe_is_instant_credit()

        _logger.info(
            "Paycomet JET notification: ref=%s order=%s status=%s is_ic=%s tx_state=%s",
            self.reference, order_ref, status, is_ic, self.state,
        )

        # Guard: never re-process terminal states
        if self.state in ('done', 'cancel', 'error'):
            _logger.info(
                "Paycomet JET: ref=%s already in terminal state '%s' — ignored.",
                self.reference, self.state,
            )
            return

        if status == 'ok':
            self._jetframe_handle_ok(notification_data, order_ref, is_ic)
        elif status == 'ko':
            self._jetframe_handle_ko(notification_data, order_ref, is_ic)
        else:
            _logger.warning(
                "Paycomet JET: unknown status '%s' for ref=%s", status, self.reference,
            )
            self._set_error(_("Paycomet: estado desconocido recibido ('%s').") % status)

    # =========================================================================
    # OK / KO handlers
    # =========================================================================

    def _jetframe_handle_ok(self, notification_data, order_ref, is_ic):
        """
        Handle a successful return (status=ok).

        CARD:
          urlOk fires AFTER the bank has authorised the payment.
          → Set DONE immediately.
          The S2S notify reinforces this but the browser return is reliable.

        INSTANT CREDIT:
          urlOk fires when the user submits the financing application.
          It does NOT mean the credit has been approved — Sabadell processes
          the request asynchronously (seconds to hours).
          → Always set PENDING.
          → The S2S notify (Response=OK) later moves it to DONE.

          We do call operationInfo once to catch the rare case where Sabadell
          approves synchronously before our urlOk handler runs, but we default
          to PENDING when uncertain.
        """
        self.ensure_one()

        if not is_ic:
            # ── Card ──────────────────────────────────────────────────────────
            self._set_done(
                state_message=_("Pago con tarjeta autorizado y confirmado por Paycomet.")
            )
            return

        # ── Instant Credit ────────────────────────────────────────────────────
        # Query operationInfo to catch synchronous approvals.
        # Keep attempts very low — this is in the browser return request cycle.
        op = self._jetframe_get_operation_info(order_ref, attempts=2, delay_ms=600)
        op_state = _parse_error_code(op.get('state'))

        _logger.info(
            "Paycomet JET IC urlOk: ref=%s order=%s operationInfo.state=%s",
            self.reference, order_ref, op_state,
        )

        if op_state == 1:
            # Rare: Sabadell approved synchronously before our handler ran
            self._set_done(
                state_message=_(
                    "Financiación Instant Credit aprobada y confirmada por Paycomet."
                )
            )
        elif op_state == 3:
            # Sabadell rejected synchronously
            error_code = op.get('errorCode') or op.get('ErrorCode')
            user_msg, is_cancel = _resolve_error(error_code)
            final_msg = "Paycomet IC: " + (
                user_msg or _("Solicitud de financiación rechazada.")
            )
            _logger.info(
                "Paycomet JET IC: rejected synchronously ref=%s code=%s",
                self.reference, error_code,
            )
            if is_cancel:
                self._set_canceled(state_message=final_msg)
            else:
                self._set_error(final_msg)
        else:
            # state=2 (pending) or no info yet — normal async case
            self._set_pending(
                state_message=_(
                    "Solicitud de financiación Instant Credit enviada correctamente. "
                    "Recibirás una confirmación cuando Sabadell procese tu solicitud "
                    "(puede tardar unos minutos)."
                )
            )

    def _jetframe_handle_ko(self, notification_data, order_ref, is_ic):
        """
        Handle a failed or cancelled return (status=ko).

        Error codes come from Paycomet URL params (browser return) or S2S body.
        If no code is present in the notification (browser return without extra
        Paycomet params), we call operationInfo to enrich the error details.
        """
        self.ensure_one()

        error_code = (
            notification_data.get('ErrorCode')
            or notification_data.get('errorCode')
            or notification_data.get('error_code')
        )
        user_msg, is_cancel = _resolve_error(error_code)

        # If no error code came in the notification, query operationInfo
        if not error_code and order_ref:
            op = self._jetframe_get_operation_info(order_ref, attempts=2, delay_ms=400)
            op_state = _parse_error_code(op.get('state'))

            if op_state == 1:
                # Race condition: operationInfo says OK despite KO browser return
                _logger.info(
                    "Paycomet JET: operationInfo state=1 despite KO return ref=%s",
                    self.reference,
                )
                self._set_done(
                    state_message=_("Pago confirmado por operationInfo (retorno KO ignorado).")
                )
                return

            error_code = op.get('errorCode') or op.get('ErrorCode')
            user_msg, is_cancel = _resolve_error(error_code)

        if is_ic:
            # IC-specific: differentiate between user abandonment and rejection
            if is_cancel or not error_code:
                # User closed the financing form without submitting → cancel
                is_cancel = True
                user_msg = user_msg or _("Solicitud de financiación cancelada por el usuario.")
            else:
                user_msg = user_msg or _("Solicitud de financiación rechazada.")

        final_msg = "Paycomet: " + (
            user_msg
            or notification_data.get('errorDescription')
            or (_("Financiación rechazada.") if is_ic else _("Pago rechazado."))
        )

        _logger.info(
            "Paycomet JET KO: ref=%s is_ic=%s error_code=%s is_cancel=%s msg=%s",
            self.reference, is_ic, error_code, is_cancel, final_msg,
        )

        if is_cancel:
            self._set_canceled(state_message=final_msg)
        else:
            self._set_error(final_msg)
