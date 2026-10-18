import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server proxies the API so the page and the backend share an origin.
// Backend binds 127.0.0.1 only; nothing here is exposed to the network.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8000', rewrite: p => p.replace(/^\/api/, '') } }
  },
  build: { outDir: 'dist' }
})
