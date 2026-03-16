{
    'name': 'Paycomet JET Payment Provider',
    'version': '2.0.0',
    'author': 'Foredu Solutions SL',
    'category': 'Accounting/Payment Providers',
    'summary': 'Paycomet Gateway (JET iFrame)',
    'depends': ['payment', 'account_payment'],
    'data': [
        'data/payment_method_data.xml',
        'views/payment_paycomet_templates.xml',
        'views/payment_provider_views.xml',
        'data/payment_provider_data.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'payment_paycomet_jetframe/static/src/js/payment_form.js',
        ],
    },
    'post_init_hook': 'post_init_hook',
    'application': False,
    'license': 'LGPL-3',
}
