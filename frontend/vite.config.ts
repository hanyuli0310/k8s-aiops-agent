import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        // 分包：此前全部塞进单个 4.4MB 的 index chunk，首屏要等它整个下载完。
        // 这几个依赖体积大且更新频率远低于业务代码，拆出来后可长期走浏览器缓存。
        manualChunks: {
          react: ['react', 'react-dom'],
          antd: ['antd', '@ant-design/icons'],
          graph: ['@antv/g6'],
          markdown: ['react-markdown', 'remark-gfm'],
        },
      },
    },
    // mermaid 等图表库单体就接近 700KB，调高阈值避免每次构建都刷无意义的警告；
    // 真正的优化是上面的分包与 mermaid 的按需动态导入。
    chunkSizeWarningLimit: 1200,
  },
});
