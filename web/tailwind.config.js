/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Linear / Vercel 现代暗夜 + 翡翠薄荷 & 电光靛蓝
        canvas: '#09090b',
        mint: {
          50: '#F2FBF7',
          100: '#E1F7ED',
          200: '#C3EFDB',
          300: '#9AE3C3',
          400: '#48CE94',
          500: '#10B981',
          600: '#059669',
          700: '#047857',
          800: '#065F46',
          900: '#064E3B',
        },
      },
      boxShadow: {
        'glow-mint': '0 0 25px -5px rgba(16, 185, 129, 0.25)',
        'glow-indigo': '0 0 25px -5px rgba(99, 102, 241, 0.25)',
        'glow-amber': '0 0 25px -5px rgba(245, 158, 11, 0.25)',
        'inner-hairline': 'inset 0 1px 0 0 rgba(255, 255, 255, 0.05)',
        'mint-sm': '0 2px 8px rgba(16, 185, 129, 0.06)',
        'mint-card': '0 10px 25px -5px rgba(0, 0, 0, 0.3), 0 8px 10px -6px rgba(0, 0, 0, 0.2)',
        'mint-glow': '0 0 20px rgba(52, 211, 153, 0.25)',
        'mint-subtle': '0 1px 3px rgba(16, 185, 129, 0.05), 0 1px 2px rgba(0, 0, 0, 0.02)',
        'glass-pill': '0 4px 16px 0 rgba(16, 185, 129, 0.12), inset 0 1px 1px 0 rgba(255, 255, 255, 0.1)',
      },
      transitionTimingFunction: {
        spring: 'cubic-bezier(0.16, 1, 0.3, 1)',
        bounce: 'cubic-bezier(0.34, 1.45, 0.7, 1)',
      },
    },
  },
  plugins: [],
}
