/* Captive-portal branding loader (shared by login/pay/success).
 *
 * Reads the signed router token (rt) from the URL, persists it in sessionStorage
 * so branding survives any hop that drops the query param (e.g. the Paystack
 * card round-trip), fetches GET /portal/branding/{rt}, and applies the operator's
 * colours/logo/text. The endpoint always returns defaults for unset fields and
 * never errors, so a missing/invalid token just yields the platform palette.
 *
 * Polling: when the response carries is_preview=true (the settings-page mobile
 * preview, resolved via a preview token — never a real router token), this
 * keeps re-fetching on an interval so in-progress (unsaved) edits show up live.
 * Real customer-facing pages never see is_preview=true, so they never poll.
 */
(function () {
    var POLL_MS = 1500;
    var RT_KEY = "portal.rt";
    var params = new URLSearchParams(window.location.search);
    var rt = params.get("rt") || "";
    try {
        if (rt) sessionStorage.setItem(RT_KEY, rt);
        else rt = sessionStorage.getItem(RT_KEY) || "";
    } catch (e) { /* sessionStorage unavailable — fall back to URL only */ }

    if (!rt) return; // no token: keep CSS defaults

    fetchAndApply();

    function fetchAndApply() {
        fetch("/portal/branding/" + encodeURIComponent(rt), { headers: { "Accept": "application/json" } })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (b) {
                if (!b) return;
                apply(b);
                if (b.is_preview) startPolling();
            })
            .catch(function () { /* leave defaults in place */ });
    }

    var polling = null;
    function startPolling() {
        if (polling) return; // already running — the first response that sets
                              // is_preview starts it once, not on every response
        polling = setInterval(fetchAndApply, POLL_MS);
    }

    function apply(b) {
        var root = document.documentElement.style;
        if (b.primary_color) root.setProperty("--primary-color", b.primary_color);
        if (b.accent_color) root.setProperty("--accent-color", b.accent_color);
        if (b.background_gradient_start) root.setProperty("--gradient-start", b.background_gradient_start);

        // Structural layout selector — drives the [data-portal-template="..."]
        // rules in style.css (card_centered needs no rules; it's the base CSS).
        if (b.template) document.documentElement.setAttribute("data-portal-template", b.template);

        // Logo: replace the inline placeholder SVG with the operator's image.
        if (b.logo_url) {
            var slot = document.querySelector("[data-brand-logo]");
            if (slot) {
                slot.innerHTML = "";
                var img = document.createElement("img");
                img.src = b.logo_url;
                img.alt = b.portal_display_name || "Logo";
                img.style.maxHeight = "56px";
                img.style.maxWidth = "180px";
                img.style.objectFit = "contain";
                slot.appendChild(img);
            }
        }

        // Header / document title.
        if (b.portal_display_name) {
            var title = document.querySelector("[data-brand-title]");
            if (title) title.textContent = b.portal_display_name;
            document.title = b.portal_display_name;
        }

        // Welcome subtitle (only when the operator set a non-empty message).
        if (b.welcome_message) {
            var welcome = document.querySelector("[data-brand-welcome]");
            if (welcome) welcome.textContent = b.welcome_message;
        }

        applyFooter(b);
    }

    // Contact footer. Explicitly writes the generic fallback text when neither
    // field is set — not just a no-op skip — so a polling preview correctly
    // reverts the footer if the operator clears a contact field mid-edit; for
    // the normal one-shot page load this produces the same generic text the
    // server already rendered, so it's a harmless overwrite either way.
    function applyFooter(b) {
        var footer = document.querySelector("[data-brand-footer]");
        if (!footer) return;
        if (!b.contact_phone && !b.contact_email) {
            footer.innerHTML = '<p>Need a voucher? Contact your local ISP office.</p>';
            return;
        }
        var links = [];
        if (b.contact_phone) links.push('<a href="tel:' + escapeAttr(b.contact_phone) + '">' + escapeText(b.contact_phone) + '</a>');
        if (b.contact_email) links.push('<a href="mailto:' + escapeAttr(b.contact_email) + '">' + escapeText(b.contact_email) + '</a>');
        footer.innerHTML = '<p>Need help? ' + links.join(' &middot; ') + '</p>';
    }

    function escapeText(s) {
        return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }
    function escapeAttr(s) {
        return escapeText(s).replace(/"/g, "&quot;");
    }
})();
