import type { Config } from 'tailwindcss';
import typography from '@tailwindcss/typography';

const config: Config = {
  content: ['./src/**/*.{html,js,svelte,ts}'],
  darkMode: 'class',
  theme: {
    extend: {
      boxShadow: {
        card: '0 18px 48px -24px rgb(15 23 42 / 0.45)'
      }
    }
  },
  plugins: [typography]
};

export default config;
