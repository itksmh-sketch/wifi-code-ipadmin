/* Captive-portal branding loader (shared by login/pay/success).
 *
 * Reads the signed router token (rt) from the URL, persists it in sessionStorage
 * so branding survives any hop that drops the query param (e.g. the Paystack
 * card round-trip), fetches GET /portal/branding/{rt}, and applies the operator's
 * colours/logo/text. The endpoint always returns defaults for unset fields and
 * never errors, so a missing/invalid token just yields the platform palette.
 */
(function () {
    var RT_KEY = "portal.rt";
    var params = new URLSearchParams(window.location.search);
    var rt = params.get("rt") || "";
    try {
        if (rt) sessionStorage.setItem(RT_KEY, rt);
        else rt = sessionStorage.getItem(RT_KEY) || "";
    } catch (e) { /* sessionStorage unavailable — fall back to URL only */ }

    if (!rt) return; // no token: keep CSS defaults

    fetch("/portal/branding/" + encodeURIComponent(rt), { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (b) { if (b) apply(b); })
        .catch(function () { /* leave defaults in place */ });

    function apply(b) {
        var root = document.documentElement.style;
        if (b.primary_color) root.setProperty("--primary-color", b.primary_color);
        if (b.accent_color) root.setProperty("--accent-color", b.accent_color);
        if (b.background_gradient_start) root.setProperty("--gradient-start", b.background_gradient_start);

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
    }
})();
