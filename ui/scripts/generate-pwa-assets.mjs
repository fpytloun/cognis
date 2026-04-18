#!/usr/bin/env node
/**
 * Generate PWA PNG icons and Apple splash images from static/favicon.svg.
 *
 * Usage (requires sharp, added as a devDependency):
 *   node scripts/generate-pwa-assets.mjs
 *
 * Generates:
 *   static/pwa/icon-192.png
 *   static/pwa/icon-512.png
 *   static/pwa/icon-maskable-512.png   (80% safe zone)
 *   static/pwa/apple-touch-icon.png    (180x180, precomposed)
 *   static/pwa/apple-splash-*.png      (per common iPhone sizes)
 *
 * On CI/local without sharp, this script is a no-op if sharp is missing.
 */

import { mkdirSync, existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, '..');
const outDir = resolve(root, 'static/pwa');
const sourceSvg = resolve(root, 'static/favicon.svg');

const BG = { r: 2, g: 6, b: 23 };

const iconTargets = [
  { name: 'icon-192.png', size: 192, maskable: false },
  { name: 'icon-512.png', size: 512, maskable: false },
  { name: 'icon-maskable-512.png', size: 512, maskable: true },
  { name: 'apple-touch-icon.png', size: 180, maskable: false }
];

// Common iPhone splash sizes (portrait). Landscape variants optional.
const splashTargets = [
  { name: 'apple-splash-2048-2732.png', w: 2048, h: 2732 }, // 12.9" iPad Pro
  { name: 'apple-splash-1668-2388.png', w: 1668, h: 2388 }, // 11" iPad Pro
  { name: 'apple-splash-1536-2048.png', w: 1536, h: 2048 }, // 9.7" iPad
  { name: 'apple-splash-1290-2796.png', w: 1290, h: 2796 }, // iPhone 14 Pro Max
  { name: 'apple-splash-1179-2556.png', w: 1179, h: 2556 }, // iPhone 14 Pro
  { name: 'apple-splash-1170-2532.png', w: 1170, h: 2532 }, // iPhone 13/14
  { name: 'apple-splash-1125-2436.png', w: 1125, h: 2436 }, // iPhone X/11/12/13 mini
  { name: 'apple-splash-828-1792.png', w: 828, h: 1792 },   // iPhone XR/11
  { name: 'apple-splash-750-1334.png', w: 750, h: 1334 }    // iPhone SE (2nd gen)
];

async function main() {
  let sharp;
  try {
    sharp = (await import('sharp')).default;
  } catch {
    console.warn('[pwa-assets] sharp not installed; skipping PNG generation.');
    console.warn('[pwa-assets] Install with: npm i -D sharp');
    return;
  }

  if (!existsSync(sourceSvg)) {
    console.error('[pwa-assets] source favicon missing at', sourceSvg);
    process.exit(1);
  }
  mkdirSync(outDir, { recursive: true });

  const svgBuffer = readFileSync(sourceSvg);

  for (const target of iconTargets) {
    const size = target.size;
    const inner = target.maskable ? Math.round(size * 0.64) : size;
    const pad = Math.round((size - inner) / 2);

    const canvas = sharp({
      create: {
        width: size,
        height: size,
        channels: 4,
        background: { r: BG.r, g: BG.g, b: BG.b, alpha: 1 }
      }
    });

    const logo = await sharp(svgBuffer).resize(inner, inner).png().toBuffer();

    await canvas
      .composite([{ input: logo, left: pad, top: pad }])
      .png()
      .toFile(resolve(outDir, target.name));

    console.log('[pwa-assets] wrote', target.name);
  }

  for (const splash of splashTargets) {
    const inner = Math.round(Math.min(splash.w, splash.h) * 0.25);
    const leftPad = Math.round((splash.w - inner) / 2);
    const topPad = Math.round((splash.h - inner) / 2);

    const canvas = sharp({
      create: {
        width: splash.w,
        height: splash.h,
        channels: 4,
        background: { r: BG.r, g: BG.g, b: BG.b, alpha: 1 }
      }
    });

    const logo = await sharp(svgBuffer).resize(inner, inner).png().toBuffer();

    await canvas
      .composite([{ input: logo, left: leftPad, top: topPad }])
      .png()
      .toFile(resolve(outDir, splash.name));

    console.log('[pwa-assets] wrote', splash.name);
  }
}

main().catch((err) => {
  console.error('[pwa-assets] failed:', err);
  process.exit(1);
});
