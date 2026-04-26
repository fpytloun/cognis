/**
 * Viewport jitter.
 *
 * Real users have a wide spread of innerWidth/innerHeight values; cloned
 * automation fleets often share an identical 1365x900 (or equivalent)
 * viewport. We perturb the reported inner dimensions by up to +/-2% so
 * sessions look more diverse without breaking layout assumptions.
 *
 * The jitter is deterministic per profile via window.__cognis_fp_seed,
 * so re-visits to the same site see consistent dimensions (avoids the
 * "viewport changed between requests" tell).
 */
(() => {
  "use strict";
  if (window.__cognis_fp_viewport_applied) return;
  window.__cognis_fp_viewport_applied = true;

  const seedSource = String(window.__cognis_fp_seed || "cognis-default");
  let seed = 0x811c9dc5;
  for (let i = 0; i < seedSource.length; i++) {
    seed ^= seedSource.charCodeAt(i);
    seed = (seed * 0x01000193) >>> 0;
  }
  // Map seed -> [-0.02, +0.02] for width and height independently.
  const widthDelta = (((seed % 401) - 200) / 10000); // -0.02..+0.02
  const heightSeed = (seed * 0x9e3779b1) >>> 0;
  const heightDelta = (((heightSeed % 401) - 200) / 10000);

  const realWidth = window.innerWidth;
  const realHeight = window.innerHeight;
  if (!realWidth || !realHeight) return;

  const fakeWidth = Math.max(640, Math.round(realWidth * (1 + widthDelta)));
  const fakeHeight = Math.max(480, Math.round(realHeight * (1 + heightDelta)));

  try {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      get: () => fakeWidth,
    });
    Object.defineProperty(window, "innerHeight", {
      configurable: true,
      get: () => fakeHeight,
    });
  } catch {
    /* swallow: some Chromium builds make these non-configurable */
  }
})();
