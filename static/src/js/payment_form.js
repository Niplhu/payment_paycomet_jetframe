/** @odoo-module **/

import PaymentForm from '@payment/js/payment_form';
import { _t } from '@web/core/l10n/translation';

const OVERLAY_ID = 'o_jetframe_overlay';
const BACKDROP_ID = 'o_jetframe_backdrop';
const IFRAME_ID = 'o_jetframe_iframe';
const LOADING_ID = 'o_jetframe_loading';

PaymentForm.include({
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

    _processRedirectFlow(providerCode, paymentOptionId, paymentMethodCode, processingValues) {
        if (providerCode !== 'jetframe') {
            return this._super(...arguments);
        }

        if (!this._jetframeShouldUseModal(paymentMethodCode)) {
            return this._super(...arguments);
        }

        const challengeUrl = this._jetframeExtractChallengeUrl(processingValues);
        if (!challengeUrl) {
            return this._super(...arguments);
        }

        this._jetframeOpenModal(challengeUrl);
    },

    _jetframeShouldUseModal(paymentMethodCode) {
        return paymentMethodCode !== 'instant_credit';
    },

    _jetframeExtractChallengeUrl(processingValues) {
        const tmp = document.createElement('div');
        tmp.innerHTML = processingValues.redirect_form_html || '';
        return tmp.querySelector('form')?.getAttribute('action') || null;
    },

    _jetframeOpenModal(url) {
        this._jetframeCleanup();
        this._jetframeDismissOdooLoader();

        const backdrop = document.createElement('div');
        backdrop.id = BACKDROP_ID;
        backdrop.className = 'modal-backdrop fade show';
        document.body.appendChild(backdrop);

        const modal = document.createElement('div');
        modal.id = OVERLAY_ID;
        modal.className = 'modal fade show';
        modal.style.display = 'block';
        modal.setAttribute('role', 'dialog');
        modal.setAttribute('aria-modal', 'true');
        modal.setAttribute('aria-labelledby', 'o_jetframe_title');
        modal.setAttribute('tabindex', '-1');

        modal.innerHTML = `
            <div class="modal-dialog modal-dialog-centered o_jetframe_dialog">
                <div class="modal-content">
                    <div class="modal-header o_jetframe_header">
                        <h5 class="modal-title" id="o_jetframe_title">
                            <svg xmlns="http://www.w3.org/2000/svg"
                                 width="15" height="15" viewBox="0 0 24 24"
                                 fill="none" stroke="currentColor"
                                 stroke-width="2.5" stroke-linecap="round"
                                 stroke-linejoin="round"
                                 class="o_jetframe_lock_icon me-2"
                                 aria-hidden="true">
                                <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
                                <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
                            </svg>
                            ${_t('Pago seguro - Paycomet')}
                        </h5>
                        <button type="button"
                                class="btn-close"
                                id="o_jetframe_close"
                                aria-label="${_t('Cerrar')}">
                        </button>
                    </div>

                    <div class="modal-body p-0 o_jetframe_body">
                        <div id="${LOADING_ID}" class="o_jetframe_loading">
                            <div class="o_jetframe_spinner"></div>
                            <span class="text-muted">${_t('Cargando formulario de pago...')}</span>
                        </div>
                        <iframe
                            id="${IFRAME_ID}"
                            src="${url}"
                            title="${_t('Formulario de pago seguro de Paycomet')}"
                            allow="payment"
                            class="o_jetframe_iframe d-none"
                            scrolling="yes"
                        ></iframe>
                    </div>
                </div>
            </div>
        `;

        document.body.appendChild(modal);
        document.body.classList.add('modal-open');
        modal.focus();

        const iframe = modal.querySelector(`#${IFRAME_ID}`);
        const loading = modal.querySelector(`#${LOADING_ID}`);

        iframe.addEventListener('load', () => {
            loading.classList.add('d-none');
            iframe.classList.remove('d-none');
        }, { once: true });

        iframe.addEventListener('error', () => {
            loading.innerHTML = `
                <div class="alert alert-danger mb-0" role="alert">
                    <strong>${_t('Error al cargar el formulario de pago.')}</strong><br>
                    ${_t('Por favor, recarga la página e inténtalo de nuevo.')}
                </div>`;
        }, { once: true });

        const close = () => {
            this._jetframeCleanup();
            this._jetframeEnablePayButton();
        };

        modal.querySelector('#o_jetframe_close').addEventListener('click', close);
        modal.addEventListener('click', (ev) => {
            if (ev.target === modal) {
                close();
            }
        });

        const onKeyDown = (ev) => {
            if (ev.key === 'Escape') {
                close();
                document.removeEventListener('keydown', onKeyDown);
            }
        };
        document.addEventListener('keydown', onKeyDown);
    },

    _jetframeEnablePayButton() {
        if (typeof this._enableButton === 'function') {
            this._enableButton();
            return;
        }
        const btn = this.el && (
            this.el.querySelector('button[name="o_payment_submit_button"]') ||
            this.el.querySelector('.o_payment_submit_button') ||
            this.el.querySelector('button[type="submit"]')
        );
        if (btn) {
            btn.removeAttribute('disabled');
            btn.classList.remove('disabled');
        }
    },

    _jetframeDismissOdooLoader() {
        try {
            if (typeof window.$ !== 'undefined' && typeof window.$.unblockUI === 'function') {
                window.$.unblockUI();
            }
        } catch (_) {
            // ignore
        }

        for (const sel of ['.o_loading', '.o_blockUI', '.o_loader', '#o_loading']) {
            document.querySelectorAll(sel).forEach((el) => {
                el.dataset.jetframeHidden = '1';
                el.style.display = 'none';
            });
        }
    },

    _jetframeRestoreOdooLoaders() {
        document.querySelectorAll('[data-jetframe-hidden="1"]').forEach((el) => {
            el.style.removeProperty('display');
            delete el.dataset.jetframeHidden;
        });
    },

    _jetframeCleanup() {
        document.getElementById(OVERLAY_ID)?.remove();
        document.getElementById(BACKDROP_ID)?.remove();
        document.body.classList.remove('modal-open');
        this._jetframeRestoreOdooLoaders?.();
    },
});
