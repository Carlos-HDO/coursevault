// Tailwind v3.4.16 (binário standalone, sem Node). Usado por tools/update_static.py para gerar
// static/app.css: rode de novo sempre que mudar classes no index.html.
module.exports = {
  content: ['./index.html'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      colors: {
        ink: { 950: '#0f0f11', 900: '#141416', 850: '#18181b', 800: '#1e1e22', 700: '#2a2a30', 600: '#36363d', 500: '#4b4b55' },
        accent: { DEFAULT: '#8b7cf6', soft: '#b3a8ff', deep: '#6d5ce8' },
      },
    },
  },
  plugins: [require('@tailwindcss/typography')],
};
