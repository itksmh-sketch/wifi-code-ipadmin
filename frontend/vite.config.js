import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: '/admin/',
  build: {
    outDir: '../backend/static/admin',
    emptyOutDir: true,
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      // So the branding-settings mobile preview iframe (src="/portal/login?...")
      // resolves under `npm run dev` too — production already serves /admin/
      // and /portal/ from the same FastAPI app/origin, so this is dev-only.
      '/portal': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      // Platform identity CSS/icons linked from index.html (served by the backend).
      '/platform-ui': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
