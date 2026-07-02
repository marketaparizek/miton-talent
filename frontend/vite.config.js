import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Builds a single self-contained file: dist/miton-talent-chat.js
// React is bundled in, CSS is injected by JS, so the host page needs nothing else.
export default defineConfig({
  plugins: [react()],
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
