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

        const redirectForm = this._jetframeExtractRedirectForm(processingValues);
        if (!redirectForm) {
            return this._super(...arguments);
        }

        this._jetframeOpenModal(redirectForm);
    },

    _jetframeShouldUseModal(paymentMethodCode) {
        return paymentMethodCode !== 'instant_credit';
    },

    _jetframeExtractRedirectForm(processingValues) {
        const tmp = document.createElement('div');
        tmp.innerHTML = processingValues.redirect_form_html || '';
        const form = tmp.querySelector('form');
        return form ? form.cloneNode(true) : null;
    },

    _jetframeOpenModal(redirectForm) {
        this._jetframeCleanup();
        this._jetframeDismissOdooLoader();
        this._jetframePollTimer && window.clearInterval(this._jetframePollTimer);
        this._jetframeStatusTimer && window.clearInterval(this._jetframeStatusTimer);
        this._jetframeOnMessage && window.removeEventListener('message', this._jetframeOnMessage);

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
                            name="${IFRAME_ID}"
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
        const form = redirectForm;
        const txReference = form.querySelector('input[name="reference"]')?.value || null;
        const txOrder = form.querySelector('input[name="order"]')?.value || null;
        let isClosed = false;

        form.setAttribute('target', IFRAME_ID);
        form.classList.add('d-none');
        modal.appendChild(form);

        const breakoutToTop = (url = '/payment/status') => {
            if (isClosed) {
                return;
            }
            isClosed = true;
            if (this._jetframePollTimer) {
                window.clearInterval(this._jetframePollTimer);
                this._jetframePollTimer = null;
            }
            if (this._jetframeStatusTimer) {
                window.clearInterval(this._jetframeStatusTimer);
                this._jetframeStatusTimer = null;
            }
            this._jetframeCleanup();
            window.location.href = url;
        };

        const inspectIframeLocation = () => {
            try {
                const href = iframe.contentWindow?.location?.href;
                if (!href || href === 'about:blank') {
                    return false;
                }
                const parsed = new URL(href, window.location.origin);
                if (parsed.origin !== window.location.origin) {
                    return false;
                }
                if (parsed.pathname === '/payment/jetframe/return' || parsed.pathname === '/payment/status') {
                    breakoutToTop('/payment/status');
                    return true;
                }
            } catch (_) {
                // Cross-origin while Paycomet is loaded; ignore until it returns to our domain.
            }
            return false;
        };

        const onIframeLoad = () => {
            if (inspectIframeLocation()) {
                return;
            }
            try {
                if (iframe.contentWindow?.location?.href === 'about:blank') {
                    return;
                }
            } catch (_) {
                // Cross-origin access means the remote page is already loaded.
            }
            loading.classList.add('d-none');
            iframe.classList.remove('d-none');
            iframe.removeEventListener('load', onIframeLoad);
        };
        iframe.addEventListener('load', onIframeLoad);

        iframe.addEventListener('error', () => {
            loading.innerHTML = `
                <div class="alert alert-danger mb-0" role="alert">
                    <strong>${_t('Error al cargar el formulario de pago.')}</strong><br>
                    ${_t('Por favor, recarga la página e inténtalo de nuevo.')}
                </div>`;
        }, { once: true });

        const close = () => {
            isClosed = true;
            if (this._jetframePollTimer) {
                window.clearInterval(this._jetframePollTimer);
                this._jetframePollTimer = null;
            }
            if (this._jetframeStatusTimer) {
                window.clearInterval(this._jetframeStatusTimer);
                this._jetframeStatusTimer = null;
            }
            this._jetframeCleanup();
            this._jetframeEnablePayButton();
        };

        this._jetframePollTimer = window.setInterval(() => {
            inspectIframeLocation();
        }, 400);

        this._jetframeStatusTimer = window.setInterval(async () => {
            if (isClosed || (!txReference && !txOrder)) {
                return;
            }
            try {
                const response = await fetch('/payment/jetframe/status', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        jsonrpc: '2.0',
                        method: 'call',
                        params: {reference: txReference, order: txOrder},
                        id: Date.now(),
                    }),
                    credentials: 'same-origin',
                });
                const payload = await response.json();
                const result = payload?.result;
                if (result?.found && result.state && result.state !== 'draft') {
                    breakoutToTop(result.landing_route || '/payment/status');
                }
            } catch (_) {
                // Ignore transient polling errors while the hosted flow is active.
            }
        }, 1200);

        // postMessage listener — receives the signal sent by _BREAKOUT_HTML
        // even when the iframe's inline script is restricted by CSP.
        this._jetframeOnMessage = (ev) => {
            if (!ev.data || ev.data.type !== 'paycomet_jetframe_done') {
                return;
            }
            window.removeEventListener('message', this._jetframeOnMessage);
            this._jetframeOnMessage = null;
            breakoutToTop(ev.data.dest || '/payment/status');
        };
        window.addEventListener('message', this._jetframeOnMessage);

        try {
            form.submit();
        } catch (_) {
            close();
            window.location.href = form.getAttribute('action') || window.location.href;
            return;
        }

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
        if (this._jetframeOnMessage) {
            window.removeEventListener('message', this._jetframeOnMessage);
            this._jetframeOnMessage = null;
        }
        if (this._jetframeStatusTimer) {
            window.clearInterval(this._jetframeStatusTimer);
            this._jetframeStatusTimer = null;
        }
        this._jetframeRestoreOdooLoaders?.();
    },
});
