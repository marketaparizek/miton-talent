import React, { useState, useRef, useEffect } from "react";

/*
 * Miton talent chat widget.
 *
 * Self-contained: no Tailwind, no external CSS. All styling is inline plus one
 * small scoped <style> block, so the widget can be dropped into any page without
 * clashing with the host site's styles. Visual design matches the Miton mockup.
 *
 * Props:
 *   backendUrl  - base URL of the FastAPI backend (default http://localhost:8000)
 *   defaultLang - "cs" or "en"
 *   height      - CSS height of the widget (e.g. "600px" or "100%")
 *
 * NOTE: PRIVACY_URL and the legal entity in the consent text still need to be
 * confirmed before go-live (legal review recommended).
 */

const ACCENT = "#E32726";
const INK = "#16181D";
const MUTED = "#6E7178";
const CARD = "#FFFFFF";
const BUBBLE_A = "#F0EEEE"; // assistant bubble
const BUBBLE_U = "#FDECEC"; // candidate bubble (soft red tint)
const FIELD_BG = "#F4F2F2"; // composer / input surface
const HAIRLINE = "rgba(22,24,29,0.06)";
const BORDER = "rgba(22,24,29,0.08)";
const DOT_SEP = "#C9C6C6";

const PRIVACY_URL = "https://www.miton.cz/zasady-zpracovani-osobnich-udaju"; // TODO: confirm final URL

const MAX_CV_MB = 8;

const GREETING = {
  cs: "Ahoj! V Mitonu se rádi spojíme se zvědavými a chytrými lidmi a o možnostech si moc rádi popovídáme. Co by tě bavilo dělat nebo jaká role tě láká?",
  en: "Hi! At Miton we love connecting with curious, smart people, and we're always happy to talk through the options. What would you enjoy doing, or what role are you drawn to?",
};

const T = {
  cs: {
    subtitle: "Pojď si s námi popovídat o možnostech v našem portfoliu.",
    labels: { area: "Oblast", level: "Úroveň", workMode: "Forma", status: "Stav" },
    placeholder: "Napiš, co tě zajímá…",
    contactTitle: "Nech nám na sebe kontakt a spojíme se s tebou.",
    name: "Jméno",
    email: "E-mail",
    linkedin: "LinkedIn",
    noteLabel: "Chceš dodat něco dalšího?",
    notePlaceholder: "Nepovinné",
    cvLabel: "Životopis (nepovinné)",
    uploadBtn: "Nahrát soubor",
    cvTooBig: `Soubor je moc velký (max ${MAX_CV_MB} MB).`,
    consent: "Souhlasím se zpracováním osobních údajů za účelem oslovení s pracovní nabídkou.",
    privacy: "Více v zásadách zpracování osobních údajů.",
    submit: "Odeslat",
    errFields: "Vyplň prosím e-mail a potvrď souhlas.",
    errConn: "Spojení se serverem se nepovedlo. Zkus to prosím poslat znovu.",
    thanks: (e) => `Díky, máme to! Projdeme si to a spojíme se s tebou na ${e}. Když něco sedne, domluvíme krátký call. A i kdybychom teď zrovna nic neměli, dáme ti vědět.`,
  },
  en: {
    subtitle: "Let's talk through the opportunities across our portfolio.",
    labels: { area: "Area", level: "Level", workMode: "Work", status: "Status" },
    placeholder: "Type your message…",
    contactTitle: "Leave us your contact and we'll get in touch.",
    name: "Name",
    email: "Email",
    linkedin: "LinkedIn",
    noteLabel: "Anything else you'd like to add?",
    notePlaceholder: "Optional",
    cvLabel: "CV (optional)",
    uploadBtn: "Upload file",
    cvTooBig: `File is too large (max ${MAX_CV_MB} MB).`,
    consent: "I agree to the processing of my personal data so that I can be contacted about job opportunities.",
    privacy: "Read our privacy notice.",
    submit: "Submit",
    errFields: "Please fill in your email and confirm consent.",
    errConn: "Couldn't reach the server. Please send it again.",
    thanks: (e) => `Thanks, we've got it! We'll review it and get back to you at ${e}. If something fits, we'll set up a short call. And even if we have nothing right now, we'll let you know.`,
  },
};

function asText(v) {
  if (Array.isArray(v)) return v.join(", ");
  return v || "";
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || "");
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

