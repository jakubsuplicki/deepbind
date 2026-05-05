// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  compatibilityDate: '2025-07-15',
  ssr: false,
  devtools: { enabled: false },

  modules: ['@nuxt/icon'],

  // Icon system (ADR 017): Phosphor for UI vocabulary, Simple Icons for brand
  // marks. Both sets are bundled locally so the Tauri shell never reaches
  // api.iconify.design at runtime — required for offline-first.
  icon: {
    provider: 'iconify',
    serverBundle: false,
    // Offline-first per ADR 014: every referenced icon is baked into the SPA
    // bundle at build time; no runtime fetch to api.iconify.design.
    fallbackToApi: false,
    clientBundle: {
      scan: true,
      includeCustomCollections: true,
      sizeLimitKb: 512,
    },
    collections: ['ph', 'simple-icons'],
    mode: 'svg',
    class: 'icon',
  },

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
