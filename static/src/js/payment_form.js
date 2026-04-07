/** @odoo-module **/

/**
 * Paycomet JET Frame — Odoo 18 payment form integration.
 *
 * Flow:
 * 1. User selects Paycomet and clicks Pay.
 * 2. We force Odoo to use the "redirect" flow so _get_specific_rendering_values
 *    is called server-side, which calls Paycomet /v1/form and gets a challengeUrl.
 * 3. Odoo renders the redirect_form template with that URL as the form's action.
 * 4. We intercept the redirect, extract the URL, and open it in a Bootstrap 5
 *    modal dialog (same style as native Odoo dialogs) — no full-page navigation.
 * 5. Paycomet shows the card form inside the modal iframe.
 * 6. After payment, Paycomet redirects to urlOk/urlKo inside the iframe.
 * 7. Our return controller serves a breakout page that navigates the parent
 *    window to /payment/status.
 */

import PaymentForm from '@payment/js/payment_form';
import { _t } from '@web/core/l10n/translation';

// IDs used to find / clean up DOM elements
const OVERLAY_ID   = 'o_jetframe_overlay';
const BACKDROP_ID  = 'o_jetframe_backdrop';
const IFRAME_ID    = 'o_jetframe_iframe';
const LOADING_ID   = 'o_jetframe_loading';

PaymentForm.include({

    // -------------------------------------------------------------------------
    // Force "redirect" flow so Odoo calls _get_specific_rendering_values on the
    // server (where we call Paycomet /v1/form to get the challenge URL).
    // -------------------------------------------------------------------------

    async _prepareInlineForm(providerId, providerCode, paymentOptionId, paymentMethodCode, flow) {
        if (providerCode !== 'jetframe') {
            return this._super(...arguments);
        }
        if (flow !== 'token') {
            this._setPaymentFlow('redirect');
        }
    },

    // -------------------------------------------------------------------------
    // Intercept the Odoo redirect flow and open the challenge URL in a modal.
    // -------------------------------------------------------------------------

    _processRedirectFlow(providerCode, paymentOptionId, paymentMethodCode, processingValues) {
        if (providerCode !== 'jetframe') {
            return this._super(...arguments);
        }

        // Extract the challengeUrl from the rendered redirect_form HTML.
        // Odoo puts it as the <form action="..."> attribute.
        const tmp = document.createElement('div');
        tmp.innerHTML = processingValues['redirect_form_html'] || '';
        const form = tmp.querySelector('form');
        const challengeUrl = form ? form.getAttribute('action') : null;

        if (!challengeUrl) {
            console.error('[Paycomet JET] challengeUrl missing in redirect_form_html', processingValues);
            return this._super(...arguments);   // fallback: full-page redirect
        }

        this._jetframeOpenModal(challengeUrl);
    },

    // -------------------------------------------------------------------------
    // Open a Bootstrap 5 modal (identical to Odoo native dialogs) with the
    // Paycomet hosted form inside an iframe.
    // -------------------------------------------------------------------------

    _jetframeOpenModal(url) {
        // Remove any leftover modal/backdrop from a previous attempt
        this._jetframeCleanup();

        // ── Dismiss Odoo's own loading overlay ────────────────────────────────
        // Odoo blocks the UI while the server call is in progress (blockUI /
        // o_loading). By the time _processRedirectFlow fires the server call is
        // done, but the overlay may still be visible. Remove it so our modal
        // is not obscured.
        this._jetframeDismissOdooLoader();

        // ── Backdrop ─────────────────────────────────────────────────────────
        // Bootstrap 5 renders a separate .modal-backdrop element.
        // We create it manually since we're not using Bootstrap's JS Modal class.
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

        // Prevent page scroll while modal is open (Bootstrap convention)
        document.body.classList.add('modal-open');

        // Focus the modal for keyboard accessibility
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

        // Click on backdrop (the semi-transparent area outside the dialog) closes
        modal.addEventListener('click', (e) => {
            if (e.target === modal) {
                close();
            }
        });

        // Escape key
        const onKeyDown = (e) => {
            if (e.key === 'Escape') {
                close();
                document.removeEventListener('keydown', onKeyDown);
            }
        };
        document.addEventListener('keydown', onKeyDown);
    },

    // -------------------------------------------------------------------------
    // Re-enable the Pay button (compatible with Odoo 18 payment form API)
    // -------------------------------------------------------------------------

    _jetframeEnablePayButton() {
        if (typeof this._enableButton === 'function') {
            this._enableButton();
            return;
        }
        // Direct DOM fallback if method was renamed in this Odoo build
        const btn = this.el && (
            this.el.querySelector('button[name="o_payment_submit_button"]')
            || this.el.querySelector('.o_payment_submit_button')
            || this.el.querySelector('button[type="submit"]')
        );
        if (btn) {
            btn.removeAttribute('disabled');
            btn.classList.remove('disabled');
        }
    },

    // -------------------------------------------------------------------------
    // Dismiss Odoo's UI-blocking loading overlay.
    //
    // Odoo 18 uses several loading mechanisms depending on the context:
    //   1. jQuery.blockUI — legacy widget layer ($.unblockUI)
    //   2. .o_loading / .o_blockUI DOM elements — website/portal layer
    //   3. .o_loader — some Odoo enterprise widgets
    //
    // We try all three so the overlay is gone before our modal appears.
    // -------------------------------------------------------------------------

    _jetframeDismissOdooLoader() {
        // 1. jQuery blockUI (still present in Odoo 18 legacy stack)
        try {
            if (typeof window.$ !== 'undefined' && typeof window.$.unblockUI === 'function') {
                window.$.unblockUI();
            }
        } catch (_) { /* ignore if jQuery / blockUI not available */ }

        // 2. DOM-based loaders — hide them so they don't cover the modal.
        //    We hide (not remove) so Odoo can show them again if needed.
        const loaderSelectors = [
            '.o_loading',         // main Odoo loading overlay
            '.o_blockUI',         // blockUI DOM element
            '.o_loader',          // enterprise loader
            '#o_loading',
        ];
        for (const sel of loaderSelectors) {
            document.querySelectorAll(sel).forEach((el) => {
                el.dataset.jetframeHidden = '1';
                el.style.display = 'none';
            });
        }
    },

    // -------------------------------------------------------------------------
    // Restore any loaders we hid (called from _jetframeCleanup via close)
    // -------------------------------------------------------------------------

    _jetframeRestoreOdooLoaders() {
        document.querySelectorAll('[data-jetframe-hidden="1"]').forEach((el) => {
            el.style.removeProperty('display');
            delete el.dataset.jetframeHidden;
        });
    },

    // -------------------------------------------------------------------------
    // Remove the modal + backdrop and restore body scroll
    // -------------------------------------------------------------------------

    _jetframeCleanup() {
        document.getElementById(OVERLAY_ID)?.remove();
        document.getElementById(BACKDROP_ID)?.remove();
        document.body.classList.remove('modal-open');
        this._jetframeRestoreOdooLoaders?.();
    },
});