export default function MitonTalentChat({
  backendUrl = "http://localhost:8000",
  defaultLang = "cs",
  height = "600px",
}) {
  const initialLang = defaultLang === "en" ? "en" : "cs";
  const [lang, setLang] = useState(initialLang);
  const [messages, setMessages] = useState([{ role: "assistant", content: GREETING[initialLang] }]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [profile, setProfile] = useState({ area: "", level: "", workMode: "", status: "" });
  const [stage, setStage] = useState("exploring");
  const [contact, setContact] = useState({ name: "", email: "", linkedin: "", note: "" });
  const [cvFile, setCvFile] = useState(null); // { name, type, data }
  const [consent, setConsent] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const t = T[lang];
  const scrollRef = useRef(null);
  const fileRef = useRef(null);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages, loading, stage, submitted]);

  function switchLang(next) {
    if (next === lang) return;
    setLang(next);
    setMessages([{ role: "assistant", content: GREETING[next] }]);
    setProfile({ area: "", level: "", workMode: "", status: "" });
    setStage("exploring");
    setContact({ name: "", email: "", linkedin: "", note: "" });
    setCvFile(null);
    setConsent(false);
    setSubmitted(false);
    setInput("");
    setError("");
  }

  async function send() {
    const text = input.trim();
    if (!text || loading) return;
    const next = [...messages, { role: "user", content: text }];
    setMessages(next);
    setInput("");
    setLoading(true);
    setError("");

    const payloadMessages = next.slice(1).map((m) => ({ role: m.role, content: m.content }));

    try {
      const res = await fetch(`${backendUrl}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: payloadMessages, lang }),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();

      setMessages((m) => [...m, { role: "assistant", content: data.reply || "..." }]);

      const p = data.profile || {};
      setProfile((prev) => ({
        area: asText(p.area) || prev.area,
        level: asText(p.level) || prev.level,
        workMode: asText(p.workMode) || prev.workMode,
        status: asText(p.status) || prev.status,
      }));

      if (data.stage === "collect_contact") setStage("collect_contact");
    } catch {
      setMessages((m) => m.slice(0, -1));
      setInput(text);
      setError(t.errConn);
    }
    setLoading(false);
  }

  function onKey(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  async function pickFile(e) {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    if (file.size > MAX_CV_MB * 1024 * 1024) {
      setError(t.cvTooBig);
      e.target.value = "";
      return;
    }
    setError("");
    try {
      const data = await readFileAsBase64(file);
      setCvFile({ name: file.name, type: file.type || "application/octet-stream", data });
    } catch {
      setError(t.errConn);
    }
  }

  async function submitContact() {
    if (!contact.email.trim() || !consent) {
      setError(t.errFields);
      return;
    }
    setError("");
    try {
      await fetch(`${backendUrl}/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          lang,
          profile,
          contact,
          consent,
          consent_text: t.consent,
          cv: cvFile,
        }),
      });
    } catch {
      // even if saving fails, we still thank the visitor; the server keeps a backup
    }
    setSubmitted(true);
  }

  const profileRows = [
    { key: "area", label: t.labels.area, value: profile.area },
    { key: "level", label: t.labels.level, value: profile.level },
    { key: "workMode", label: t.labels.workMode, value: profile.workMode },
    { key: "status", label: t.labels.status, value: profile.status },
  ].filter((r) => r.value);

  const S = styles;
  const canSend = !loading && input.trim().length > 0;

  return (
    <div style={{ ...S.root, height }}>
      <style>{CSS}</style>
      <div style={S.card}>
        {/* Header */}
        <div style={S.header}>
          <div style={S.langWrap}>
            <button onClick={() => switchLang("cs")} style={{ ...S.langBtn, ...(lang === "cs" ? S.langActive : S.langIdle) }}>CS</button>
            <span style={S.langDot}>·</span>
            <button onClick={() => switchLang("en")} style={{ ...S.langBtn, ...(lang === "en" ? S.langActive : S.langIdle) }}>EN</button>
          </div>
          <p style={S.subtitle}>{t.subtitle}</p>

          {profileRows.length > 0 && (
            <div style={S.chips}>
              {profileRows.map((r) => (
                <span key={r.key} style={S.chip}>
                  <span style={S.chipLabel}>{r.label}:&nbsp;</span>
                  <span style={S.chipValue}>{r.value}</span>
                </span>
              ))}
            </div>
          )}
        </div>

        {/* Conversation */}
        <div ref={scrollRef} className="mtc-scroll" style={S.scroll}>
          {messages.map((m, i) => (
            <div key={i} style={m.role === "user" ? S.rowRight : S.rowLeft}>
              {m.role === "assistant" && <div style={S.avatar}>m</div>}
              <div style={m.role === "user" ? S.bubbleUser : S.bubbleAssistant}>{m.content}</div>
            </div>
          ))}

          {loading && (
            <div style={S.rowLeft}>
              <div style={S.avatar}>m</div>
              <div style={S.typing}>
                <span className="mtc-dot" style={{ ...S.dot, animationDelay: "0s" }} />
                <span className="mtc-dot" style={{ ...S.dot, animationDelay: ".2s" }} />
                <span className="mtc-dot" style={{ ...S.dot, animationDelay: ".4s" }} />
              </div>
            </div>
          )}

          {stage === "collect_contact" && !submitted && (
            <div style={S.form}>
              <p style={S.formTitle}>{t.contactTitle}</p>
              <Field label={t.name} value={contact.name} onChange={(v) => setContact({ ...contact, name: v })} placeholder={lang === "cs" ? "Jan Novák" : "Jane Doe"} />
              <Field label={t.email} value={contact.email} onChange={(v) => setContact({ ...contact, email: v })} placeholder="jan@email.cz" />
              <Field label={t.linkedin} value={contact.linkedin} onChange={(v) => setContact({ ...contact, linkedin: v })} placeholder="https://linkedin.com/in/..." />
              <Field label={t.noteLabel} value={contact.note} onChange={(v) => setContact({ ...contact, note: v })} placeholder={t.notePlaceholder} />

              <div>
                <label style={S.fieldLabel}>{t.cvLabel}</label>
                <button onClick={() => fileRef.current && fileRef.current.click()} style={S.uploadBtn}>
                  {cvFile ? cvFile.name : t.uploadBtn}
                </button>
                <input ref={fileRef} type="file" accept=".pdf,.doc,.docx" style={{ display: "none" }} onChange={pickFile} />
              </div>

              <label style={S.consentRow}>
                <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} style={{ marginTop: 2 }} />
                <span>
                  {t.consent}{" "}
                  <a href={PRIVACY_URL} target="_blank" rel="noopener noreferrer" className="mtc-a">{t.privacy}</a>
                </span>
              </label>

              <button onClick={submitContact} style={S.submitBtn}>{t.submit}</button>
            </div>
          )}

          {submitted && <div style={S.thanks}>{t.thanks(contact.email)}</div>}

          {error && <p style={S.error}>{error}</p>}
        </div>

        {/* Composer */}
        {!submitted && (
          <div style={S.composerWrap}>
            <div style={S.composerPill}>
              <textarea
                className="mtc-ta"
                rows={1}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={onKey}
                placeholder={t.placeholder}
                style={S.textarea}
              />
              <button
                onClick={send}
                disabled={!canSend}
                style={{ ...S.sendBtn, opacity: canSend ? 1 : 0.4, cursor: canSend ? "pointer" : "default" }}
                aria-label="Send"
              >
                <ArrowUp />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Field({ label, value, onChange, placeholder }) {
  return (
    <div>
      <label style={styles.fieldLabel}>{label}</label>
      <input className="mtc-in" value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} style={styles.input} />
    </div>
  );
}

function ArrowUp() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M12 19V5M12 5l-6 6M12 5l6 6" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

const styles = {
  root: {
    width: "100%",
    fontFamily: "'Inter', system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
    color: INK,
    display: "flex",
  },
  card: {
    width: "100%",
    background: CARD,
    borderRadius: 16,
    border: `1px solid rgba(22,24,29,0.05)`,
    boxShadow: "0 12px 32px -18px rgba(22,24,29,0.2)",
    overflow: "hidden",
    display: "flex",
    flexDirection: "column",
  },
  header: { padding: "20px 22px 14px", borderBottom: `1px solid ${HAIRLINE}` },
  langWrap: { display: "flex", alignItems: "center", gap: 10, fontSize: 12, fontWeight: 600 },
  langBtn: { padding: 0, background: "transparent", border: "none", cursor: "pointer", fontSize: 12, fontWeight: 600, fontFamily: "inherit" },
  langActive: { color: ACCENT },
  langIdle: { color: MUTED, fontWeight: 500 },
  langDot: { color: DOT_SEP },
  subtitle: { color: MUTED, fontSize: 13, lineHeight: 1.4, margin: "10px 0 0" },
  chips: { marginTop: 12, display: "flex", flexWrap: "wrap", gap: 8 },
  chip: { display: "inline-flex", fontSize: 12, background: BUBBLE_U, padding: "5px 11px", borderRadius: 999 },
  chipLabel: { color: ACCENT, opacity: 0.62 },
  chipValue: { color: ACCENT, fontWeight: 600 },
  scroll: { flex: 1, overflowY: "auto", padding: "20px 22px", display: "flex", flexDirection: "column", gap: 14 },
  rowLeft: { display: "flex", justifyContent: "flex-start", alignItems: "flex-end", gap: 10 },
  rowRight: { display: "flex", justifyContent: "flex-end" },
  avatar: {
    flex: "0 0 auto", width: 28, height: 28, borderRadius: "50%", background: ACCENT, color: "#fff",
    fontSize: 13, fontWeight: 600, display: "flex", alignItems: "center", justifyContent: "center", lineHeight: 1,
  },
  bubbleAssistant: {
    maxWidth: "82%", padding: "12px 14px", borderRadius: 16, borderBottomLeftRadius: 6,
    background: BUBBLE_A, color: INK, fontSize: 14, lineHeight: 1.5, whiteSpace: "pre-wrap",
  },
  bubbleUser: {
    maxWidth: "82%", padding: "12px 14px", borderRadius: 16, borderBottomRightRadius: 6,
    background: BUBBLE_U, color: INK, fontSize: 14, lineHeight: 1.5, whiteSpace: "pre-wrap",
  },
  typing: { background: BUBBLE_A, borderRadius: 16, borderBottomLeftRadius: 6, padding: "15px 16px", display: "flex", gap: 5, alignItems: "center" },
  dot: { width: 7, height: 7, borderRadius: "50%", background: MUTED, display: "inline-block" },
  form: { marginTop: 4, border: `1px solid ${BORDER}`, borderRadius: 14, padding: 16, display: "flex", flexDirection: "column", gap: 12 },
  formTitle: { fontSize: 14, fontWeight: 600, margin: 0 },
  fieldLabel: { fontSize: 12, color: MUTED, display: "block", marginBottom: 4 },
  input: { width: "100%", boxSizing: "border-box", fontSize: 14, padding: "9px 12px", borderRadius: 10, border: `1px solid ${BORDER}`, outline: "none", fontFamily: "inherit", color: INK },
  uploadBtn: { fontSize: 14, padding: "9px 14px", borderRadius: 10, border: `1px solid ${BORDER}`, background: "#fff", cursor: "pointer", fontFamily: "inherit", color: INK },
  consentRow: { display: "flex", alignItems: "flex-start", gap: 8, fontSize: 12, color: MUTED, lineHeight: 1.45, cursor: "pointer" },
  submitBtn: { width: "100%", fontSize: 14, fontWeight: 600, color: "#fff", padding: "12px", borderRadius: 10, border: "none", background: ACCENT, cursor: "pointer", fontFamily: "inherit" },
  thanks: { marginTop: 4, border: `1px solid ${BORDER}`, borderRadius: 14, padding: 16, fontSize: 14, lineHeight: 1.55 },
  error: { fontSize: 12, color: ACCENT, margin: 0 },
  composerWrap: { padding: "14px 18px 18px", borderTop: `1px solid ${HAIRLINE}` },
  composerPill: {
    display: "flex", alignItems: "flex-end", gap: 10,
    background: FIELD_BG, border: `1px solid ${BORDER}`, borderRadius: 12, padding: "11px 11px 11px 14px",
  },
  textarea: {
    flex: 1, resize: "none", fontSize: 14, lineHeight: 1.5, border: "none", background: "transparent",
    outline: "none", maxHeight: 120, fontFamily: "inherit", color: INK, padding: 0, margin: 0,
  },
  sendBtn: {
    flex: "0 0 auto", width: 36, height: 36, borderRadius: "50%", background: ACCENT, border: "none",
    display: "flex", alignItems: "center", justifyContent: "center",
  },
};

const CSS = `
.mtc-scroll::-webkit-scrollbar { width: 8px; }
.mtc-scroll::-webkit-scrollbar-thumb { background: rgba(0,0,0,0.12); border-radius: 8px; }
.mtc-ta::placeholder, .mtc-in::placeholder { color: #9A9DA2; }
.mtc-in:focus { border-color: rgba(22,24,29,0.28); }
.mtc-a { color: ${ACCENT}; text-decoration: underline; }
@keyframes mtBlink { 0%, 60%, 100% { opacity: .25; transform: translateY(0); } 30% { opacity: .9; transform: translateY(-2px); } }
.mtc-dot { animation: mtBlink 1.4s infinite; }
@media (prefers-reduced-motion: reduce) { .mtc-dot { animation: none; opacity: .55; } }
`;
