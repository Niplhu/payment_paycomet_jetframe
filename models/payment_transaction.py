import ipaddress
import logging
import re
import time
from urllib.parse import urlencode, urlsplit

try:
    import requests as req_lib
except Exception:  # pragma: no cover - optional dependency at import time
    req_lib = None

from odoo import _, fields, models
from odoo.exceptions import RedirectWarning, ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)

PAYCOMET_FORM_URL = "https://rest.paycomet.com/v1/form"
PAYCOMET_PAYMENTS_URL = "https://rest.paycomet.com/v1/payments"
PAYCOMET_ERRORS_URL = "https://rest.paycomet.com/v1/errors"
PAYCOMET_OPERATION_INFO_URL = "https://rest.paycomet.com/v1/payments/{order}/info"
PAYCOMET_METHOD_CARD = 1
PAYCOMET_METHOD_INSTANT_CREDIT = 33
PAYCOMET_ERROR_MESSAGES = {
    1129: ("El pago fue cancelado por el usuario.", True),
    1115: ("La operacion fue cancelada.", True),
    1003: ("Datos de tarjeta incorrectos. Comprueba el numero, la fecha y el CVV.", False),
    1004: ("Tarjeta no valida o caducada.", False),
    1005: ("Fondos insuficientes.", False),
    1006: ("Tarjeta bloqueada o restringida.", False),
    1010: ("Operacion denegada por el emisor de la tarjeta.", False),
    1015: ("Numero de intentos excedido. Intentelo mas tarde.", False),
    1110: ("El importe minimo para esta operacion no ha sido alcanzado.", False),
    1111: ("El importe supera el maximo permitido.", False),
    1112: ("Importe no valido.", False),
    1123: ("Autenticacion 3D Secure fallida.", False),
    1124: ("Tiempo de espera agotado durante la autenticacion.", False),
    1125: ("El banco emisor no soporta 3D Secure.", False),
    1000: ("Error interno de Paycomet. Intentelo de nuevo.", False),
    1001: ("Terminal no encontrado o inactivo.", False),
    1002: ("Credenciales de terminal incorrectas.", False),
    1050: ("Pedido duplicado. Ya existe una transaccion con esta referencia.", False),
    1053: ("La orden ya fue procesada.", False),
    9050: ("Solicitud de credito rechazada por el proveedor financiero.", False),
    9051: ("Documentacion requerida para el credito no disponible.", False),
    9055: ("Limite de credito superado.", False),
}
ISO_3166_NUMERIC_BY_ALPHA2 = {
    'DE': '276',
    'ES': '724',
    'FR': '250',
    'GB': '826',
    'IE': '372',
    'IT': '380',
    'NL': '528',
    'PT': '620',
    'US': '840',
}


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    paycomet_order = fields.Char(
        string="Paycomet Order",
        readonly=True,
        copy=False,
    )

    # ---------------------------------------------------------
    # Redirect flow values (Odoo checkout -> redirect_form)
    # ---------------------------------------------------------

    def _get_specific_rendering_values(self, processing_values):
        values = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'jetframe':
            return values

        self.ensure_one()
        form_url = self._jetframe_get_form_challenge_url(processing_values=processing_values)
        if not form_url:
            raise ValidationError(_("Paycomet no devolvio una URL de formulario valida."))

        values.update({
            'form_url': form_url,
        })
        return values

    # ---------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------

    def _jetframe_get_public_base_url(self):
        self.ensure_one()
        base_url = (self.provider_id.get_base_url() or '').strip().rstrip('/')
        if not base_url:
            raise ValidationError(_("Configura la URL base pública de Odoo."))

        parsed = urlsplit(base_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValidationError(_("La URL base pública de Odoo no es válida."))

        if parsed.scheme != 'https':
            raise ValidationError(_(
                "La URL base publica de Odoo debe usar HTTPS para Paycomet JET. "
                "Actualiza web.base.url a una URL https:// valida."
            ))
        return base_url.rstrip('/')

    def _jetframe_build_order(self, processing_values=None):
        self.ensure_one()
        ref = re.sub(r'[^A-Za-z0-9]', '', (self.reference or '').upper())
        payment_method_code = self._jetframe_get_selected_payment_method_code(
            processing_values=processing_values
        )
        if payment_method_code in ('instant_credit', 'credit') and ref and ref[0].isalpha():
            ref = ref[1:]
        if ref:
            return ref[:12]
        if self.id:
            return f"TX{self.id}"[:12]
        return 'TX0000000001'

    def _jetframe_get_country_numeric_code(self, country):
        if not country:
            return None
        code = (country.code or '').strip().upper()
        if not code:
            return None
        if code.isdigit() and len(code) == 3:
            return code
        return ISO_3166_NUMERIC_BY_ALPHA2.get(code)

    def _jetframe_get_merchant_data(self, processing_values=None):
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id
        company_partner = self.company_id.partner_id.commercial_partner_id
        partner_name = (partner.name or '').strip()
        name_parts = [part for part in partner_name.split() if part]
        first_name = name_parts[0] if name_parts else ''
        surname = ' '.join(name_parts[1:]) if len(name_parts) > 1 else (partner_name or '')
        customer = {
            'id': str(partner.id),
            'name': first_name[:254],
            'surname': surname[:254],
        }
        email = (partner.email or '').strip()
        if email:
            customer['email'] = email

        billing_partner = partner if partner.exists() else company_partner
        partner_country_numeric = self._jetframe_get_country_numeric_code(billing_partner.country_id)
        company_country_numeric = self._jetframe_get_country_numeric_code(company_partner.country_id)

        is_instant_credit = self._jetframe_is_instant_credit(processing_values=processing_values)
        country_numeric = partner_country_numeric or company_country_numeric
        if is_instant_credit and company_country_numeric:
            country_numeric = company_country_numeric
        billing = {}
        if country_numeric:
            billing['billAddrCountry'] = country_numeric
        if billing_partner.city:
            billing['billAddrCity'] = billing_partner.city[:50]
        if billing_partner.street:
            billing['billAddrLine1'] = billing_partner.street[:50]
        if billing_partner.street2:
            billing['billAddrLine2'] = billing_partner.street2[:50]
        if billing_partner.zip:
            billing['billAddrPostCode'] = billing_partner.zip[:16]

        if is_instant_credit and not billing.get('billAddrCountry'):
            raise ValidationError(_(
                "Para Instant Credit debes configurar un pais de facturacion en el cliente o en la compania."
            ))

        if is_instant_credit and billing.get('billAddrCountry') != '724':
            _logger.warning(
                "Paycomet JET: Instant Credit se esta enviando con billAddrCountry=%s (recomendado 724/ES).",
                billing.get('billAddrCountry'),
            )

        merchant_data = {'customer': customer}
        if billing:
            merchant_data['billing'] = billing
        return merchant_data

    def _jetframe_get_client_ip(self):
        self.ensure_one()
        if not (request and request.httprequest):
            return None

        http_request = request.httprequest
        candidates = []

        xff = (http_request.headers.get('X-Forwarded-For') or '').strip()
        if xff:
            candidates.extend(part.strip() for part in xff.split(',') if part.strip())
        x_real_ip = (http_request.headers.get('X-Real-IP') or '').strip()
        if x_real_ip:
            candidates.append(x_real_ip)
        if http_request.remote_addr:
            candidates.append(http_request.remote_addr.strip())

        for candidate in candidates:
            try:
                ipaddress.ip_address(candidate)
                return candidate
            except ValueError:
                continue
        return None

    def _jetframe_api_headers(self):
        self.ensure_one()
        if req_lib is None:
            raise ValidationError(_("Falta la dependencia Python 'requests' en el servidor."))
        return {
            'PAYCOMET-API-TOKEN': self.provider_id.paycomet_api_key,
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

    def _jetframe_extract_challenge_url(self, payload):
        if not isinstance(payload, dict):
            return None

        candidates = [payload]
        for nested_key in ('payment', 'data', 'result'):
            nested_payload = payload.get(nested_key)
            if isinstance(nested_payload, dict):
                candidates.append(nested_payload)

        for candidate in candidates:
            for key in ('challengeUrl', 'challengeURL', 'challenge_url'):
                challenge_url = candidate.get(key)
                if isinstance(challenge_url, str) and challenge_url.strip():
                    parsed = urlsplit(challenge_url.strip())
                    if parsed.scheme in ('http', 'https') and parsed.netloc:
                        return challenge_url.strip()
        return None

    def _jetframe_get_selected_payment_method_code(self, processing_values=None):
        self.ensure_one()
        payment_method_code = None
        if processing_values:
            payment_method_code = processing_values.get('payment_method_code')
            if not payment_method_code and processing_values.get('payment_method_id'):
                payment_method = self.env['payment.method'].browse(processing_values['payment_method_id'])
                payment_method_code = payment_method.code if payment_method.exists() else None
        if not payment_method_code and self.payment_method_id:
            payment_method_code = self.payment_method_id.code
        if not payment_method_code:
            payment_method_code = self.payment_method_code
        return (payment_method_code or '').strip().lower()

    def _jetframe_is_instant_credit(self, processing_values=None):
        payment_method_code = self._jetframe_get_selected_payment_method_code(
            processing_values=processing_values,
        )
        return payment_method_code in ('credit', 'instant_credit')

    def _jetframe_get_form_methods(self, processing_values=None):
        payment_method_code = self._jetframe_get_selected_payment_method_code(processing_values=processing_values)
        _logger.info(
            "Paycomet JET: selected payment method ref=%s code=%s tx_pm=%s",
            self.reference,
            payment_method_code,
            self.payment_method_id.code if self.payment_method_id else None,
        )
        if payment_method_code in ('credit', 'instant_credit'):
            return [PAYCOMET_METHOD_INSTANT_CREDIT]
        if payment_method_code == 'card':
            return [PAYCOMET_METHOD_CARD]
        # Safe fallback for unexpected cases.
        return [PAYCOMET_METHOD_CARD]

    def _jetframe_describe_error(self, error_code, terminal_id):
        self.ensure_one()
        if not error_code:
            return None

        payload = {
            'terminal': terminal_id,
            'errorCode': int(error_code),
            'lang': 'es',
        }
        try:
            response = req_lib.post(
                PAYCOMET_ERRORS_URL,
                json=payload,
                headers=self._jetframe_api_headers(),
                timeout=20,
            )
            data = response.json()
        except Exception:
            return None
        return data.get('errorDescription')

    def _jetframe_resolve_error(self, error_code, terminal_id=None):
        self.ensure_one()
        try:
            code = int(error_code)
        except (TypeError, ValueError):
            return None, False

        message, is_cancel = PAYCOMET_ERROR_MESSAGES.get(code, (None, False))
        if message:
            return message, is_cancel

        return self._jetframe_describe_error(code, terminal_id), False

    def _jetframe_get_operation_info(self, order_ref, terminal_id, attempts=3, delay_seconds=0.8):
        self.ensure_one()
        if not order_ref or not terminal_id:
            return {}

        payload = {
            'payment': {
                'terminal': int(terminal_id),
                'order': order_ref,
            }
        }
        endpoint = PAYCOMET_OPERATION_INFO_URL.format(order=order_ref)
        attempts = max(1, int(attempts or 1))
        for attempt in range(1, attempts + 1):
            try:
                response = req_lib.post(
                    endpoint,
                    json=payload,
                    headers=self._jetframe_api_headers(),
                    timeout=20,
                )
            except Exception as exc:
                _logger.warning(
                    "Paycomet JET: error llamando operationInfo ref=%s order=%s intento=%s/%s error=%s",
                    self.reference,
                    order_ref,
                    attempt,
                    attempts,
                    exc,
                )
                if attempt < attempts:
                    time.sleep(delay_seconds)
                continue

            try:
                data = response.json()
            except Exception:
                _logger.warning(
                    "Paycomet JET: operationInfo no JSON ref=%s order=%s http=%s body=%s",
                    self.reference,
                    order_ref,
                    response.status_code,
                    response.text,
                )
                if attempt < attempts:
                    time.sleep(delay_seconds)
                continue

            _logger.info(
                "Paycomet JET: operationInfo ref=%s order=%s intento=%s/%s http=%s body=%s",
                self.reference,
                order_ref,
                attempt,
                attempts,
                response.status_code,
                data,
            )

            if isinstance(data, dict):
                payment_data = data.get('payment')
                if isinstance(payment_data, dict):
                    return payment_data
                if any(key in data for key in ('state', 'errorCode', 'response', 'stateName')):
                    return data

            if attempt < attempts:
                time.sleep(delay_seconds)

        return {}

    # ---------------------------------------------------------
    # /v1/form (required)
    # ---------------------------------------------------------

    def _jetframe_get_form_challenge_url(self, processing_values=None):
        self.ensure_one()
        provider = self.provider_id

        if not provider.paycomet_api_key:
            raise ValidationError(_("Configura la API Key de Paycomet."))

        try:
            terminal_id = int(provider.paycomet_terminal_id)
        except (TypeError, ValueError):
            raise ValidationError(_("El Terminal ID debe ser numérico."))

        if self.amount <= 0:
            raise ValidationError(_("El importe debe ser mayor que cero."))

        base_url = self._jetframe_get_public_base_url()
        order_ref = self._jetframe_build_order(processing_values=processing_values)
        self.paycomet_order = order_ref
        amount_cents = str(int(round(self.amount * 100)))
        client_ip = self._jetframe_get_client_ip()

        url_ok = f"{base_url}/payment/jetframe/return?{urlencode({'reference': self.reference, 'order': order_ref, 'status': 'ok'})}"
        url_ko = f"{base_url}/payment/jetframe/return?{urlencode({'reference': self.reference, 'order': order_ref, 'status': 'ko'})}"

        methods = self._jetframe_get_form_methods(processing_values=processing_values)
        payment_payload = {
            'terminal': terminal_id,
            'order': order_ref,
            'amount': amount_cents,
            'currency': self.currency_id.name,
            'secure': 1,
            'userInteraction': 1,
            'urlOk': url_ok,
            'urlKo': url_ko,
            'productDescription': self.reference,
            'merchantData': self._jetframe_get_merchant_data(processing_values=processing_values),
        }

        is_instant_credit = self._jetframe_is_instant_credit(processing_values=processing_values)
        if is_instant_credit:
            payment_payload['methodId'] = PAYCOMET_METHOD_INSTANT_CREDIT
            endpoint = PAYCOMET_PAYMENTS_URL
            payload = {
                'language': 'es',
                'payment': payment_payload,
            }
        else:
            payment_payload.update({
                'methods': methods,
                'excludedMethods': [],
            })
            endpoint = PAYCOMET_FORM_URL
            payload = {
                'operationType': 1,
                'language': 'es',
                'payment': payment_payload,
            }

        if client_ip:
            payload['payment']['originalIp'] = client_ip

        _logger.info(
            "Paycomet JET: payload endpoint=%s ref=%s order=%s amount=%s method=%s methods=%s",
            endpoint,
            self.reference,
            order_ref,
            amount_cents,
            payload['payment'].get('methodId'),
            payload['payment'].get('methods'),
        )
        _logger.info(
            "Paycomet JET: payload fields terminal=%s methodId=%s order=%s productDescription=%s language=%s billAddrCountry=%s",
            payload['payment'].get('terminal'),
            payload['payment'].get('methodId'),
            payload['payment'].get('order'),
            payload['payment'].get('productDescription'),
            payload.get('language'),
            ((payload['payment'].get('merchantData') or {}).get('billing') or {}).get('billAddrCountry'),
        )

        try:
            response = req_lib.post(
                endpoint,
                json=payload,
                headers=self._jetframe_api_headers(),
                timeout=30,
            )
            data = response.json()
        except Exception as exc:
            _logger.warning("Paycomet JET: error llamando %s ref=%s error=%s", endpoint, self.reference, exc)
            raise ValidationError(_("Error de comunicacion con Paycomet al obtener el formulario de pago."))

        if not isinstance(data, dict):
            _logger.warning("Paycomet JET: respuesta no JSON endpoint=%s ref=%s body=%s", endpoint, self.reference, data)
            raise ValidationError(_("Paycomet devolvio una respuesta no valida al crear el formulario de pago."))

        has_error_code = 'errorCode' in data
        error_code = data.get('errorCode', -1)
        try:
            error_code = int(error_code)
        except (TypeError, ValueError):
            error_code = -1

        challenge_url = self._jetframe_extract_challenge_url(data)
        if challenge_url and (error_code == 0 or not has_error_code):
            return challenge_url

        _logger.warning("Paycomet JET: respuesta no usable endpoint=%s ref=%s body=%s", endpoint, self.reference, data)
        error_msg, is_cancel = self._jetframe_resolve_error(
            data.get('errorCode'), terminal_id,
        )
        error_msg = data.get('errorDescription') or error_msg
        if error_msg:
            if is_cancel:
                raise ValidationError(error_msg)
            raise ValidationError(_("Paycomet: %s") % error_msg)
        raise ValidationError(_("Paycomet no devolvio una URL valida para el formulario de pago."))

    def _create_payment(self, **extra_create_values):
        self.ensure_one()
        if self.provider_code != 'jetframe':
            return super()._create_payment(**extra_create_values)

        provider = self.provider_id
        if not provider.journal_id:
            journal = self.env['account.journal'].search([
                ('company_id', '=', provider.company_id.id),
                ('type', '=', 'bank'),
            ], limit=1)
            if journal:
                provider.journal_id = journal

        provider._ensure_payment_method_line()
        payment_method_line = provider.journal_id.inbound_payment_method_line_ids.filtered(
            lambda line: line.payment_provider_id == provider
        )[:1]
        if not payment_method_line:
            payment_method_line = provider.journal_id.inbound_payment_method_line_ids.filtered(
                lambda line: line.code == provider.code
            )[:1]
        if not payment_method_line:
            payment_method_line = provider.journal_id.inbound_payment_method_line_ids[:1]
        if not payment_method_line:
            raise ValidationError(_(
                "Configura un diario bancario y una linea de metodo de pago para Paycomet JET."
            ))

        extra_create_values.setdefault('payment_method_line_id', payment_method_line.id)
        return super()._create_payment(**extra_create_values)

    def _post_process(self):
        non_jetframe_txs = self.filtered(lambda tx: tx.provider_code != 'jetframe')
        if non_jetframe_txs:
            super(PaymentTransaction, non_jetframe_txs)._post_process()

        for tx in self.filtered(lambda tx: tx.provider_code == 'jetframe'):
            try:
                super(PaymentTransaction, tx)._post_process()
            except RedirectWarning as warning:
                _logger.exception(
                    "Paycomet JET: post-processing blocked by configuration warning for tx %s: %s",
                    tx.reference,
                    warning,
                )
                tx._set_error(_(
                    "El pago se autorizo, pero no se pudo completar la contabilizacion. "
                    "Revisa la configuracion contable del diario y vuelve a procesar la transaccion."
                ))
                tx.is_post_processed = True

    # ---------------------------------------------------------
    # Return URL notification handling
    # ---------------------------------------------------------

    def _get_tx_from_notification_data(self, provider_code, notification_data):
        if provider_code != 'jetframe':
            return super()._get_tx_from_notification_data(provider_code, notification_data)

        reference = notification_data.get('reference')
        tx = self.env['payment.transaction']
        if reference:
            tx = self.search([
                ('reference', '=', reference),
                ('provider_code', '=', 'jetframe'),
            ], limit=1)

        if not tx:
            order_ref = (notification_data.get('order') or '').strip()
            if order_ref:
                tx = self.search([
                    ('paycomet_order', '=', order_ref),
                    ('provider_code', '=', 'jetframe'),
                ], limit=1)

        if not tx:
            if reference:
                raise ValidationError(_("Paycomet: no se encontro la transaccion %s") % reference)
            raise ValidationError(_("Paycomet: faltan identificadores de retorno (reference/order)."))
        return tx

    def _process_notification_data(self, notification_data):
        if self.provider_code != 'jetframe':
            return super()._process_notification_data(notification_data)

        status = notification_data.get('status')
        is_instant_credit = self._jetframe_is_instant_credit()
        _logger.info(
            "Paycomet notification: reference=%s status=%s tx_state=%s full_data=%s",
            self.reference,
            status,
            self.state,
            notification_data,
        )

        if status == 'ok' and self.state != 'done':
            if is_instant_credit:
                provider = self.provider_id
                order_ref = (notification_data.get('order') or self.paycomet_order or self._jetframe_build_order())
                operation_info = self._jetframe_get_operation_info(
                    order_ref,
                    provider.paycomet_terminal_id,
                    attempts=4,
                    delay_seconds=0.8,
                )
                if operation_info:
                    state_value = operation_info.get('state')
                    try:
                        state_value = int(state_value)
                    except (TypeError, ValueError):
                        pass

                    if state_value == 1 and self.state != 'done':
                        self._set_done(state_message=_("Pago confirmado tras verificacion de estado en Paycomet."))
                        return
                    if state_value == 2 and self.state not in ('done', 'error'):
                        self._set_pending(state_message=_("Pago en estado pendiente segun Paycomet."))
                        return

                if self.state not in ('done', 'error'):
                    self._set_pending(state_message=_(
                        "Retorno de Instant Credit recibido. Pendiente de confirmacion final en Paycomet."
                    ))
            else:
                self._set_done(state_message=_("Pago confirmado tras autenticacion 3D Secure."))
        elif status == 'ko' and self.state not in ('done', 'error'):
            error_description = (
                notification_data.get('errorDescription')
                or notification_data.get('error_description')
                or notification_data.get('message')
                or notification_data.get('error')
            )
            error_code = notification_data.get('errorCode') or notification_data.get('error_code')

            provider = self.provider_id
            order_ref = (notification_data.get('order') or self.paycomet_order or self._jetframe_build_order())
            operation_info = self._jetframe_get_operation_info(
                order_ref,
                provider.paycomet_terminal_id,
                attempts=4,
                delay_seconds=0.8,
            )
            if operation_info:
                state_value = operation_info.get('state')
                try:
                    state_value = int(state_value)
                except (TypeError, ValueError):
                    pass

                if state_value == 1 and self.state != 'done':
                    self._set_done(state_message=_("Pago confirmado tras verificación de estado en Paycomet."))
                    return
                if state_value == 2 and self.state not in ('done', 'error'):
                    self._set_pending(state_message=_("Pago en estado pendiente según Paycomet."))
                    return

                error_description = (
                    error_description
                    or operation_info.get('errorDescription')
                    or operation_info.get('stateName')
                    or operation_info.get('response')
                )
                error_code = error_code or operation_info.get('errorCode')
            else:
                _logger.warning(
                    "Paycomet JET: retorno KO sin detalle de operationInfo ref=%s order=%s",
                    self.reference,
                    order_ref,
                )

            is_cancel = False
            if error_code:
                resolved_error, is_cancel = self._jetframe_resolve_error(
                    error_code, provider.paycomet_terminal_id,
                )
                error_description = error_description or resolved_error

            if error_description:
                if is_cancel:
                    self._set_canceled(state_message=error_description)
                else:
                    self._set_error(_("Paycomet: %s") % error_description)
            elif error_code:
                self._set_error(_("Paycomet rechazó el pago (código %s).") % error_code)
            else:
                self._set_error(_("Pago rechazado por Paycomet."))
