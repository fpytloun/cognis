import type { Config } from 'tailwindcss';
import typography from '@tailwindcss/typography';

const config: Config = {
  content: ['./src/**/*.{html,js,svelte,ts}'],
  darkMode: 'class',
  theme: {
    extend: {
      boxShadow: {
        card: '0 18px 48px -24px rgb(2 6 23 / 0.6), 0 0 0 1px rgb(56 189 248 / 0.05)'
      }
    }
  },
  plugins: [typography]
};

export default config;
