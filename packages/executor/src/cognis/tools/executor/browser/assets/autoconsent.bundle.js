/**
 * cognis-autoconsent — curated CMP (cookie consent) auto-dismisser.
 *
 * This script runs as a page init script on every navigation. It looks for
 * known cookie-consent dialogs (OneTrust, Cookiebot, Quantcast, Sourcepoint,
 * Didomi, TrustArc, Iubenda, Usercentrics, CookieYes, Borlabs, plus generic
 * heuristics) and clicks the configured action button.
 *
 * Configuration is supplied by the controller via two globals injected
 * BEFORE this script:
 *
 *   window.__cognis_autoconsent = {
 *     action: "accept" | "reject" | "off",
 *     delayMs: 800,
 *     disabledHosts: ["example.com", ...]
 *   };
 *
 * If `action === "off"` or the current hostname matches any entry in
 * `disabledHosts` (suffix match), the script no-ops.
 *
 * This is a self-contained, runtime-agnostic alternative to a full library
 * like @duckduckgo/autoconsent. It is deliberately small (no
 * controller/page message protocol) so it works identically in headless
 * Playwright, headed Patchright, and inside extensions if reused later.
 */
(() => {
  "use strict";

  const cfg = (window.__cognis_autoconsent || {});
  const action = (cfg.action || "off").toLowerCase();
  if (action !== "accept" && action !== "reject") return;

  const disabledHosts = Array.isArray(cfg.disabledHosts) ? cfg.disabledHosts : [];
  const host = (window.location && window.location.hostname || "").toLowerCase();
  for (const entry of disabledHosts) {
    if (typeof entry !== "string") continue;
    const norm = entry.trim().toLowerCase();
    if (!norm) continue;
    if (host === norm || host.endsWith("." + norm)) return;
  }

  const delayMs = Math.max(0, Math.min(15000, Number(cfg.delayMs) || 800));

  // Some CMPs render their controls in closed shadow roots. Keep only
  // consent-shaped hosts inspectable so generic rules can reach them without
  // opening unrelated application components.
  const nativeAttachShadow = Element.prototype.attachShadow;
  function consentAttachShadow(init) {
    const marker = normalizeText([
      this.tagName,
      this.id,
      typeof this.className === "string" ? this.className : "",
      this.getAttribute && this.getAttribute("role"),
      this.getAttribute && this.getAttribute("aria-label"),
    ].filter(Boolean).join(" "));
    if (/(cmp|consent|cookie|privacy)/.test(marker)) {
      init = Object.assign({}, init, { mode: "open" });
    }
    return nativeAttachShadow.call(this, init);
  }
  Element.prototype.attachShadow = consentAttachShadow;
  let attemptRoots = null;

  function restoreAttachShadow() {
    if (Element.prototype.attachShadow === consentAttachShadow) {
      Element.prototype.attachShadow = nativeAttachShadow;
    }
  }

  // Per-CMP rules. Order matters: most specific first, most generic last.
  // Each rule has:
  //   name:     CMP identifier (logged on success)
  //   detect:   selector that, if present + visible, indicates the CMP
  //   accept:   selectors to click for "accept all"
  //   reject:   selectors to click for "reject all" (deny non-essential)
  //   prep:     optional selector(s) to click first (e.g. "Customise")
  const RULES = [
    {
      name: "OneTrust",
      detect: "#onetrust-banner-sdk, #onetrust-consent-sdk",
      accept: ["#onetrust-accept-btn-handler", ".onetrust-close-btn-handler"],
      reject: [
        "#onetrust-reject-all-handler",
        ".ot-pc-refuse-all-handler",
      ],
      prep: ["#onetrust-pc-btn-handler"],
    },
    {
      name: "Cookiebot",
      detect: "#CybotCookiebotDialog",
      accept: ["#CybotCookiebotDialogBodyLevelButtonAcceptAll", "#CybotCookiebotDialogBodyButtonAccept"],
      reject: ["#CybotCookiebotDialogBodyButtonDecline", "#CybotCookiebotDialogBodyLevelButtonCustomizeClose"],
    },
    {
      name: "Quantcast",
      detect: ".qc-cmp2-summary-buttons, .qc-cmp2-container",
      accept: ['button[mode="primary"]'],
      reject: ['button[mode="secondary"]', 'button[aria-label*="Reject" i]'],
    },
    {
      name: "Sourcepoint",
      detect: '[class*="sp_message_container"], [id*="sp_message_container"]',
      accept: ['button[title="Accept"]', '.message-button.accept-all', 'button[aria-label*="Accept" i]'],
      reject: ['button[title*="Reject" i]', '.message-button[title*="Reject" i]'],
    },
    {
      name: "Didomi",
      detect: "#didomi-host, #didomi-notice",
      accept: ["#didomi-notice-agree-button"],
      reject: ["#didomi-notice-disagree-button", '[data-testid="continue-without-agreeing"]'],
    },
    {
      name: "TrustArc",
      detect: "#consent_blackbar, #truste-consent-track",
      accept: ["#truste-consent-button"],
      reject: ["#truste-consent-required"],
    },
    {
      name: "Iubenda",
      detect: "#iubenda-cs-banner",
      accept: [".iubenda-cs-accept-btn"],
      reject: [".iubenda-cs-reject-btn", ".iubenda-cs-customize-btn"],
    },
    {
      name: "Usercentrics",
      detect: "#usercentrics-root, #uc-main-banner",
      accept: ['[data-testid="uc-accept-all-button"]', 'button[data-action="acceptAll"]'],
      reject: ['[data-testid="uc-deny-all-button"]', 'button[data-action="denyAll"]'],
    },
    {
      name: "CookieYes",
      detect: ".cky-consent-container, .cky-modal",
      accept: [".cky-btn-accept"],
      reject: [".cky-btn-reject"],
    },
    {
      name: "Borlabs",
      detect: "#BorlabsCookieBox",
      accept: ['[data-cookie-accept-all]', '#CookiePrefSave'],
      reject: ['[data-cookie-refuse]', '#CookiePrefSave'],
    },
    {
      name: "Osano",
      detect: ".osano-cm-window",
      accept: [".osano-cm-accept-all"],
      reject: [".osano-cm-denyAll", ".osano-cm-deny"],
    },
    {
      name: "Klaro",
      detect: ".klaro .cookie-modal, .klaro .cookie-notice",
      accept: [".klaro .cm-btn-accept-all", ".klaro .cm-btn-accept"],
      reject: [".klaro .cm-btn-decline"],
    },
    {
      name: "Termly",
      detect: ".termly-styled-banner, [data-testid='banner-container']",
      accept: ["[data-tid='banner-accept']", "button[aria-label*='Accept All' i]"],
      reject: ["[data-tid='banner-decline']", "button[aria-label*='Decline All' i]"],
    },
    {
      name: "WordPress GDPR Cookie Compliance (Moove)",
      detect: "#moove_gdpr_cookie_info_bar",
      accept: ["#moove_gdpr_cookie_info_bar .mgbutton.moove-gdpr-infobar-allow-all"],
      reject: ["#moove_gdpr_cookie_info_bar .mgbutton.moove-gdpr-infobar-reject-btn"],
    },
    {
      name: "Complianz",
      detect: ".cmplz-cookiebanner, #cmplz-cookiebanner-container",
      accept: [".cmplz-accept", ".cmplz-btn.cmplz-accept-all"],
      reject: [".cmplz-deny", ".cmplz-btn.cmplz-deny"],
    },
    {
      name: "GDPR Cookie Consent (CookieLawInfo)",
      detect: "#cookie-law-info-bar",
      accept: ["#cookie_action_close_header", "#wt-cli-accept-all-btn"],
      reject: ["#wt-cli-reject-btn"],
    },
  ];

  function isVisible(el) {
    if (!el || !(el instanceof HTMLElement)) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none" || parseFloat(style.opacity) === 0) {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function clickIfVisible(selector) {
    const nodes = queryAllDeep(selector);
    for (const node of nodes) {
      if (isVisible(node)) {
        try {
          node.click();
          markClicked();
          return true;
        } catch {
          /* swallow click errors, try next */
        }
      }
    }
    return false;
  }

  function tryRule(rule) {
    const detector = queryOneDeep(rule.detect);
    if (!detector || !isVisible(detector)) return false;
    const targets = (action === "accept" ? rule.accept : rule.reject) || [];
    if (!targets.length) return false;
    for (const sel of targets) {
      if (clickIfVisible(sel)) {
        try {
          // eslint-disable-next-line no-console
          console.debug("[cognis-autoconsent] " + rule.name + " " + action);
        } catch {
          /* console may be unavailable in some sandboxed contexts */
        }
        return true;
      }
    }
    return false;
  }

  function normalizeText(value) {
    return String(value || "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase();
  }

  function rootsDeep() {
    if (attemptRoots) return attemptRoots;
    const roots = [document];
    for (let index = 0; index < roots.length; index += 1) {
      const root = roots[index];
      for (const element of root.querySelectorAll("*")) {
        if (element.shadowRoot) roots.push(element.shadowRoot);
      }
    }
    attemptRoots = roots;
    return attemptRoots;
  }

  function queryAllDeep(selector) {
    const nodes = [];
    for (const root of rootsDeep()) {
      nodes.push(...root.querySelectorAll(selector));
    }
    return nodes;
  }

  function queryOneDeep(selector) {
    for (const root of rootsDeep()) {
      const node = root.querySelector(selector);
      if (node) return node;
    }
    return null;
  }

  function elementText(el) {
    return normalizeText([
      el.innerText,
      el.textContent,
      el.getAttribute("aria-label"),
      el.getAttribute("title"),
      el.getAttribute("value"),
      el.getAttribute("data-testid"),
    ].filter(Boolean).join(" "));
  }

  const BANNER_TOKENS = [
    "cookie", "cookies", "consent", "gdpr", "privacy", "personal data",
    "soubory cookie", "cookies", "souhlas", "osobni udaje", "ochrana osobnich udaju",
    "sukromi", "sukromie", "udaje", "ochrana osobnych udajov",
    "ciasteczka", "zgoda", "prywatnosc", "dane osobowe",
    "datenschutz", "einwilligung", "personenbezogene daten",
    "confidentialite", "donnees personnelles", "consentement",
    "privacidad", "datos personales", "consentimiento",
    "privacidade", "dados pessoais", "consentimento",
    "privacy", "dati personali", "consenso",
    "prywatnosci", "datove", "udaju",
  ];

  const ACCEPT_LABELS = [
    "accept all", "accept", "agree", "i agree", "ok", "got it", "allow all", "allow", "continue",
    "rozumim a souhlasim", "souhlasim", "prijmout vse", "prijmout vsechny", "povolit vse", "pokracovat", "rozumim",
    "suhlasim", "prijat vsetko", "prijat vsetky", "povolit vsetko",
    "akceptuj wszystko", "zaakceptuj wszystkie", "zgadzam sie", "zgoda", "zezwol na wszystko",
    "alle akzeptieren", "akzeptieren", "zustimmen", "ich stimme zu", "einverstanden", "alle zulassen",
    "tout accepter", "accepter", "j accepte", "autoriser tout",
    "aceptar todo", "aceptar", "estoy de acuerdo", "permitir todo",
    "aceitar tudo", "aceitar", "concordo", "permitir tudo",
    "accetta tutto", "accetta", "acconsento", "consenti tutto",
    "alles accepteren", "accepteren", "ik ga akkoord", "alles toestaan",
    "godta alle", "godta", "acceptera alla", "acceptera", "acceptar alle", "acceptar",
  ];

  const REJECT_LABELS = [
    "reject all", "reject", "decline", "decline all", "deny", "deny all", "only essential", "only necessary", "necessary only",
    "pouze nezbytne cookies", "pouze nezbytne", "jen nezbytne", "odmitnout vse", "odmitnout", "nesouhlasim",
    "iba nevyhnutne", "len nevyhnutne", "odmietnut vsetko", "odmietnut", "nesuhlasim",
    "tylko niezbedne", "odrzuc wszystkie", "odrzuc", "nie zgadzam sie",
    "nur notwendige", "nur erforderliche", "alle ablehnen", "ablehnen", "nicht zustimmen",
    "uniquement necessaires", "tout refuser", "refuser", "je refuse",
    "solo necesarias", "rechazar todo", "rechazar", "no acepto",
    "apenas necessarios", "rejeitar tudo", "rejeitar", "nao aceito",
    "solo necessari", "rifiuta tutto", "rifiuta", "non accetto",
    "alleen noodzakelijke", "alles weigeren", "weigeren",
  ];

  function labelMatches(label, candidates) {
    if (!label) return false;
    const compact = label.replace(/["'“”„]/g, "").trim();
    return candidates.some((candidate) => {
      if (compact === candidate) return true;
      return compact.length <= 80 && compact.includes(candidate);
    });
  }

  const LEGAL_LINK_TOKENS = [
    "privacy policy", "privacy & cookies", "privacy and cookies", "cookie policy",
    "third party cookie", "social media cookies", "terms", "legal", "support",
    "documentation", "learn more", "more info", "read more",
    "cookie-erklarung", "cookie erklarung",
  ];

  function isLegalOrNavigationCandidate(el, label) {
    const href = normalizeText((el.getAttribute && el.getAttribute("href")) || "");
    const combined = (label + " " + href).trim();
    if (LEGAL_LINK_TOKENS.some((token) => combined.includes(token))) return true;
    const chromeAncestor = el.closest && el.closest("footer, header, nav");
    if (!chromeAncestor) return false;
    return !(el.closest("[role='dialog'], [aria-modal='true'], dialog"));
  }

  function isActionControl(el, banner) {
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "button") return true;
    if (tag === "input") return true;
    if ((el.getAttribute && el.getAttribute("role")) === "button") return true;
    if (tag !== "a") return false;
    if (banner && banner.matches("[role='dialog'], [aria-modal='true'], dialog")) return true;
    const className = String(el.className || "").toLowerCase();
    return /\b(btn|button|consent|accept|reject|agree)\b/.test(className);
  }

  // Heuristic fallback: scan visible <button>/<a> for multilingual consent text.
  // Only used after every named rule failed and a banner-shaped container
  // is likely present.
  function heuristicFallback() {
    const banners = [];
    for (const el of queryAllDeep("div, section, aside, dialog, [role='dialog'], [aria-modal='true']")) {
      if (!isVisible(el)) continue;
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      const isOverlay = style.position === "fixed" || style.position === "sticky" || style.position === "absolute";
      const isDialog = el.matches("dialog, [role='dialog'], [aria-modal='true']");
      const isLargeDialog = rect.width >= Math.min(window.innerWidth * 0.45, 480) && rect.height >= 120;
      if (!isDialog && !isOverlay) continue;
      if (!isDialog && !isLargeDialog) continue;
      const text = normalizeText(el.innerText || el.textContent || "");
      if (text.length < 8 || text.length > 8000) continue;
      if (BANNER_TOKENS.some((token) => text.includes(token))) {
        banners.push(el);
      }
    }
    if (!banners.length) return false;

    const labels = action === "accept" ? ACCEPT_LABELS : REJECT_LABELS;

    for (const banner of banners) {
      const buttons = banner.querySelectorAll("button, a, [role='button'], input[type='button'], input[type='submit']");
      for (const btn of buttons) {
        if (!isVisible(btn)) continue;
        if (!isActionControl(btn, banner)) continue;
        const label = elementText(btn);
        if (isLegalOrNavigationCandidate(btn, label)) continue;
        if (!labelMatches(label, labels)) continue;
        try {
          btn.click();
          markClicked();
          try { console.debug("[cognis-autoconsent] heuristic " + action + ": " + label); } catch {}
          return true;
        } catch {
          /* try next */
        }
      }
    }
    return false;
  }

  function removeConsentBackdrops() {
    for (const el of queryAllDeep("div, section, aside, dialog, [role='dialog'], [aria-modal='true']")) {
      if (!isVisible(el)) continue;
      const style = window.getComputedStyle(el);
      if (style.position !== "fixed" && style.position !== "sticky" && style.position !== "absolute") continue;
      const text = normalizeText(el.innerText || el.textContent || "");
      if (text.length < 8 || text.length > 8000) continue;
      if (!BANNER_TOKENS.some((token) => text.includes(token))) continue;
      const labels = Array.from(el.querySelectorAll("button, a, [role='button'], input[type='button'], input[type='submit']"))
        .map((btn) => elementText(btn));
      const hasAction = labels.some((label) => labelMatches(label, ACCEPT_LABELS) || labelMatches(label, REJECT_LABELS));
      if (!hasAction) continue;
      try {
        el.remove();
      } catch {
        try { el.style.display = "none"; } catch {}
      }
    }
    const body = document.body;
    if (body) {
      try {
        body.style.overflow = "auto";
        body.style.position = "";
      } catch {
        /* ignore style cleanup failures */
      }
    }
  }

  function clickedCookieBannerRecently() {
    try {
      const clickedAt = Number(window.__cognis_autoconsent_clicked_at || 0);
      return clickedAt > 0 && Date.now() - clickedAt < 5000;
    } catch {
      return false;
    }
  }

  function markClicked() {
    try {
      window.__cognis_autoconsent_clicked_at = Date.now();
    } catch {
      /* ignore */
    }
  }

  function cleanupAfterClick() {
    if (!clickedCookieBannerRecently()) return;
    restoreAttachShadow();
    setTimeout(removeConsentBackdrops, 250);
    setTimeout(removeConsentBackdrops, 1000);
  }

  function attempt() {
    attemptRoots = null;
    for (const rule of RULES) {
      if (tryRule(rule)) {
        cleanupAfterClick();
        return true;
      }
    }
    const clicked = heuristicFallback();
    if (clicked) cleanupAfterClick();
    return clicked;
  }

  function start() {
    // Try once after the configured delay, then poll a few times to handle
    // banners injected late by JS (Sourcepoint, Quantcast, etc. often do).
    const deadline = Date.now() + 8000;
    let attempts = 0;
    const tick = () => {
      attempts += 1;
      if (attempt()) return;
      if (Date.now() < deadline && attempts < 20) {
        setTimeout(tick, 400);
      } else {
        restoreAttachShadow();
      }
    };
    setTimeout(tick, delayMs);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
