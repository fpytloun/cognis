/**
 * Battery API stub.
 *
 * Real Chrome desktop on most modern machines either lacks a battery (a
 * laptop without one, or a desktop) or returns a plausible plugged-in
 * status. Headless Chromium tends to return a suspicious flat
 * 0%-discharging baseline that some fingerprinters use as a tell.
 *
 * We replace navigator.getBattery() with a static, reasonable value
 * derived from the per-profile seed. The same profile always gets the
 * same battery reading; different profiles get different readings.
 */
(() => {
  "use strict";
  if (window.__cognis_fp_battery_applied) return;
  window.__cognis_fp_battery_applied = true;

  const seedSource = String(window.__cognis_fp_seed || "cognis-default");
  let seed = 0x811c9dc5;
  for (let i = 0; i < seedSource.length; i++) {
    seed ^= seedSource.charCodeAt(i);
    seed = (seed * 0x01000193) >>> 0;
  }
  const variance = (seed % 20) / 100; // 0.00 - 0.19
  const charging = (seed & 1) === 0;
  const level = Math.min(1.0, Math.max(0.6, 0.85 + variance * (charging ? 1 : -1)));

  const stub = {
    charging,
    chargingTime: charging ? 1800 : Infinity,
    dischargingTime: charging ? Infinity : 6000,
    level,
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {
      return true;
    },
    onchargingchange: null,
    onchargingtimechange: null,
    ondischargingtimechange: null,
    onlevelchange: null,
  };

  if (navigator && typeof navigator.getBattery === "function") {
    const original = navigator.getBattery.bind(navigator);
    Object.defineProperty(navigator, "getBattery", {
      value: function () {
        // Try the real one first; if it succeeds quickly (<50ms) we use
        // its shape but override our key fields. If it errors or never
        // resolves, fall back to the stub.
        return Promise.race([
          original().then(
            (real) => {
              try {
                Object.defineProperty(real, "charging", { configurable: true, get: () => stub.charging });
                Object.defineProperty(real, "level", { configurable: true, get: () => stub.level });
                Object.defineProperty(real, "chargingTime", { configurable: true, get: () => stub.chargingTime });
                Object.defineProperty(real, "dischargingTime", { configurable: true, get: () => stub.dischargingTime });
                return real;
              } catch {
                return stub;
              }
            },
            () => stub,
          ),
          new Promise((resolve) => setTimeout(() => resolve(stub), 50)),
        ]);
      },
      writable: true,
      configurable: true,
    });
  } else if (navigator) {
    // Some Chromium builds expose getBattery only behind a flag; install a
    // shim so probes that look for it find a plausible value.
    Object.defineProperty(navigator, "getBattery", {
      value: function () {
        return Promise.resolve(stub);
      },
      writable: true,
      configurable: true,
    });
  }
})();
