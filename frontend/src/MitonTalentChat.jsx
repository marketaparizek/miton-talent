import React, { useState, useRef, useEffect } from "react";
import LOGO from "./logo.js";

/*
 * Miton talent chat widget.
 *
 * Self-contained: no Tailwind, no external CSS. All styling is inline plus one
 * small scoped <style> block, so the widget can be dropped into any page without
 * clashing with the host site's styles.
 *
 * Layout: an "LLM window", not a messenger. The empty state shows the Miton mark,
 * a big headline and a centered composer with starter suggestions (like Claude).
 * Once the conversation starts, assistant replies render as plain text with the
 * Miton logo, user messages as soft right-aligned bubbles, composer docked below.
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
const BUBBLE_U = "#FDECEC"; // candidate bubble (soft red tint)
const CHIP_BG = "#FDECEC";
const FIELD_BG = "#F7F6F5"; // composer surface
const HAIRLINE = "rgba(22,24,29,0.06)";
const COMPOSER_BORDER = "rgba(22,24,29,0.1)";
const CARD_BORDER = "rgba(22,24,29,0.1)";
const INPUT_BORDER = "rgba(22,24,29,0.12)";
const DASH_BORDER = "rgba(22,24,29,0.18)";
const DOT_SEP = "#C9C6C6";

// No privacy policy page exists on miton.cz yet (checked July 2026). Once legal
// publishes one, put its URL here and the link renders again automatically.
const PRIVACY_URL = "";

const MAX_CV_MB = 8;

const T = {
  cs: {
    heroTitle: "Hledáme chytré lidi.",
    heroSub: "Zvažuješ práci ve startupu? Napiš, co by tě bavilo dělat nebo jaká role tě láká.",
    starters: ["Chci být founder", "Hledám práci ve startupu", "Zajímá mě práce v Mitonu", "Jen si mapuju možnosti"],
    labels: { area: "Oblast", level: "Úroveň", workMode: "Forma", status: "Stav" },
    placeholder: "Napiš, jakou roli hledáš…",
    contactTitle: "Nech nám na sebe kontakt",
    name: "Jméno",
    email: "E-mail",
    linkedin: "LinkedIn",
    notePlaceholder: "Chceš dodat něco dalšího?",
    cvLabel: "Životopis (nepovinné)",
    cvTooBig: `Soubor je moc velký (max ${MAX_CV_MB} MB).`,
    consent: "Souhlasím se zpracováním osobních údajů za účelem oslovení s pracovní nabídkou.",
    privacy: "Zásady zpracování.",
    submit: "Odeslat",
    errFields: "Vyplň prosím e-mail a potvrď souhlas.",
    errConn: "Spojení se serverem se nepovedlo. Zkus to prosím poslat znovu.",
    thanksTitle: "Díky, máme to!",
    thanks: "Rozhodíme sítě napříč naším portfoliem, jestli je něco, co by ti mohlo sedět, a spojíme se s tebou e-mailem. Kdyby cokoliv, napiš naší kolegyni Markétě Pařízek na marketa.parizek@miton.cz.",
  },
  en: {
    heroTitle: "Scouting bright minds.",
    heroSub: "Considering a startup job? Tell us what you would enjoy doing or what role you are drawn to.",
    starters: ["I want to be a founder", "I'm looking for a startup job", "I'm interested in working at Miton", "Just mapping my options"],
    labels: { area: "Area", level: "Level", workMode: "Work", status: "Status" },
    placeholder: "Tell us what role you're looking for…",
    contactTitle: "Leave us your contact",
    name: "Name",
    email: "Email",
    linkedin: "LinkedIn",
    notePlaceholder: "Anything else you'd like to add?",
    cvLabel: "CV (optional)",
    cvTooBig: `File is too large (max ${MAX_CV_MB} MB).`,
    consent: "I agree to the processing of my personal data so that I can be contacted about job opportunities.",
    privacy: "Privacy notice.",
    submit: "Submit",
    errFields: "Please fill in your email and confirm consent.",
    errConn: "Couldn't reach the server. Please send it again.",
    thanksTitle: "Thanks, we've got it!",
    thanks: "We'll cast the net across our portfolio to see if there is something that could fit you, and we'll get in touch by email. If you need anything, write to our colleague Markéta Pařízek at marketa.parizek@miton.cz.",
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
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [profile, setProfile] = useState({ area: "", level: "", workMode: "", status: "" });
  const [summary, setSummary] = useState("");
  const [stage, setStage] = useState("exploring");
  const [contact, setContact] = useState({ name: "", email: "", linkedin: "", note: "" });
  const [cvFile, setCvFile] = useState(null); // { name, type, data }
  const [consent, setConsent] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const t = T[lang];
  const scrollRef = useRef(null);
  const fileRef = useRef(null);
  const taRef = useRef(null);

  const hero = messages.length === 0 && !submitted;

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages, loading, stage, submitted]);

  // Auto-grow the composer textarea up to its max height while typing,
  // and shrink it back when the input is cleared after sending.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 120) + "px";
  }, [input, hero]);

  function switchLang(next) {
    if (next === lang) return;
    setLang(next);
    setMessages([]);
    setProfile({ area: "", level: "", workMode: "", status: "" });
    setSummary("");
    setStage("exploring");
    setContact({ name: "", email: "", linkedin: "", note: "" });
    setCvFile(null);
    setConsent(false);
    setSubmitted(false);
    setInput("");
    setError("");
  }

  async function send(textOverride) {
    const text = (textOverride != null ? textOverride : input).trim();
    if (!text || loading) return;
    const next = [...messages, { role: "user", content: text }];
    setMessages(next);
    setInput("");
    setLoading(true);
    setError("");

    const payloadMessages = next.map((m) => ({ role: m.role, content: m.content }));

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

      if (typeof data.summary === "string" && data.summary.trim()) setSummary(data.summary);

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
          summary,
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

  const collectingContact = stage === "collect_contact" && !submitted;
  const showChips = profileRows.length > 0 && !collectingContact && !submitted && !hero;

  const S = styles;
  const canSend = !loading && input.trim().length > 0;

  const composer = (
    <div style={S.composerPill}>
      <textarea
        ref={taRef}
        className="mtc-ta"
        rows={hero ? 2 : 1}
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={onKey}
        placeholder={t.placeholder}
        style={S.textarea}
        autoFocus={hero}
      />
      <button
        onClick={() => send()}
        disabled={!canSend}
        style={{ ...S.sendBtn, opacity: canSend ? 1 : 0.4, cursor: canSend ? "pointer" : "default" }}
        aria-label="Send"
      >
        <ArrowUp />
      </button>
    </div>
  );

  return (
    <div style={{ ...S.root, height }}>
      <style>{CSS}</style>
      <div style={S.card}>
        {/* Top bar: profile chips left, language toggle right */}
        <div style={{ ...S.topbar, borderBottom: hero ? "none" : `1px solid ${HAIRLINE}` }}>
          <div style={S.chips}>
            {showChips &&
              profileRows.map((r) => (
                <span key={r.key} style={S.chip}>
                  <span style={S.chipLabel}>{r.label}:&nbsp;</span>
                  <span style={S.chipValue}>{r.value}</span>
                </span>
              ))}
          </div>
          <div style={S.langWrap}>
            <button onClick={() => switchLang("cs")} style={{ ...S.langBtn, ...(lang === "cs" ? S.langActive : S.langIdle) }}>CS</button>
            <span style={S.langDot}>·</span>
            <button onClick={() => switchLang("en")} style={{ ...S.langBtn, ...(lang === "en" ? S.langActive : S.langIdle) }}>EN</button>
          </div>
        </div>

        {/* Hero: centered logo, headline, composer, starter chips */}
        {hero && (
          <div style={S.hero}>
            <img src={LOGO} alt="Miton" width={52} height={52} style={S.heroLogo} />
            <div style={S.heroTitle}>{t.heroTitle}</div>
            <p style={S.heroSub}>{t.heroSub}</p>
            <div style={S.heroComposer}>{composer}</div>
            <div style={S.starters}>
              {t.starters.map((s) => (
                <button key={s} style={S.starter} onClick={() => send(s)}>{s}</button>
              ))}
            </div>
            {error && <p style={S.error}>{error}</p>}
          </div>
        )}

        {/* Conversation */}
        {!hero && (
          <div ref={scrollRef} className="mtc-scroll" style={submitted ? S.scrollCenter : S.scroll}>
            {submitted ? (
              <div style={S.thanksWrap}>
                <div style={S.checkCircle}><CheckBig /></div>
                <div style={S.thanksTitle}>{t.thanksTitle}</div>
                <div style={S.thanksText}>{t.thanks}</div>
              </div>
            ) : (
              <>
                {messages.map((m, i) =>
                  m.role === "user" ? (
                    <div key={i} style={S.rowRight}>
                      <div style={S.bubbleUser}>{m.content}</div>
                    </div>
                  ) : (
                    <div key={i} style={S.rowAssistant}>
                      <img src={LOGO} alt="" width={26} height={26} style={S.avatar} />
                      <div style={S.assistantText}>{m.content}</div>
                    </div>
                  )
                )}

                {loading && (
                  <div style={S.rowAssistant}>
                    <img src={LOGO} alt="" width={26} height={26} style={S.avatar} />
                    <div style={S.typing}>
                      <span className="mtc-dot" style={{ ...S.dot, animationDelay: "0s" }} />
                      <span className="mtc-dot" style={{ ...S.dot, animationDelay: ".2s" }} />
                      <span className="mtc-dot" style={{ ...S.dot, animationDelay: ".4s" }} />
                    </div>
                  </div>
                )}

                {collectingContact && (
                  <div style={S.formCard}>
                    <div style={S.formTitle}>{t.contactTitle}</div>
                    <Field label={t.name} value={contact.name} onChange={(v) => setContact({ ...contact, name: v })} placeholder={t.name} />
                    <Field label={t.email} value={contact.email} onChange={(v) => setContact({ ...contact, email: v })} placeholder={t.email} />
                    <Field label={t.linkedin} value={contact.linkedin} onChange={(v) => setContact({ ...contact, linkedin: v })} placeholder={t.linkedin} />
                    <Field label={t.notePlaceholder} value={contact.note} onChange={(v) => setContact({ ...contact, note: v })} placeholder={t.notePlaceholder} />

                    <button onClick={() => fileRef.current && fileRef.current.click()} style={S.cvBox}>
                      <UploadIcon />
                      <span>{cvFile ? cvFile.name : t.cvLabel}</span>
                    </button>
                    <input ref={fileRef} type="file" accept=".pdf,.doc,.docx" style={{ display: "none" }} onChange={pickFile} />

                    <div style={S.consentRow} onClick={() => setConsent(!consent)}>
                      <div style={S.checkbox}>{consent && <CheckSmall />}</div>
                      <div style={S.consentText}>
                        {t.consent}
                        {PRIVACY_URL && (
                          <>
                            {" "}
                            <a href={PRIVACY_URL} target="_blank" rel="noopener noreferrer" className="mtc-a" onClick={(e) => e.stopPropagation()}>{t.privacy}</a>
                          </>
                        )}
                      </div>
                    </div>

                    <button onClick={submitContact} style={S.submitBtn}>{t.submit}</button>
                  </div>
                )}

                {error && <p style={S.error}>{error}</p>}
              </>
            )}
          </div>
        )}

        {/* Docked composer (conversation mode only) */}
        {!hero && !submitted && <div style={S.composerWrap}>{composer}</div>}
      </div>
    </div>
  );
}

