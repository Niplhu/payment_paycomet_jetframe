/** @odoo-module **/

import paymentForm from '@payment/js/payment_form';

paymentForm.include({
    async _prepareInlineForm(providerId, providerCode, paymentOptionId, paymentMethodCode, flow) {
        if (providerCode !== 'jetframe') {
            await this._super(...arguments);
            return;
        }

        if (flow !== 'token') {
            this._setPaymentFlow('redirect');
        }
    },

    async _processDirectFlow(providerCode, paymentOptionId, paymentMethodCode, processingValues) {
        if (providerCode !== 'jetframe') {
            await this._super(...arguments);
            return;
        }

        this._processRedirectFlow(
            providerCode,
            paymentOptionId,
            paymentMethodCode,
            processingValues,
        );
    },
});
