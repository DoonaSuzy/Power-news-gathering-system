/**
 * Vite 配置。
 *
 * 为什么用 proxy 而不是让前端直连 8123：
 * 1. 浏览器会因跨端口触发 CORS 预检，中台的 CORS 白名单里加 dev 端口是额外配置负担；
 * 2. 本机常驻 HTTP 代理，`127.0.0.1` 不排除的话请求会被代理拦成 502，
 *    而走同源 `/api` 前缀就绕开了这个坑；
 * 3. 生产部署时前端与中台同域，`/api` 由网关转发，前端代码一行都不用改。
 */
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8123',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    // 单页轻量应用，不做代码分割：多一个 chunk 就多一次请求，得不偿失
    chunkSizeWarningLimit: 800,
  },
})
