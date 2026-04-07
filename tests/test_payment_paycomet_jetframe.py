# Part of Odoo. See LICENSE file for full copyright and licensing details.

from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

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
        self.assertNotIn('language', captured_payload['json'])
        self.assertNotIn('operationType', captured_payload['json'])
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

    def test_instant_credit_rejects_amount_below_sandbox_minimum(self):
        tx = self._create_transaction(flow='redirect', amount=153.67)
        tx.payment_method_id = self.credit_payment_method

        with self.assertRaises(ValidationError):
            tx._get_specific_rendering_values({'payment_method_code': 'instant_credit'})

    @mute_logger('odoo.addons.payment_paycomet_jetframe.models.payment_transaction')
    def test_instant_credit_merchant_data_uses_documented_phone_fields(self):
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
        self.assertEqual(customer.get('mobilePhone'), '+34600112233')
        self.assertNotIn('phone', customer)

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
