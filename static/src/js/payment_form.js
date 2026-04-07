/** @odoo-module **/

/**
 * Paycomet JET Frame — Odoo 18 payment form integration.
 *
 * Card opens its challengeUrl in a Bootstrap 5 modal iframe overlay.
 * Instant Credit uses Odoo's standard top-level redirect flow because
 * instantcredit.net rejects being embedded in an iframe.
 *
 * ── CARD (methodId=1) ───────────────────────────────────────────────────────
 *   1. User clicks Pay.
 *   2. Server calls Paycomet /v1/form → gets challengeUrl.
 *   3. JS opens challengeUrl in a modal iframe.
 *   4. After card entry + 3DS, Paycomet redirects iframe to urlOk/urlKo.
 *   5. Our return controller serves breakout HTML → parent navigates to
 *      /payment/status.
 *
 * ── INSTANT CREDIT (methodId=33) ────────────────────────────────────────────
 *   Full-page redirect flow. The server patches the challengeUrl to the test
 *   endpoint automatically when the provider is not in production mode
 *   (avoids HTTP 500 from api.instantcredit.net on test tokens).
 *
 *   1. User clicks "Request financing".
 *   2. Server calls /v1/payments with methodId=33 → gets challengeUrl
 *      (test or production).
 *   3. JS submits Odoo's redirect form with target=_top.
 *   4. User fills IC form (DNI, IBAN, cuotas, firma SEPA…).
 *   5. Paycomet redirects iframe to urlOk (pending) or urlKo (rejected).
 *   6. Breakout HTML → parent navigates to /payment/status.
 */

import PaymentForm from '@payment/js/payment_form';
import { _t } from '@web/core/l10n/translation';

// DOM element IDs — used for the card modal only
const OVERLAY_ID  = 'o_jetframe_overlay';
const BACKDROP_ID = 'o_jetframe_backdrop';
const IFRAME_ID   = 'o_jetframe_iframe';
const LOADING_ID  = 'o_jetframe_loading';

PaymentForm.include({

    // =========================================================================
    // Force "redirect" flow so Odoo calls _get_specific_rendering_values on the
    // server, which calls Paycomet /v1/form and gets the challengeUrl.
    // =========================================================================

    async _prepareInlineForm(providerId, providerCode, paymentOptionId, paymentMethodCode, flow) {
        if (providerCode !== 'jetframe') {
            return this._super(...arguments);
        }
        if (flow !== 'token') {
            this._setPaymentFlow('redirect');
        }
    },

    // =========================================================================
    // Route the payment to the correct flow based on the method.
    // =========================================================================

    _processRedirectFlow(providerCode, paymentOptionId, paymentMethodCode, processingValues) {
        if (providerCode !== 'jetframe') {
            return this._super(...arguments);
        }

        // Extract the challengeUrl from the rendered redirect_form HTML.
        // Odoo puts it as the <form action="…"> attribute.
        const tmp = document.createElement('div');
        tmp.innerHTML = processingValues['redirect_form_html'] || '';
        const form = tmp.querySelector('form');
        const challengeUrl = form ? form.getAttribute('action') : null;

        if (!challengeUrl) {
            console.error('[Paycomet JET] challengeUrl missing in redirect_form_html', processingValues);
            return this._super(...arguments);   // fallback: Odoo default redirect
        }

        if (paymentMethodCode === 'instant_credit') {
            return this._super(...arguments);
        }

        this._jetframeOpenModal(challengeUrl);
    },

    // =========================================================================
    // CARD FLOW: open challengeUrl in a Bootstrap 5 modal iframe overlay
    // =========================================================================

    _jetframeOpenModal(url) {
        this._jetframeCleanup();
        this._jetframeDismissOdooLoader();

        // ── Backdrop ─────────────────────────────────────────────────────────
        const backdrop = document.createElement('div');
        backdrop.id = BACKDROP_ID;
        backdrop.className = 'modal-backdrop fade show';
        document.body.appendChild(backdrop);

        // ── Modal ─────────────────────────────────────────────────────────────
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
                            ${_t('Pago seguro — Paycomet')}
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
                            <span class="text-muted">${_t('Cargando formulario de pago…')}</span>
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

        // ── Iframe lifecycle ──────────────────────────────────────────────────
        const iframe  = modal.querySelector(`#${IFRAME_ID}`);
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

        // ── Close handlers ────────────────────────────────────────────────────
        const close = () => {
            this._jetframeCleanup();
            this._jetframeEnablePayButton();
        };

        modal.querySelector('#o_jetframe_close').addEventListener('click', close);

        modal.addEventListener('click', (e) => {
            if (e.target === modal) close();
        });

        const onKeyDown = (e) => {
            if (e.key === 'Escape') {
                close();
                document.removeEventListener('keydown', onKeyDown);
            }
        };
        document.addEventListener('keydown', onKeyDown);
    },

    // =========================================================================
    // Helpers
    // =========================================================================

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
        } catch (_) { /* ignore */ }

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
