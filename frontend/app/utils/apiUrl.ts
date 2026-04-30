declare global {
  interface Window {
    __JARVIS_CONFIG__?: {
      backendUrl: string
      wsUrl: string
    }
  }
}

export function apiUrl(path: string): string {
  if (typeof window !== 'undefined' && window.__JARVIS_CONFIG__?.backendUrl) {
    return window.__JARVIS_CONFIG__.backendUrl + path
  }
  return path
}

export {}
