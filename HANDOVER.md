# Handover: embedding the widget

For the web developer. The chat is a self-contained widget. You do not run the
backend. You build one JavaScript file and drop it on the career page, pointing it
at the backend URL Miton gives you.

## What you get

- A React widget in `frontend/src/` with no external CSS dependency (no Tailwind,
  no global styles). It will not clash with the site's styling.
- A build that outputs a single file `dist/miton-talent-chat.js` with React bundled in.

## Build

```
cd frontend
npm install
npm run build
```

Output: `frontend/dist/miton-talent-chat.js`.

## Embed on the page

Add a container div and the script. The widget mounts into the div and fills it.

```html
<div
  id="miton-talent-chat"
  data-backend="https://BACKEND-URL"   <!-- the deployed backend URL from Miton -->
  data-lang="cs"                       <!-- "cs" or "en" -->
  style="max-width:640px;height:600px;margin:0 auto"
></div>
<script src="/path/to/miton-talent-chat.js"></script>
```

Notes:
- `data-backend` is required (or set `VITE_BACKEND_URL` before building).
- Size and position come from the div. Set the height and width you want; the
  widget fills the container.
- Only one widget per page (the id must be unique).

## If the site already uses React

You can skip the standalone build and import the component directly:

```jsx
import MitonTalentChat from "./MitonTalentChat.jsx";

<MitonTalentChat backendUrl="https://BACKEND-URL" defaultLang="cs" height="600px" />
```

## Placement on the career page

Hero section as the dominant element. On desktop: headline and reasons on the
left, the chat on the right. On mobile: full width below the headline. Any
"Chci probrat možnosti" buttons further down should scroll up to the chat.

## What the widget does

- Talks with the candidate, shows extracted profile chips as the chat goes.
- When it makes sense, shows a short contact form (name, email, LinkedIn, note,
  optional CV) with a GDPR consent checkbox.
- On submit it posts to the backend, which writes to Notion and emails the CV.

Nothing else is needed on the site side. Questions: marketa.parizek@miton.cz
