import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { preprocessCSS, resolveConfig } from 'vite';

let compiled: Promise<string> | undefined;

export function productionCss(): Promise<string> {
  compiled ??= resolveConfig({ configFile: false }, 'build').then(async (config) => {
    const filename = resolve('src/app.css');
    const result = await preprocessCSS(readFileSync(filename, 'utf8'), filename, config);
    return result.code;
  });
  return compiled;
}
