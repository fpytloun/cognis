/**
 * AudioContext fingerprint perturbation.
 *
 * Adds tiny deterministic noise to AudioBuffer.getChannelData() return
 * values so that audio-fingerprint probes (used by some bot-detectors)
 * see a stable but non-default fingerprint per profile.
 *
 * The noise level is well below human perception (~1e-7) and below the
 * threshold most fingerprinters compare against; it is high enough to
 * differentiate from the unmodified Chromium baseline.
 *
 * The seed is pulled from window.__cognis_fp_seed (string), set by the
 * Python BrowserManager from a hash of the profile_id (or the session
 * generation when no profile is present). Same input -> same output, so
 * re-visits to the same site see a consistent fingerprint.
 */
(() => {
  "use strict";
  if (window.__cognis_fp_audio_applied) return;
  window.__cognis_fp_audio_applied = true;

  const seedSource = String(window.__cognis_fp_seed || "cognis-default");
  // Fast 32-bit hash (FNV-1a).
  let seed = 0x811c9dc5;
  for (let i = 0; i < seedSource.length; i++) {
    seed ^= seedSource.charCodeAt(i);
    seed = (seed * 0x01000193) >>> 0;
  }

  function mulberry32(s) {
    return function () {
      s = (s + 0x6D2B79F5) >>> 0;
      let t = s;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const rand = mulberry32(seed);

  function perturb(buffer) {
    if (!buffer || typeof buffer.length !== "number") return buffer;
    // Apply <= 1e-7 noise; below human hearing and below most
    // fingerprint thresholds but enough to differ from the baseline.
    const len = buffer.length;
    for (let i = 0; i < len; i++) {
      buffer[i] = buffer[i] + (rand() * 0.0000001 - 0.00000005);
    }
    return buffer;
  }

  const ABProto = window.AudioBuffer && window.AudioBuffer.prototype;
  if (ABProto && typeof ABProto.getChannelData === "function") {
    const orig = ABProto.getChannelData;
    ABProto.getChannelData = function (channel) {
      const data = orig.call(this, channel);
      try {
        return perturb(data);
      } catch {
        return data;
      }
    };
  }
})();
