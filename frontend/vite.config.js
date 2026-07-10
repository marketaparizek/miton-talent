import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Builds a single self-contained file: dist/miton-talent-chat.js
// React is bundled in, CSS is injected by JS, so the host page needs nothing else.
export default defineConfig({
  plugins: [react()],
  // Standalone bundle ships its own React. In library mode Vite does not replace
  // process.env.NODE_ENV, and React's production build references it, which throws
  // "process is not defined" in the browser and prevents the widget from mounting.
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    lib: {
      entry: "src/main.jsx",
      name: "MitonTalentChat",
      formats: ["iife"],
      fileName: () => "miton-talent-chat.js",
    },
    rollupOptions: {
      output: { inlineDynamicImports: true },
    },
  },
});
