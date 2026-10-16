import { defineConfig } from 'vite';
export default defineConfig({
  server:{proxy:{'/api':'http://127.0.0.1:8765','/health':'http://127.0.0.1:8765'}},
  build:{rollupOptions:{input:{app:'index.html',preview:'preview.html'}}},
});
