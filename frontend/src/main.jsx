import React from "react";
import { createRoot } from "react-dom/client";
import MitonTalentChat from "./MitonTalentChat.jsx";

/*
 * Auto-mount entry for the standalone embed build.
 *
 * On the page:
 *   <div id="miton-talent-chat"
 *        data-backend="https://your-backend-url"
 *        data-lang="cs"
 *        style="max-width:640px;height:600px;margin:0 auto"></div>
 *   <script src="miton-talent-chat.js"></script>
 *
 * data-backend can be omitted if VITE_BACKEND_URL is set at build time.
 */

function mount() {
  const el = document.getElementById("miton-talent-chat");
  if (!el) return;
  const backendUrl = el.dataset.backend || import.meta.env.VITE_BACKEND_URL || "http://localhost:8000";
  const defaultLang = el.dataset.lang || "cs";
  const height = el.dataset.height || el.style.height || "600px";
  createRoot(el).render(
    React.createElement(MitonTalentChat, { backendUrl, defaultLang, height })
  );
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", mount);
} else {
  mount();
}
