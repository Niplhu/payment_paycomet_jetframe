# Part of Odoo. See LICENSE file for full copyright and licensing details.

from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.payment_paycomet_jetframe.models import payment_transaction as tx_module
from odoo.addons.payment.tests.common import PaymentCommon


class _FakeResponse:

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@tagged('post_install', '-at_install')
class TestPaycometJetframe(PaymentCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls._prepare_provider('jetframe', update_values={
            'paycomet_terminal_id': '1',
            'paycomet_api_key': 'test_api_key_secret',
        })
        cls.payment_method = cls.env.ref('payment.payment_method_card')
        cls.credit_payment_method = cls.env.ref('payment_paycomet_jetframe.payment_method_instant_credit')
        cls.provider.payment_method_ids = [(6, 0, [cls.payment_method.id, cls.credit_payment_method.id])]

    def test_get_default_payment_method_codes(self):
        self.assertEqual(self.provider._get_default_payment_method_codes(), {'card', 'instant_credit'})

    @mute_logger('odoo.addons.payment_paycomet_jetframe.models.payment_transaction')
    def test_rendering_values_only_card_methods_when_card_selected(self):
        tx = self._create_transaction(flow='redirect')
        tx.payment_method_id = self.payment_method

        captured_payload = {}

        def _fake_post(*args, **kwargs):
            captured_payload['url'] = args[0] if args else None
            captured_payload['json'] = kwargs.get('json')
            return _FakeResponse({'errorCode': 0, 'challengeUrl': 'https://example.com/challenge'})

        with patch(
            'odoo.addons.payment_paycomet_jetframe.models.payment_transaction.req_lib.post',
            side_effect=_fake_post,
        ):
            tx._get_specific_rendering_values({'payment_method_code': 'card'})

        self.assertEqual(
            set(captured_payload['json']['payment']['methods']),
            {1},
            "When card is selected, /v1/form should expose only card",
        )

    @mute_logger('odoo.addons.payment_paycomet_jetframe.models.payment_transaction')
    def test_rendering_values_only_credit_methods_when_credit_selected(self):
        tx = self._create_transaction(flow='redirect')
        tx.reference = 'A123456789012'
        tx.payment_method_id = self.credit_payment_method

        captured_payload = {}

        def _fake_post(*args, **kwargs):
            captured_payload['url'] = args[0] if args else None
            captured_payload['json'] = kwargs.get('json')
            return _FakeResponse({'errorCode': 0, 'challengeUrl': 'https://example.com/challenge'})

        with patch(
            'odoo.addons.payment_paycomet_jetframe.models.payment_transaction.req_lib.post',
            side_effect=_fake_post,
        ):
            tx._get_specific_rendering_values({'payment_method_code': 'instant_credit'})

        self.assertEqual(
            captured_payload['json']['payment']['methodId'],
            33,
            "When Instant Credit is selected, payload must send methodId=33",
        )
        self.assertNotIn('methods', captured_payload['json']['payment'])
        self.assertEqual(
            captured_payload['json']['payment']['order'],
            '123456789012',
            "Instant Credit orders must remove the leading transaction letter",
        )
        self.assertEqual(captured_payload['json']['payment']['terminal'], 1)
        self.assertEqual(captured_payload['json']['payment']['productDescription'], tx.reference)
        self.assertEqual(captured_payload['url'], 'https://rest.paycomet.com/v1/payments')
        self.assertEqual(captured_payload['json']['language'], 'es')
        self.assertNotIn('operationType', captured_payload['json'])
        self.assertNotIn('urlNotification', captured_payload['json']['payment'])
        self.assertEqual(
            tx.paycomet_order,
            '123456789012',
            "Instant Credit must persist Paycomet order for return matching",
        )

        url_ok = captured_payload['json']['payment']['urlOk']
        url_ko = captured_payload['json']['payment']['urlKo']
        self.assertEqual(parse_qs(urlsplit(url_ok).query).get('order', [''])[0], '123456789012')
        self.assertEqual(parse_qs(urlsplit(url_ko).query).get('order', [''])[0], '123456789012')

    def test_rendering_values_raise_on_form_error(self):
        tx = self._create_transaction(flow='redirect')

        with patch(
            'odoo.addons.payment_paycomet_jetframe.models.payment_transaction.req_lib.post',
            return_value=_FakeResponse({'errorCode': 1145}),
        ):
            with self.assertRaises(ValidationError):
                tx._get_specific_rendering_values({})

    def test_instant_credit_requires_billing_country(self):
        tx = self._create_transaction(flow='redirect', amount=200.0)
        tx.payment_method_id = self.credit_payment_method
        tx.partner_id.country_id = False
        tx.company_id.partner_id.country_id = False

        with self.assertRaises(ValidationError):
            tx._get_specific_rendering_values({'payment_method_code': 'instant_credit'})

    @mute_logger('odoo.addons.payment_paycomet_jetframe.models.payment_transaction')
    def test_instant_credit_merchant_data_keeps_customer_block_compatible(self):
        tx = self._create_transaction(flow='redirect', amount=200.0)
        tx.payment_method_id = self.credit_payment_method
        tx.partner_id.mobile = '+34 600 11 22 33'
        tx.partner_id.phone = '+34 961 11 22 33'

        captured_payload = {}

        def _fake_post(*args, **kwargs):
            captured_payload['json'] = kwargs.get('json')
            return _FakeResponse({'errorCode': 0, 'challengeUrl': 'https://example.com/challenge'})

        with patch(
            'odoo.addons.payment_paycomet_jetframe.models.payment_transaction.req_lib.post',
            side_effect=_fake_post,
        ):
            tx._get_specific_rendering_values({'payment_method_code': 'instant_credit'})

        customer = captured_payload['json']['payment']['merchantData']['customer']
        self.assertNotIn('mobilePhone', customer)
        self.assertNotIn('homePhone', customer)

    def test_rendering_values_use_challenge_url_when_available(self):
        tx = self._create_transaction(flow='redirect')

        with patch(
            'odoo.addons.payment_paycomet_jetframe.models.payment_transaction.req_lib.post',
            return_value=_FakeResponse({'errorCode': 0, 'challengeUrl': 'https://example.com/challenge'}),
        ):
            values = tx._get_specific_rendering_values({})

        self.assertEqual(values['form_url'], 'https://example.com/challenge')

    def test_rendering_values_use_challenge_url_without_error_code(self):
        tx = self._create_transaction(flow='redirect')

        with patch(
            'odoo.addons.payment_paycomet_jetframe.models.payment_transaction.req_lib.post',
            return_value=_FakeResponse({'challengeURL': 'https://example.com/challenge'}),
        ):
            values = tx._get_specific_rendering_values({})

        self.assertEqual(values['form_url'], 'https://example.com/challenge')

    def test_get_tx_from_notification_data_requires_reference_or_order(self):
        tx = self._create_transaction(flow='redirect')
        with self.assertRaises(ValidationError):
            tx._get_tx_from_notification_data('jetframe', {'status': 'ok'})

    def test_get_tx_from_notification_data_fallbacks_to_order(self):
        tx = self._create_transaction(flow='redirect')
        tx.paycomet_order = '123456789012'
        resolved_tx = tx._get_tx_from_notification_data('jetframe', {
            'status': 'ok',
            'order': '123456789012',
        })
        self.assertEqual(resolved_tx.id, tx.id)

    @mute_logger('odoo.addons.payment_paycomet_jetframe.models.payment_transaction')
    def test_notification_ok_instant_credit_sets_pending_without_operation_info(self):
        tx = self._create_transaction(flow='redirect')
        tx.payment_method_id = self.credit_payment_method

        with patch(
            'odoo.addons.payment_paycomet_jetframe.models.payment_transaction.PaymentTransaction._jetframe_get_operation_info',
            return_value={},
        ):
            tx._process_notification_data({'status': 'ok'})

        self.assertEqual(tx.state, 'pending')

    @mute_logger('odoo.addons.payment_paycomet_jetframe.models.payment_transaction')
    def test_notification_ok_instant_credit_sets_done_with_operation_info(self):
        tx = self._create_transaction(flow='redirect')
        tx.payment_method_id = self.credit_payment_method

        with patch(
            'odoo.addons.payment_paycomet_jetframe.models.payment_transaction.PaymentTransaction._jetframe_get_operation_info',
            return_value={'state': 1},
        ):
            tx._process_notification_data({'status': 'ok'})

        self.assertEqual(tx.state, 'done')

    def test_resolve_error_uses_local_cancel_message(self):
        tx = self._create_transaction(flow='redirect')

        message, is_cancel = tx._jetframe_resolve_error(1129, 1)

        self.assertEqual(message, 'El pago fue cancelado por el usuario.')
        self.assertTrue(is_cancel)

    def test_resolve_error_uses_local_card_message(self):
        tx = self._create_transaction(flow='redirect')

        message, is_cancel = tx._jetframe_resolve_error(1005, 1)

        self.assertEqual(message, 'Fondos insuficientes.')
        self.assertFalse(is_cancel)

    def test_local_error_map_contains_supported_codes(self):
        expected_codes = {
            1129, 1115, 1003, 1004, 1005, 1006, 1010, 1015,
            1110, 1111, 1112, 1123, 1124, 1125, 1000, 1001,
            1002, 1050, 1053, 9050, 9051, 9055,
        }

        self.assertTrue(expected_codes.issubset(set(tx_module.PAYCOMET_ERROR_MESSAGES)))

    def test_resolve_error_falls_back_to_paycomet_errors_api(self):
        tx = self._create_transaction(flow='redirect')
        mocked_response = Mock()
        mocked_response.json.return_value = {'errorDescription': 'Error remoto'}

        with patch(
            'odoo.addons.payment_paycomet_jetframe.models.payment_transaction.req_lib.post',
            return_value=mocked_response,
        ):
            message, is_cancel = tx._jetframe_resolve_error(7777, 1)

        self.assertEqual(message, 'Error remoto')
        self.assertFalse(is_cancel)

    def test_notification_ko_sets_cancel_on_cancel_codes(self):
        tx = self._create_transaction(flow='redirect')
        tx.payment_method_id = self.payment_method

        with patch(
            'odoo.addons.payment_paycomet_jetframe.models.payment_transaction.PaymentTransaction._jetframe_get_operation_info',
            return_value={},
        ):
            tx._process_notification_data({'status': 'ko', 'errorCode': '1129'})

        self.assertEqual(tx.state, 'cancel')

    def test_notification_ko_sets_error_on_card_error_codes(self):
        tx = self._create_transaction(flow='redirect')
        tx.payment_method_id = self.payment_method

        with patch(
            'odoo.addons.payment_paycomet_jetframe.models.payment_transaction.PaymentTransaction._jetframe_get_operation_info',
            return_value={},
        ):
            tx._process_notification_data({'status': 'ko', 'errorCode': '1005'})

        self.assertEqual(tx.state, 'error')
        self.assertIn('Fondos insuficientes', tx.state_message)

    def test_client_ip_ignores_private_proxy_addresses(self):
        tx = self._create_transaction(flow='redirect')
        httprequest = Mock()
        httprequest.headers = {
            'X-Forwarded-For': '10.0.0.5, 192.168.1.10',
            'X-Real-IP': '127.0.0.1',
        }
        httprequest.remote_addr = '172.16.0.2'

        with patch('odoo.addons.payment_paycomet_jetframe.models.payment_transaction.request') as request_mock:
            request_mock.httprequest = httprequest
            self.assertFalse(tx._jetframe_get_client_ip())

    def test_client_ip_keeps_public_forwarded_address(self):
        tx = self._create_transaction(flow='redirect')
        httprequest = Mock()
        httprequest.headers = {
            'X-Forwarded-For': '88.12.34.56, 10.0.0.5',
        }
        httprequest.remote_addr = '172.16.0.2'

        with patch('odoo.addons.payment_paycomet_jetframe.models.payment_transaction.request') as request_mock:
            request_mock.httprequest = httprequest
            self.assertEqual(tx._jetframe_get_client_ip(), '88.12.34.56')
