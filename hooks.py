import logging
from odoo import SUPERUSER_ID, api
_logger = logging.getLogger(__name__)
def post_init_hook(env_or_cr, registry=None):
    if registry is None:
        env = env_or_cr
    else:
        env = api.Environment(env_or_cr, SUPERUSER_ID, {})
    provider_model = env['payment.provider']
    try:
        provider = env.ref('payment_paycomet_jetframe.payment_provider_jetframe', raise_if_not_found=False)
        if not provider:
            provider = provider_model.search([('code', '=', 'jetframe')], limit=1)
        if not provider:
            provider = provider_model.search([('name', '=', 'Paycomet JET')], limit=1)
        available_codes = dict(provider_model._fields['code']._description_selection(env))
        if not provider:
            provider = provider_model.create({
                'name': 'Paycomet JET',
                'code': 'jetframe' if 'jetframe' in available_codes else 'none',
                'state': 'disabled',
                'is_published': False,
            })
        vals = {'name': 'Paycomet JET'}
        if 'jetframe' in available_codes:
            vals['code'] = 'jetframe'
        module = env.ref('base.module_payment_paycomet_jetframe', raise_if_not_found=False)
        if module:
            vals['module_id'] = module.id
        redirect_view = env.ref('payment_paycomet_jetframe.redirect_form', raise_if_not_found=False)
        inline_view = env.ref('payment_paycomet_jetframe.inline_form', raise_if_not_found=False)
        if redirect_view:
            vals['redirect_form_view_id'] = redirect_view.id
        if inline_view:
            vals['inline_form_view_id'] = inline_view.id
        provider.write(vals)
        card_method = env.ref('payment.payment_method_card', raise_if_not_found=False)
        instant_method = env.ref(
            'payment_paycomet_jetframe.payment_method_instant_credit',
            raise_if_not_found=False,
        )
        method_ids = [m.id for m in (card_method, instant_method) if m]
        if method_ids:
            provider.payment_method_ids = [(6, 0, method_ids)]
    except Exception:
        _logger.exception("Paycomet JET: post_init_hook failed while creating provider")
