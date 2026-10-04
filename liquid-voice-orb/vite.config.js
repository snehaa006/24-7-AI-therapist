import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    // FastAPI backend (see ../server). Keeps the Gemini key out of the browser.
    // API_PORT lets the end-to-end test point a second dev server at a stubbed backend.
    proxy: { '/api': `http://127.0.0.1:${process.env.API_PORT || 8000}` },
    // Lets an ngrok tunnel (ngrok http 5173) reach the dev server; Vite blocks unknown hostnames otherwise.
    allowedHosts: ['.ngrok-free.app', '.ngrok-free.dev', '.ngrok.app', '.ngrok.dev', '.ngrok.io'],
  },
});
