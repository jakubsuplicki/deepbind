// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  compatibilityDate: '2025-07-15',
  ssr: false,
  devtools: { enabled: false },

  app: {
    head: {
      link: [
        { rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' },
        { rel: 'icon', type: 'image/x-icon', href: '/favicon.ico' },
        { rel: 'icon', type: 'image/png', sizes: '32x32', href: '/favicon-32x32.png' },
        { rel: 'icon', type: 'image/png', sizes: '192x192', href: '/favicon-192x192.png' },
        { rel: 'apple-touch-icon', sizes: '180x180', href: '/apple-touch-icon.png' },
      ],
    },
  },

  css: ['~/assets/css/main.css', '~/assets/css/settings.css'],
  runtimeConfig: {
    public: {
      backendWsUrl: 'ws://127.0.0.1:8000/api/chat/ws',
      // ADR 014 §B — propagate the build-time bundle flag. Default '1' (the
      // v1 desktop-bundle). Pages that gate cloud-provider UI read this
      // via `useRuntimeConfig().public.desktopBundle === '1'` and skip
      // rendering the AddKeyModal, the API-keys settings panel, the cloud
      // entries in the chat-model picker. Set JARVIS_DESKTOP_BUNDLE=0 in
      // the build environment to produce a bundle that re-includes the
      // cloud-provider UI (used by CI's hybrid-SKU build target).
      desktopBundle: process.env.JARVIS_DESKTOP_BUNDLE ?? '1',
    },
  },
  nitro: {
    // Static SPA bundle for the Tauri shell (ADR 003 §G). `nuxt generate`
    // emits a self-contained .output/public/ that the Rust shell points
    // its WebviewWindowBuilder at. No Node server needed.
    preset: 'static',
    // Dev-only: the browser dev server proxies /api/* to the local backend.
    // Tauri shell never sees these — it injects backendUrl via
    // window.__JARVIS_CONFIG__ and frontend code routes through apiUrl().
    devProxy: {
      '/api': {
        target: 'http://127.0.0.1:8000/api',
        changeOrigin: true,
      },
    },
    routeRules: {
      '/api/**': { proxy: 'http://127.0.0.1:8000/api/**' },
    },
  },
  vite: {
    server: {
      hmr: {
        clientPort: 3000,
      },
    },
    ssr: {
      noExternal: ['force-graph', 'three'],
    },
  },
  typescript: {
    strict: true,
  },
})
