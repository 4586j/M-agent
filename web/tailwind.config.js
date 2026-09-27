/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // 生机绿白视觉系统 v1.0（docs/客户端界面设计.md §5）——业务代码只用语义类，不写裸色值
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
        'mint-sm': '0 2px 8px rgba(16, 185, 129, 0.06)',
        'mint-card':
          '0 10px 25px -5px rgba(16, 185, 129, 0.08), 0 8px 10px -6px rgba(16, 185, 129, 0.04)',
        'mint-glow': '0 0 20px rgba(52, 211, 153, 0.25)',
      },
    },
  },
  plugins: [],
}