function Field({ label, value, onChange, placeholder }) {
  return (
    <input
      className="mtc-in"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      aria-label={label}
      style={styles.input}
    />
  );
}

function ArrowUp() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M12 19V5M12 5l-6 6M12 5l6 6" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function UploadIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" style={{ flex: "none" }}>
      <path d="M12 16V4M12 4l-4 4M12 4l4 4M5 20h14" stroke={MUTED} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function CheckSmall() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none">
      <path d="M5 12l5 5L20 6" stroke={ACCENT} strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function CheckBig() {
  return (
    <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
      <path d="M5 12l5 5L20 6" stroke={ACCENT} strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
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

  topbar: {
    padding: "12px 18px",
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
    minHeight: 22,
  },
  langWrap: { display: "flex", alignItems: "center", gap: 10, fontSize: 12, fontWeight: 600, flex: "none" },
  langBtn: { padding: 0, background: "transparent", border: "none", cursor: "pointer", fontSize: 12, fontWeight: 600, fontFamily: "inherit" },
  langActive: { color: ACCENT },
  langIdle: { color: MUTED, fontWeight: 500 },
  langDot: { color: DOT_SEP },
  chips: { display: "flex", flexWrap: "wrap", gap: 8, minHeight: 1 },
  chip: { display: "inline-flex", fontSize: 12, background: CHIP_BG, padding: "5px 11px", borderRadius: 999 },
  chipLabel: { color: ACCENT, opacity: 0.62 },
  chipValue: { color: ACCENT, fontWeight: 600 },

  // hero (empty state)
  hero: {
    flex: 1,
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    textAlign: "center",
    padding: "12px 24px 40px",
    gap: 0,
  },
  heroLogo: { display: "block", marginBottom: 18 },
  heroTitle: { fontSize: 26, fontWeight: 600, letterSpacing: "-0.02em", lineHeight: 1.2 },
  heroSub: { color: MUTED, fontSize: 14, lineHeight: 1.55, maxWidth: 420, margin: "10px 0 24px" },
  heroComposer: { width: "100%", maxWidth: 560 },
  starters: { display: "flex", flexWrap: "wrap", justifyContent: "center", gap: 8, marginTop: 14 },
  starter: {
    fontSize: 13,
    color: INK,
    background: "#fff",
    border: `1px solid ${INPUT_BORDER}`,
    borderRadius: 999,
    padding: "7px 14px",
    cursor: "pointer",
    fontFamily: "inherit",
  },

  // conversation
  scroll: { flex: 1, overflowY: "auto", padding: "20px 22px", display: "flex", flexDirection: "column", gap: 18, maxWidth: 640, width: "100%", margin: "0 auto", boxSizing: "border-box" },
  scrollCenter: { flex: 1, overflowY: "auto", padding: "22px", display: "flex", flexDirection: "column" },
  rowAssistant: { display: "flex", justifyContent: "flex-start", alignItems: "flex-start", gap: 10 },
  rowRight: { display: "flex", justifyContent: "flex-end" },
  avatar: { flex: "0 0 auto", display: "block", marginTop: 1 },
  assistantText: { fontSize: 14.5, lineHeight: 1.6, whiteSpace: "pre-wrap", color: INK, paddingTop: 2 },
  bubbleUser: {
    maxWidth: "78%", padding: "10px 14px", borderRadius: 16, borderBottomRightRadius: 6,
    background: BUBBLE_U, color: INK, fontSize: 14, lineHeight: 1.5, whiteSpace: "pre-wrap",
  },
  typing: { padding: "8px 2px", display: "flex", gap: 5, alignItems: "center" },
  dot: { width: 7, height: 7, borderRadius: "50%", background: MUTED, display: "inline-block" },

  // contact card
  formCard: { background: "#fff", border: `1px solid ${CARD_BORDER}`, borderRadius: 14, padding: 16, display: "flex", flexDirection: "column", gap: 11 },
  formTitle: { fontSize: 14, fontWeight: 600 },
  input: { width: "100%", boxSizing: "border-box", fontSize: 13, padding: "9px 12px", borderRadius: 12, border: `1px solid ${INPUT_BORDER}`, outline: "none", fontFamily: "inherit", color: INK, background: "#fff" },
  cvBox: { display: "flex", alignItems: "center", gap: 8, width: "100%", boxSizing: "border-box", border: `1px dashed ${DASH_BORDER}`, borderRadius: 12, padding: "9px 12px", color: MUTED, fontSize: 13, background: "transparent", cursor: "pointer", fontFamily: "inherit", textAlign: "left" },
  consentRow: { display: "flex", alignItems: "flex-start", gap: 9, cursor: "pointer" },
  checkbox: { width: 16, height: 16, border: `1.5px solid ${ACCENT}`, borderRadius: 4, flex: "none", marginTop: 1, display: "flex", alignItems: "center", justifyContent: "center", background: "#fff" },
  consentText: { fontSize: 11, color: MUTED, lineHeight: 1.4 },
  submitBtn: { width: "100%", background: ACCENT, color: "#fff", borderRadius: 12, padding: 11, textAlign: "center", fontSize: 14, fontWeight: 500, border: "none", cursor: "pointer", fontFamily: "inherit" },

  // thank you
  thanksWrap: { flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", textAlign: "center", gap: 16, padding: "24px 8px" },
  checkCircle: { width: 52, height: 52, borderRadius: "50%", background: BUBBLE_U, display: "flex", alignItems: "center", justifyContent: "center" },
  thanksTitle: { fontSize: 18, fontWeight: 600 },
  thanksText: { fontSize: 14, color: MUTED, lineHeight: 1.55, maxWidth: 360 },

  error: { fontSize: 12, color: ACCENT, margin: "10px 0 0" },

  // composer
  composerWrap: { padding: "14px 18px 18px", maxWidth: 640, width: "100%", margin: "0 auto", boxSizing: "border-box" },
  composerPill: {
    display: "flex",
    alignItems: "flex-end",
    gap: 10,
    background: FIELD_BG,
    border: `1px solid ${COMPOSER_BORDER}`,
    borderRadius: 18,
    padding: "12px 12px 12px 16px",
    boxShadow: "0 2px 10px -6px rgba(22,24,29,0.12)",
  },
  textarea: {
    flex: 1, resize: "none", fontSize: 14, lineHeight: 1.5, border: "none", background: "transparent",
    outline: "none", maxHeight: 120, fontFamily: "inherit", color: INK, padding: 0, margin: 0,
  },
  sendBtn: {
    flex: "0 0 auto", width: 34, height: 34, borderRadius: "50%", background: ACCENT, border: "none",
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
