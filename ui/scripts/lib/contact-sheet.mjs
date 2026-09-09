import sharp from 'sharp';

export async function createContactSheet(entries, outputPath, { columns = 3, cellWidth = 420 } = {}) {
  const prepared = await Promise.all(entries.map(async ({ path, label }) => {
    const image = sharp(path);
    const metadata = await image.metadata();
    const scaledHeight = Math.max(1, Math.round((metadata.height ?? cellWidth) * cellWidth / (metadata.width ?? cellWidth)));
    const body = await image.resize({ width: cellWidth }).png().toBuffer();
    return { body, label, height: scaledHeight + 56 };
  }));
  const rows = Math.ceil(prepared.length / columns);
  const rowHeights = Array.from({ length: rows }, (_, row) =>
    Math.max(...prepared.slice(row * columns, (row + 1) * columns).map((entry) => entry.height))
  );
  const width = columns * cellWidth;
  const height = rowHeights.reduce((sum, value) => sum + value, 0);
  const composites = [];
  let rowTop = 0;
  for (let index = 0; index < prepared.length; index += 1) {
    const row = Math.floor(index / columns);
    if (index > 0 && index % columns === 0) rowTop += rowHeights[row - 1];
    const left = (index % columns) * cellWidth;
    const entry = prepared[index];
    const labelSvg = Buffer.from(
      `<svg width="${cellWidth}" height="56"><rect width="100%" height="100%" fill="#0f172a"/>`
      + `<text x="18" y="34" fill="#f8fafc" font-family="sans-serif" font-size="16">${escapeXml(entry.label)}</text></svg>`
    );
    composites.push({ input: labelSvg, left, top: rowTop });
    composites.push({ input: entry.body, left, top: rowTop + 56 });
  }
  await sharp({
    create: { width, height, channels: 4, background: '#e2e8f0' },
  }).composite(composites).png().toFile(outputPath);
}

function escapeXml(value) {
  return value.replace(/[<>&'"]/g, (character) => ({
    '<': '&lt;', '>': '&gt;', '&': '&amp;', "'": '&apos;', '"': '&quot;',
  })[character]);
}
