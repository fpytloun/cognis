#!/usr/bin/env node
/**
 * Generate favicon and PWA assets from the canonical root Logo.JPG raster.
 *
 * Usage (requires sharp, added as a devDependency):
 *   node scripts/generate-pwa-assets.mjs
 *
 * Generates:
 *   static/favicon.svg              (SVG wrapper around the raster source)
 *   static/favicon.ico              (PNG-compressed ICO, 16/32/48px)
 *   static/pwa/icon.svg             (SVG wrapper around the raster source)
 *   static/pwa/icon-192.png
 *   static/pwa/icon-512.png
 *   static/pwa/icon-maskable-512.png   (80% safe zone)
 *   static/pwa/apple-touch-icon.png    (180x180, precomposed)
 *   static/pwa/apple-splash-*.png      (per common iPhone sizes)
 *
 * On CI/local without sharp, this script is a no-op if sharp is missing.
 */

import { mkdirSync, existsSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, '..');
const staticDir = resolve(root, 'static');
const outDir = resolve(staticDir, 'pwa');
const sourceLogo = resolve(root, '..', 'Logo.JPG');

const BG = { r: 4, g: 12, b: 15 };

function makeRasterSvg(dataUri) {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" role="img" aria-label="Cognis">
  <image href="${dataUri}" width="1024" height="1024" preserveAspectRatio="xMidYMid slice" />
</svg>
`;
}

function makeIco(entries) {
  const headerSize = 6;
  const directorySize = entries.length * 16;
  let imageOffset = headerSize + directorySize;
  const header = Buffer.alloc(headerSize + directorySize);

  header.writeUInt16LE(0, 0); // reserved
  header.writeUInt16LE(1, 2); // icon
  header.writeUInt16LE(entries.length, 4);

  entries.forEach((entry, index) => {
    const dirOffset = headerSize + index * 16;
    header.writeUInt8(entry.size >= 256 ? 0 : entry.size, dirOffset);
    header.writeUInt8(entry.size >= 256 ? 0 : entry.size, dirOffset + 1);
    header.writeUInt8(0, dirOffset + 2); // color count
    header.writeUInt8(0, dirOffset + 3); // reserved
    header.writeUInt16LE(1, dirOffset + 4); // color planes
    header.writeUInt16LE(32, dirOffset + 6); // bits per pixel
    header.writeUInt32LE(entry.buffer.length, dirOffset + 8);
    header.writeUInt32LE(imageOffset, dirOffset + 12);
    imageOffset += entry.buffer.length;
  });

  return Buffer.concat([header, ...entries.map((entry) => entry.buffer)]);
}

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

  if (!existsSync(sourceLogo)) {
    console.error('[pwa-assets] source logo missing at', sourceLogo);
    process.exit(1);
  }
  mkdirSync(staticDir, { recursive: true });
  mkdirSync(outDir, { recursive: true });

  const logoBuffer = readFileSync(sourceLogo);
  const logoDataUri = `data:image/jpeg;base64,${logoBuffer.toString('base64')}`;
  const svgWrapper = makeRasterSvg(logoDataUri);
  writeFileSync(resolve(staticDir, 'favicon.svg'), svgWrapper);
  writeFileSync(resolve(outDir, 'icon.svg'), svgWrapper);
  console.log('[pwa-assets] wrote favicon.svg');
  console.log('[pwa-assets] wrote icon.svg');

  const icoEntries = await Promise.all(
    [16, 32, 48].map(async (size) => ({
      size,
      buffer: await sharp(logoBuffer).resize(size, size, { fit: 'cover' }).png().toBuffer()
    }))
  );
  writeFileSync(resolve(staticDir, 'favicon.ico'), makeIco(icoEntries));
  console.log('[pwa-assets] wrote favicon.ico');

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

    const logo = await sharp(logoBuffer).resize(inner, inner, { fit: 'cover' }).png().toBuffer();

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

    const logo = await sharp(logoBuffer).resize(inner, inner, { fit: 'cover' }).png().toBuffer();

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
