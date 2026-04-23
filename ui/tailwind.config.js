/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        display: ['"Bricolage Grotesque"', 'sans-serif'],
        serif: ['"Instrument Serif"', 'serif'],
        mono: ['"IBM Plex Mono"', 'monospace'],
      },
      boxShadow: {
        stamp: '10px 10px 0 rgba(245, 94, 10, 0.22)',
      },
      colors: {
        ink: '#0f1720',
        paper: '#f7efe3',
        rust: '#f55e0a',
        smoke: '#a7b0b8',
        brass: '#d2a85a',
      },
      backgroundImage: {
        grain: 'radial-gradient(circle at 20% 20%, rgba(245,94,10,0.18), transparent 25%), radial-gradient(circle at 80% 0%, rgba(210,168,90,0.18), transparent 30%), linear-gradient(135deg, rgba(255,255,255,0.02), rgba(255,255,255,0))',
      },
    },
  },
  plugins: [],
};
