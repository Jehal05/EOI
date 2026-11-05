import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: { target: 'es2022', sourcemap: false, chunkSizeWarningLimit: 400 },
  server: { proxy: { '/api': 'http://127.0.0.1:8083', '/cim': 'http://127.0.0.1:8083', '/ems': 'http://127.0.0.1:8083', '/hist': 'http://127.0.0.1:8083' } },
  test: { environment: 'node' }
});
