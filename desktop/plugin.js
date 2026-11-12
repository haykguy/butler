import { ROUTES_AREA, SIDEBAR_NAV_AREA, host, useValue } from '@hermes/plugin-sdk';
import { createElement as h, useEffect, useRef, useState } from 'react';

async function createProjectAgents(files, folder, text) {
  if (!files || !folder || !folder.id || typeof text !== 'string' || !text.trim()) throw new Error('Choose a project folder and enter instructions.');
  const result = await files.writeText(folder.id, 'AGENTS.md', text, { exclusive: true });
  if (!result || !result.ok) throw new Error((result && result.message) || 'Could not create AGENTS.md.');
  const check = await files.readText(folder.id, 'AGENTS.md');
  if (!check || !check.ok || check.text !== text) throw new Error('Created file could not be verified; inspect it before retrying.');
}

const PROJECT_AGENTS_SUGGESTION = '# Project working principles\n\n- Inspect existing project instructions before making changes.\n- Preserve unrelated changes and verify the requested outcome with appropriate tests.\n- Report results and blockers accurately.\n';

const TONES = ['Gentle', 'Supportive', 'Balanced', 'Direct', 'Strict'];
const PERSISTENCE_LEVELS = ['Minimal', 'Occasional', 'Regular', 'Persistent'];

const TONE_DESCRIPTIONS = {
  Gentle: 'Warm, low-pressure check-ins that give you breathing room and easy outs.',
  Supportive: 'Encouraging, steady coaching that helps unblock friction before nudging.',
  Balanced: 'Clear, attentive human assistant — grounded, concise, and matter-of-factly helpful.',
  Direct: 'Crisp, zero-fluff accountability focused straight on the next concrete action.',
  Strict: 'Firm, no-excuses follow-through that calls out drift and protects priorities.',
};

const PERSISTENCE_DETAILS = {
  Minimal: {
    badge: 'Light touch',
    nonNegotiable: 'Butler decides when to check in, and does so sparingly. It lets go early.',
    commitment: 'Butler follows up rarely, only when it clearly helps.',
  },
  Occasional: {
    badge: 'When something is open',
    nonNegotiable: 'Butler picks the timing and follows up when an item is genuinely still open, then steps back if you are busy.',
    commitment: 'Butler chooses when to come back based on how you respond.',
  },
  Regular: {
    badge: 'Steady follow-through',
    nonNegotiable: 'Butler keeps steady follow-through, adjusting spacing to how you respond and what is at stake.',
    commitment: 'Butler decides each next touch itself and keeps at it while the item is open.',
  },
  Persistent: {
    badge: 'Persistent · model-chosen timing',
    nonNegotiable: 'Butler maintains pressure on open items, but the model chooses each next touch from context—there are no preset reminder intervals. It may pause or stop if further nudges would not help (subject to quiet hours and the unanswered-nudge safety gate).',
    commitment: 'Butler decides whether and when to push again and preps next steps until resolved. It picks the timing, not a fixed schedule.',
  },
};

function formatDateTime(iso, tz) {
  if (!iso) return 'No next time selected';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const opts = {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  };
  if (tz) {
    try {
      return new Intl.DateTimeFormat(undefined, { ...opts, timeZone: tz }).format(d);
    } catch (_err) {
      // Fallback to browser timezone if custom tz is unavailable in Intl
    }
  }
  return new Intl.DateTimeFormat(undefined, opts).format(d);
}

function formatTimeOnly(iso, tz) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const opts = { hour: 'numeric', minute: '2-digit' };
  if (tz) {
    try {
      return new Intl.DateTimeFormat(undefined, { ...opts, timeZone: tz }).format(d);
    } catch (_err) {
      // ignore
    }
  }
  return new Intl.DateTimeFormat(undefined, opts).format(d);
}

function toDatetimeLocalValue(iso, tz) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: tz || undefined,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).formatToParts(d);
    const get = (t) => (parts.find((p) => p.type === t) || {}).value || '';
    const hh = get('hour') === '24' ? '00' : get('hour');
    if (get('year') && get('month') && get('day') && hh && get('minute')) {
      return `${get('year')}-${get('month')}-${get('day')}T${hh}:${get('minute')}`;
    }
  } catch (_err) {
    // Fallback below
  }
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function isSnoozedFuture(item) {
  if (!item || item.status !== 'active' || !item.due_at) return false;
  const dueMs = new Date(item.due_at).getTime();
  return Number.isFinite(dueMs) && dueMs > Date.now() + 20 * 60 * 1000 && (item.stage === -1 || item.nudge_count > 0);
}

const styles = {
  page: {
    padding: '28px 32px 56px',
    maxWidth: 1040,
    margin: '0 auto',
    color: 'var(--foreground, #1f2421)',
    fontFamily: 'inherit',
    lineHeight: 1.5,
  },
  headerRow: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'flex-start',
    gap: 16,
    flexWrap: 'wrap',
    marginBottom: 20,
  },
  titleGroup: {
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
  },
  h1: {
    margin: 0,
    fontSize: 24,
    fontWeight: 650,
    letterSpacing: '-0.02em',
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    flexWrap: 'wrap',
  },
  subtitle: {
    margin: 0,
    fontSize: 13.5,
    color: 'var(--muted-foreground, #6e776f)',
  },
  headerActions: {
    display: 'flex',
    gap: 8,
    alignItems: 'center',
    flexWrap: 'wrap',
  },
  card: {
    background: 'var(--card, rgba(127, 127, 127, 0.04))',
    border: '1px solid var(--border, rgba(127, 127, 127, 0.2))',
    borderRadius: 14,
    padding: '18px 20px',
    marginBottom: 16,
  },
  subtleCard: {
    background: 'color-mix(in srgb, var(--card, #888) 70%, transparent)',
    border: '1px solid var(--border, rgba(127, 127, 127, 0.18))',
    borderRadius: 12,
    padding: '13px 15px',
  },
  cardHeader: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    gap: 12,
    flexWrap: 'wrap',
    marginBottom: 12,
  },
  cardTitle: {
    margin: 0,
    fontSize: 15.5,
    fontWeight: 620,
    letterSpacing: '-0.01em',
  },
  cardHint: {
    margin: '2px 0 0',
    fontSize: 12.5,
    color: 'var(--muted-foreground, #6e776f)',
  },
  tabBar: {
    display: 'inline-flex',
    gap: 4,
    padding: 4,
    borderRadius: 11,
    background: 'color-mix(in srgb, var(--foreground, #888) 6%, transparent)',
    border: '1px solid var(--border, rgba(127, 127, 127, 0.18))',
    marginBottom: 20,
    flexWrap: 'wrap',
  },
  tabBtn: (active) => ({
    border: 'none',
    borderRadius: 8,
    padding: '7px 14px',
    fontSize: 13,
    fontWeight: active ? 600 : 500,
    cursor: 'pointer',
    background: active ? 'var(--card, rgba(255,255,255,0.9))' : 'transparent',
    color: active ? 'var(--foreground, #1f2421)' : 'var(--muted-foreground, #6e776f)',
    boxShadow: active ? '0 1px 3px rgba(0,0,0,0.08)' : 'none',
    display: 'inline-flex',
    alignItems: 'center',
    gap: 7,
  }),
  btn: (variant = 'secondary', disabled = false) => {
    const base = {
      borderRadius: 9,
      padding: '7px 13px',
      fontSize: 13,
      fontWeight: 560,
      cursor: disabled ? 'not-allowed' : 'pointer',
      opacity: disabled ? 0.55 : 1,
      display: 'inline-flex',
      alignItems: 'center',
      gap: 6,
      transition: 'opacity 120ms ease',
    };
    if (variant === 'primary') {
      return {
        ...base,
        background: 'var(--foreground, #252a24)',
        color: 'var(--card, #f6f7f3)',
        border: '1px solid var(--foreground, #252a24)',
      };
    }
    if (variant === 'sage') {
      return {
        ...base,
        background: 'color-mix(in srgb, #4f8a5b 18%, var(--card, transparent))',
        color: 'var(--foreground, #1f2421)',
        border: '1px solid color-mix(in srgb, #4f8a5b 42%, var(--border, #666))',
      };
    }
    if (variant === 'danger') {
      return {
        ...base,
        background: 'color-mix(in srgb, #d9534f 12%, transparent)',
        color: 'color-mix(in srgb, #d9534f 85%, var(--foreground, #c33))',
        border: '1px solid color-mix(in srgb, #d9534f 35%, var(--border, #666))',
      };
    }
    if (variant === 'mini') {
      return {
        ...base,
        padding: '3px 8px',
        fontSize: 11.5,
        borderRadius: 7,
        background: 'color-mix(in srgb, var(--foreground, #888) 5%, transparent)',
        color: 'var(--foreground, #1f2421)',
        border: '1px solid var(--border, rgba(127, 127, 127, 0.24))',
      };
    }
    return {
      ...base,
      background: 'var(--card, transparent)',
      color: 'var(--foreground, #1f2421)',
      border: '1px solid var(--border, rgba(127, 127, 127, 0.28))',
    };
  },

  input: {
    width: '100%',
    padding: '8px 11px',
    borderRadius: 9,
    border: '1px solid var(--border, rgba(127, 127, 127, 0.3))',
    background: 'color-mix(in srgb, var(--card, transparent) 85%, transparent)',
    color: 'var(--foreground, inherit)',
    fontSize: 13.5,
    boxSizing: 'border-box',
  },
  modalBackdrop: {
    position: 'fixed',
    inset: 0,
    background: 'rgba(10, 14, 12, 0.55)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 20,
    zIndex: 999,
  },
  modalPanel: {
    width: '100%',
    maxWidth: 580,
    maxHeight: '88vh',
    overflowY: 'auto',
    background: 'var(--card, #1e211f)',
    color: 'var(--foreground, #f2f4f0)',
    border: '1px solid var(--border, rgba(127, 127, 127, 0.32))',
    borderRadius: 16,
    padding: '22px 24px',
    boxShadow: '0 18px 48px rgba(0, 0, 0, 0.32)',
  },
};

const OVC_CSS = `
.ovc{--ink:#382b20;--muted:#8d7a64;--gold:#a77e49;--line:#e2d4be;--paper:#f6f1e8;--serif:'Cormorant Garamond','Iowan Old Style',Georgia,serif;
  --foreground:#382b20;--muted-foreground:#8d7a64;--border:#e2d4be;--card:#fffcf6;--accent:#a77e49;
  color:var(--ink);font-family:Montserrat,'Avenir Next','Helvetica Neue',sans-serif;min-height:100%;background:#cec5b8;padding:18px 22px 40px;box-sizing:border-box}
.ovc *{box-sizing:border-box}
.ovc-app{max-width:1280px;margin:0 auto;background:var(--paper);box-shadow:0 22px 65px #3e30242c;overflow:hidden}
.ovc button{font-family:inherit}
.ovc-top{height:64px;background:#382a20;color:#fcf5e9;display:flex;align-items:center;justify-content:space-between;padding:0 36px;border-bottom:2px solid #b49464}
.ovc-brand{display:flex;align-items:center;gap:11px;font:700 16px var(--serif);letter-spacing:.21em}
.ovc-brand-mark{font:500 25px var(--serif);letter-spacing:0;color:#d8bc86}
.ovc-right{display:flex;align-items:center;gap:22px;font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:#f2e4cf}
.ovc-dot{width:6px;height:6px;border-radius:50%;background:#d8bc86;display:inline-block;margin-right:7px;vertical-align:1px}
.ovc-status.amber .ovc-dot{background:#e0a24a}.ovc-status.muted .ovc-dot{background:#8d7a64}.ovc-status.ok .ovc-dot{background:#9fd08c}
.ovc-toplink{background:none;border:0;color:#f2e4cf;font-size:10px;letter-spacing:.1em;text-transform:uppercase;cursor:pointer}
.ovc-toplink:hover{color:#d8bc86}
.ovc-menu{min-height:52px;background:#fdf9f1;display:flex;justify-content:center;flex-wrap:wrap;gap:34px;border-bottom:1px solid #e5d8c2}
.ovc-menu button{background:none;border:0;font-size:11px;font-weight:600;color:#83705b;position:relative;cursor:pointer;padding:0 2px}
.ovc-menu button:hover{color:#3f3022}
.ovc-menu button.active{color:#3f3022}
.ovc-menu button.active:after{content:'';position:absolute;bottom:0;left:0;right:0;height:2px;background:#a7814e}
.ovc-hero{height:380px;position:relative;display:flex;align-items:flex-end;justify-content:center;text-align:center;background-color:#dec6a3;background-position:center 38%;background-size:cover;background-repeat:no-repeat}
.ovc-hero:before{content:'';position:absolute;inset:0;background:linear-gradient(to bottom,rgba(255,244,221,0) 20%,rgba(252,243,225,.09) 44%,rgba(250,242,227,.87) 78%,#f6f1e8 100%)}
.ovc-hero-content{position:relative;z-index:1;padding:0 20px 18px}
.ovc-eyebrow{font-size:9.5px;letter-spacing:.2em;text-transform:uppercase;color:#93754d;font-weight:600}
.ovc-hero h1{font:500 52px/1.05 var(--serif);margin:10px 0 6px;color:#382b20}
.ovc-hero p{font:italic 20px var(--serif);color:#7c674f;margin:0}
.ovc-flourish{color:#a7814e;margin-top:10px;font:16px var(--serif);letter-spacing:.2em}
.ovc-quick{display:flex;justify-content:center;flex-wrap:wrap;gap:12px;padding:20px 30px 6px}
.ovc-btn{display:inline-flex;align-items:center;gap:6px;border:1px solid #c8ac7d;background:#fffcf6;color:#4a3826;font-size:10.5px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;padding:12px 20px;cursor:pointer;transition:background .15s,border-color .15s}
.ovc-btn:hover:not(:disabled){background:#f4e9d6;border-color:#a7814e}
.ovc-btn:disabled{opacity:.55;cursor:default}
.ovc-btn.primary{background:#382a20;color:#fcf5e9;border-color:#382a20}
.ovc-btn.primary:hover:not(:disabled){background:#4a3829}
.ovc-btn.small{padding:8px 13px;font-size:9.5px}
.ovc-btn.ghost{background:transparent}
.ovc-link{background:none;border:0;padding:0;color:#9a7b50;font-size:9.5px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;cursor:pointer}
.ovc-link:hover{color:#5f4527;text-decoration:underline}
.ovc-alerts{max-width:1060px;margin:0 auto;padding:8px 40px 0}
.ovc-alert{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:10px 14px;margin:6px 0;font-size:12.5px;border:1px solid}
.ovc-alert.ok{background:#f1f2e3;border-color:#c4c99a;color:#4a4a26}
.ovc-alert.err{background:#f7e4dc;border-color:#d9a58f;color:#7a2e15}
.ovc-busy{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:#9a7b50;padding:4px 0}
.ovc-content{max-width:1060px;margin:0 auto;padding:24px 40px 10px}
.ovc-pad{padding:24px 40px}
.ovc-muted{color:#927f69;font:15px var(--serif);margin:10px 0}
.ovc-essentials{background:linear-gradient(135deg,#fffcf6,#faf3e7);border:1px solid #c8ac7d;box-shadow:inset 0 0 0 5px #fbf6ec,0 8px 22px #6a4b2a12;padding:27px 31px;margin:0 0 35px}
.ovc-essentials-head{display:flex;align-items:center;justify-content:space-between;gap:18px;padding-bottom:19px;border-bottom:1px solid #dfceb2}
.ovc-essentials h2,.ovc-sectionhead h2{font:600 32px var(--serif);margin:6px 0 2px}
.ovc-essentials-head p{font:italic 17px var(--serif);color:#907c63;margin:0}
.ovc-progress{font-size:9.5px;color:#9a7b50;text-transform:uppercase;letter-spacing:.09em;white-space:nowrap}
.ovc-essential-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.ovc-essential-item{display:flex;align-items:flex-start;gap:12px;padding:22px 16px;border-right:1px solid #e9dece;border-bottom:1px solid #f0e6d6}
.ovc-essential-item:last-child{border-right:0}
.ovc-essential-text{cursor:pointer;flex:1;min-width:0}
.ovc-essential-text:hover strong{text-decoration:underline;text-decoration-color:#c8ac7d}
.ovc-essential-item strong{font-size:12px;line-height:1.4}
.ovc-essential-item p{font:16px var(--serif);color:#927f69;margin:5px 0 0}
.ovc-checkbox{flex:none;width:18px;height:18px;border:1px solid #a7814e;background:#fffcf6;margin-top:1px;cursor:pointer;position:relative;padding:0}
.ovc-checkbox.checked{background:#a7814e}
.ovc-checkbox.checked:after{content:'✓';color:#fffcf6;font-size:12px;position:absolute;inset:0;display:flex;align-items:center;justify-content:center}
.ovc-tag{margin-left:auto;font-size:8.5px;letter-spacing:.09em;text-transform:uppercase;color:#8d6d43;border:1px solid #dcc7a5;padding:4px 8px;white-space:nowrap;background:#fdf8ef}
.ovc-time{margin-left:auto;font-size:10px;color:#9b8b77;white-space:nowrap}
.ovc-empty{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding:20px 0;font:16px var(--serif);color:#927f69}
.ovc-quickadd{display:flex;gap:8px;padding:14px 0 4px}
.ovc-quickadd input{flex:1;border:1px solid #dfceb2;background:#fffdf8;padding:9px 12px;font:15px var(--serif);color:var(--ink);outline:none}
.ovc-quickadd input:focus{border-color:#a7814e}
.ovc-essentials-foot,.ovc-panel-foot{border-top:1px solid #e9dece;padding-top:14px;margin-top:10px;display:flex;justify-content:space-between;align-items:center;gap:10px;color:#9b8b77;font-size:10px}
.ovc-sectionhead{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;margin:0 0 16px}
.ovc-sectionhead p{font:italic 17px var(--serif);color:#907c63;margin:0}
.ovc-panel{background:#fffcf6;border:1px solid #e2d4be;padding:22px 26px;margin-bottom:22px}
.ovc-panelhead{display:flex;justify-content:space-between;align-items:center;gap:10px;padding-bottom:12px;border-bottom:1px solid #eee5d9}
.ovc-panelhead h3{font:600 24px var(--serif);margin:0}
.ovc-meta{font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:#a08867}
.ovc-row{display:flex;align-items:flex-start;gap:14px;padding:14px 0;border-bottom:1px solid #f1e9dc}
.ovc-row.clickable{cursor:pointer}
.ovc-row.clickable:hover{background:#fbf5ea}
.ovc-row-icon{color:#a7814e;font:18px var(--serif);width:18px;text-align:center}
.ovc-row-body{flex:1;min-width:0}
.ovc-row-body strong{font-size:12px}
.ovc-row-body p{font:15px var(--serif);color:#927f69;margin:3px 0 0}
.ovc-second{display:grid;grid-template-columns:1fr 1fr;gap:22px}
.ovc-second>.ovc-panel{margin-bottom:22px}
.ovc-approach{padding:15px 0 18px;border-bottom:1px solid #eee5d9}
.ovc-approach-heading{display:flex;justify-content:space-between;font-size:11px}
.ovc-approach-heading span{color:#90785a}
.ovc-approach p{font:15px var(--serif);color:#927f69;margin:7px 0 12px}
.ovc-approach input{width:100%;accent-color:#a7804f;margin:3px 0;cursor:pointer}
.ovc-slider-labels{display:flex;justify-content:space-between;gap:3px;color:#98856c;font-size:9px;margin-top:8px}
.ovc-slider-labels span{cursor:pointer}
.ovc-slider-labels span.on{font-weight:700;color:#675037}
.ovc-setting{display:flex;justify-content:space-between;gap:10px;padding:12px 0;border-bottom:1px solid #f1e9dc;font-size:11px}
.ovc-setting span{font:15px var(--serif);color:#7d684f}
.ovc-settings-actions{display:flex;gap:10px;flex-wrap:wrap;padding-top:16px}
.ovc-goldline{height:1px;background:linear-gradient(90deg,transparent,#c8ac7d,transparent);margin:28px 40px 0}
.ovc-footer{text-align:center;font:italic 17px var(--serif);color:#9a8468;padding:16px 0 30px}
.ovc-legacy{color:var(--ink)}
.ovc-legacy input,.ovc-legacy select,.ovc-legacy textarea{background:#fffdf8;color:var(--ink)}
@media(max-width:860px){.ovc{padding:0}.ovc-second{grid-template-columns:1fr}.ovc-content,.ovc-alerts{padding-left:18px;padding-right:18px}.ovc-hero{height:300px}.ovc-hero h1{font-size:38px}.ovc-top{padding:0 18px}.ovc-menu{gap:16px}.ovc-essentials{padding:22px 18px}.ovc-essentials-head,.ovc-sectionhead{flex-direction:column;align-items:flex-start}}
`;

const HERO_IMAGE = 'data:image/jpeg;base64,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';

// Hermes Desktop SDK: ctx.rest(path, { method, body }) targets this plugin's own backend routes.
function restCall(ctx, method, path, body) {
  const rest = ctx && ctx.rest;
  if (typeof rest === 'function') {
    return rest(path, body === undefined ? { method } : { method, body });
  }
  if (rest && typeof rest[method.toLowerCase()] === 'function') {
    return rest[method.toLowerCase()](path, body);
  }
  return Promise.reject(new Error('Plugin backend bridge unavailable.'));
}

function Page({ ctx }) {
  const [activeTab, setActiveTab] = useState('overview');
  const [prefs, setPrefs] = useState(null);
  const [dirtyPrefs, setDirtyPrefsState] = useState(false);
  // Ref mirrors dirtyPrefs so the long-lived poll closure sees current value.
  const dirtyRef = useRef(false);
  // Last settings object confirmed by the backend (no unsaved drafts).
  const serverPrefsRef = useRef(null);
  const setDirtyPrefs = (v) => { dirtyRef.current = Boolean(v); setDirtyPrefsState(Boolean(v)); };
  const [stateData, setStateData] = useState({
    responsibilities: [],
    non_negotiables: [],
    commitments: [],
    detected_system_timezone: '',
    effective_timezone: '',
    effective_timezone_source: '',
    local_today: '',
    lifetime_awareness_active: false,
    quota: null,
    routes: [],
    recent_activity: [],
    cadence_info: null,
    allow_gateway_injection: false,
    plugin_enabled: true,
    hermes_live_config: null,
  });

  const [quickTitle, setQuickTitle] = useState('');
  const [itemFilter, setItemFilter] = useState('all');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  // Add / Edit Item modal state
  const [itemModal, setItemModal] = useState(null);

  // 3-Step Quick Setup modal state
  const [wizardOpen, setWizardOpen] = useState(false);
  const [wizardStep, setWizardStep] = useState(1);
  const [reviewItems, setReviewItems] = useState(null);
  const [reviewError, setReviewError] = useState('');
  const [reviewApplied, setReviewApplied] = useState(false);
  const [projectFolder, setProjectFolder] = useState(null);
  const [projectExisting, setProjectExisting] = useState(null);
  const [projectText, setProjectText] = useState(PROJECT_AGENTS_SUGGESTION);
  const [projectMessage, setProjectMessage] = useState('');
  const [wizardForm, setWizardForm] = useState({
    timezone: '',
    applyQuietPreset: true,
    quietEnabled: true,
    quietStart: '22:00',
    quietEnd: '08:00',
    dailyLimit: 12,
    tone: 'Balanced',
    persistence: 'Persistent',
    nonNegotiables: ['', '', ''],
  });

  const applyIncomingState = (data, preserveDraftPrefs = false) => {
    if (!data) return;
    if (data.settings) serverPrefsRef.current = data.settings;
    if (!preserveDraftPrefs || !dirtyRef.current) {
      setPrefs(data.settings || null);
    }
    const allRows = Array.isArray(data.responsibilities) ? data.responsibilities : [];
    const nn = Array.isArray(data.non_negotiables)
      ? data.non_negotiables
      : allRows.filter((r) => r.kind === 'non_negotiable');
    const cm = Array.isArray(data.commitments)
      ? data.commitments
      : allRows.filter((r) => r.kind !== 'non_negotiable');
    setStateData({
      responsibilities: allRows,
      non_negotiables: nn,
      commitments: cm,
      detected_system_timezone: data.detected_system_timezone || '',
      effective_timezone: data.effective_timezone || '',
      effective_timezone_source: data.effective_timezone_source || '',
      local_today: data.local_today || '',
      lifetime_awareness_active: Boolean(data.lifetime_awareness_active),
      quota: data.quota || null,
      routes: Array.isArray(data.routes) ? data.routes : [],
      recent_activity: Array.isArray(data.recent_activity) ? data.recent_activity : [],
      cadence_info: data.cadence_info || null,
      allow_gateway_injection: Boolean(data.allow_gateway_injection),
      plugin_enabled: data.plugin_enabled !== false,
      hermes_live_config: data.hermes_live_config || null,
    });
  };

  const refreshState = (aliveGuard = () => true, preserveDraft = true) =>
    restCall(ctx, 'GET', '/state')
      .then((data) => {
        if (!aliveGuard()) return null;
        applyIncomingState(data, preserveDraft);
        return data;
      })
      .catch((err) => {
        if (aliveGuard()) {
          setError((err && err.message) || 'Could not load Butler.');
        }
        return null;
      });

  useEffect(() => {
    let alive = true;
    setPrefs(null);
    setDirtyPrefs(false);
    setMessage('');
    setError('');
    refreshState(() => alive, false);

    const poll = () => {
      if (!alive) return;
      restCall(ctx, 'GET', '/state')
        .then((data) => {
          if (!alive || !data) return;
          applyIncomingState(data, true);
        })
        .catch(() => {});
    };

    let stopTimer = null;
    if (ctx && typeof ctx.setInterval === 'function') {
      const handle = ctx.setInterval(poll, 15000);
      stopTimer = () => {
        if (typeof handle === 'function') handle();
        else if (handle != null) globalThis.clearInterval(handle);
      };
    } else {
      const id = globalThis.setInterval(poll, 15000);
      stopTimer = () => globalThis.clearInterval(id);
    }

    return () => {
      alive = false;
      if (stopTimer) stopTimer();
    };
  }, [ctx]);

  const updatePref = (key, value) => {
    setPrefs((curr) => ({ ...(curr || {}), [key]: value }));
    setDirtyPrefs(true);
    setMessage('');
  };

  const openQuickSetup = () => {
    setReviewItems(null);
    setReviewError('');
    setReviewApplied(false);
    const baseTz = (prefs && prefs.timezone) || stateData.detected_system_timezone || stateData.effective_timezone || '';
    setWizardForm({
      timezone: baseTz,
      applyQuietPreset: true,
      quietEnabled: prefs ? prefs.quietEnabled !== false : true,
      quietStart: (prefs && prefs.quietStart) || '22:00',
      quietEnd: (prefs && prefs.quietEnd) || '08:00',
      dailyLimit: (prefs && prefs.dailyLimit != null) ? prefs.dailyLimit : 12,
      tone: (prefs && prefs.tone) || 'Balanced',
      persistence: (prefs && prefs.persistence) || 'Persistent',
      nonNegotiables: ['', '', ''],
    });
    setWizardStep(1);
    setWizardOpen(true);
  };

  const openCreateModal = (defaultKind = 'commitment') => {
    setItemModal({
      mode: 'create',
      id: null,
      title: '',
      kind: defaultKind,
      due_at: '',
      deadline_at: '',
      prep_hint: '',
      notes: '',
    });
  };

  const openEditModal = (item) => {
    const tz = stateData.effective_timezone || item.timezone || '';
    setItemModal({
      mode: 'edit',
      id: item.id,
      title: item.title || '',
      kind: item.kind === 'non_negotiable' ? 'non_negotiable' : (item.kind || 'commitment'),
      due_at: toDatetimeLocalValue(item.due_at, tz),
      deadline_at: toDatetimeLocalValue(item.deadline_at, tz),
      prep_hint: item.prep_hint || '',
      notes: item.notes || '',
    });
  };

  const handleQuickAddNonNegotiable = async (e) => {
    if (e && e.preventDefault) e.preventDefault();
    const trimmed = quickTitle.trim();
    if (!trimmed) return;
    setBusy(true);
    setError('');
    try {
      await restCall(ctx, 'POST', '/responsibilities', {
        title: trimmed,
        kind: 'non_negotiable',
      });
      setQuickTitle('');
      await refreshState(() => true, true);
      setMessage(`Added daily non-negotiable: "${trimmed}".`);
    } catch (err) {
      setError((err && err.message) || 'Could not add daily non-negotiable.');
    } finally {
      setBusy(false);
    }
  };

  const handleSaveItemModal = async (e) => {
    if (e && e.preventDefault) e.preventDefault();
    if (!itemModal || !itemModal.title.trim()) {
      setError('Please enter a title.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      if (itemModal.mode === 'create') {
        await restCall(ctx, 'POST', '/responsibilities', {
          title: itemModal.title.trim(),
          kind: itemModal.kind,
          due_at: itemModal.due_at || null,
          deadline_at: itemModal.deadline_at || null,
          prep_hint: itemModal.prep_hint || '',
          notes: itemModal.notes || '',
        });
        setMessage(`Added ${itemModal.kind === 'non_negotiable' ? 'daily non-negotiable' : 'commitment'}: "${itemModal.title.trim()}".`);
      } else {
        await restCall(ctx, 'POST', `/responsibilities/${encodeURIComponent(itemModal.id)}/action`, {
          action: 'update',
          title: itemModal.title.trim(),
          kind: itemModal.kind,
          due_at: itemModal.due_at || null,
          deadline_at: itemModal.deadline_at || null,
          prep_hint: itemModal.prep_hint || '',
          notes: itemModal.notes || '',
        });
        setMessage(`Updated "${itemModal.title.trim()}".`);
      }
      setItemModal(null);
      await refreshState(() => true, true);
    } catch (err) {
      setError((err && err.message) || 'Could not save item.');
    } finally {
      setBusy(false);
    }
  };

  const runItemAction = async (item, actionPayload, feedbackText = '') => {
    setBusy(true);
    setError('');
    try {
      await restCall(ctx, 'POST', `/responsibilities/${encodeURIComponent(item.id)}/action`, actionPayload);
      await refreshState(() => true, true);
      if (feedbackText) setMessage(feedbackText);
    } catch (err) {
      setError((err && err.message) || `Could not perform ${actionPayload.action}.`);
    } finally {
      setBusy(false);
    }
  };

  const toggleItemDone = (item) => {
    const isDone = item.kind === 'non_negotiable' ? Boolean(item.done_today) : item.status === 'completed';
    if (isDone) {
      runItemAction(item, { action: 'reopen' }, `Reopened "${item.title}".`);
    } else {
      runItemAction(item, { action: 'complete' }, `Checked off "${item.title}".`);
    }
  };

  const handleCheckInNow = async (itemId = null) => {
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const res = await restCall(ctx, 'POST', '/check-in-now', itemId ? { item_id: itemId } : {});
      await refreshState(() => true, true);
      if (res && res.ok) {
        setMessage(res.detail || 'Proactive check-in triggered.');
      } else {
        setError((res && res.detail) || 'Check-in could not fire right now.');
      }
    } catch (err) {
      setError((err && err.message) || 'Could not trigger check-in.');
    } finally {
      setBusy(false);
    }
  };

  const handleEnableBackgroundWakes = async () => {
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const res = await restCall(ctx, 'POST', '/enable-proactive', {
        timezone: (prefs && prefs.timezone) || stateData.detected_system_timezone || '',
      });
      setDirtyPrefs(false);
      applyIncomingState(res, false);
      setMessage('Proactive wakes and gateway session injection are now enabled.');
    } catch (err) {
      setError((err && err.message) || 'Could not enable proactive wakes.');
    } finally {
      setBusy(false);
    }
  };

  const loadPromptReview = async () => {
    setWizardStep(4);
    setBusy(true);
    setError('');
    setReviewError('');
    setReviewItems(null);
    setReviewApplied(false);
    try {
      const result = await restCall(ctx, 'GET', '/prompt-review');
      if (!result || !Array.isArray(result.items)) throw new Error('Invalid prompt review response.');
      setReviewItems(result.items.map((item) => ({ ...item, selected: false, originalProposed: item.proposed })));
    } catch (err) {
      setReviewError((err && err.message) || 'Could not load prompt review.');
    } finally {
      setBusy(false);
    }
  };

  const applyPromptReview = async () => {
    const items = (reviewItems || []).filter((item) => item.selected)
      .map(({ id, sha256, proposed }) => ({ id, sha256, proposed }));
    if (!items.length) return;
    setBusy(true);
    setReviewError('');
    try {
      const result = await restCall(ctx, 'POST', '/prompt-review/apply', { items });
      if (!result || result.error || result.ok === false || result.success === false || (Array.isArray(result.conflicts) && result.conflicts.length) || (Array.isArray(result.errors) && result.errors.length)) {
        throw new Error((result && (result.error || JSON.stringify(result.conflicts || result.errors))) || 'Could not apply prompt changes.');
      }
      setReviewApplied(true);
    } catch (err) {
      setReviewError(`${(err && err.message) || 'Could not apply prompt changes.'} Refresh the review before retrying; refreshing discards edits.`);
      setReviewItems(null);
    } finally {
      setBusy(false);
    }
  };

  const chooseProject = async () => {
    setProjectFolder(null);
    setProjectExisting(null);
    setProjectMessage('');
    if (!ctx.files || typeof ctx.files.requestFolder !== 'function') {
      setProjectMessage('Project folder access is unavailable in this Desktop version.');
      return;
    }
    setBusy(true);
    try {
      const picked = await ctx.files.requestFolder();
      if (!picked || !picked.ok) throw new Error((picked && picked.message) || 'Folder selection unavailable.');
      if (!picked.folder) return;
      const status = await ctx.files.stat(picked.folder.id, 'AGENTS.md');
      if (!status || !status.ok) throw new Error((status && status.message) || 'Could not inspect AGENTS.md.');
      if (status.exists) {
        const existing = await ctx.files.readText(picked.folder.id, 'AGENTS.md');
        if (!existing || !existing.ok) throw new Error((existing && existing.message) || 'Could not read AGENTS.md.');
        setProjectExisting(existing.text);
      }
      setProjectFolder(picked.folder);
    } catch (err) {
      setProjectMessage((err && err.message) || 'Could not choose project folder.');
    } finally { setBusy(false); }
  };

  const applyProjectAgents = async () => {
    setBusy(true);
    setProjectMessage('');
    try {
      await createProjectAgents(ctx.files, projectFolder, projectText);
      setProjectExisting(projectText);
      setProjectMessage('AGENTS.md created and verified.');
    } catch (err) {
      setProjectMessage((err && err.message) || 'Could not create AGENTS.md.');
    } finally { setBusy(false); }
  };

  const handleCompleteWizard = async () => {
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const cleanNN = wizardForm.nonNegotiables
        .map((s) => s.trim())
        .filter(Boolean);
      const res = await restCall(ctx, 'POST', '/enable-proactive', {
        timezone: wizardForm.timezone.trim(),
        applyQuietPreset: Boolean(wizardForm.applyQuietPreset),
        quietEnabled: Boolean(wizardForm.quietEnabled),
        quietStart: wizardForm.quietStart,
        quietEnd: wizardForm.quietEnd,
        dailyLimit: wizardForm.dailyLimit === '' || wizardForm.dailyLimit == null ? null : Number(wizardForm.dailyLimit),
        tone: wizardForm.tone,
        persistence: wizardForm.persistence,
        nonNegotiables: cleanNN,
      });
      setDirtyPrefs(false);
      applyIncomingState(res, false);
      setWizardOpen(false);
      setMessage(`Butler armed (${wizardForm.tone} · ${wizardForm.persistence})${res.seeded_count ? ` with ${res.seeded_count} daily non-negotiable(s)` : ''}.`);
    } catch (err) {
      setError((err && err.message) || 'Quick setup failed.');
    } finally {
      setBusy(false);
    }
  };

  const detectTimezone = async (useIp, forWizard = false) => {
    setBusy(true);
    setError('');
    try {
      const res = await restCall(ctx, 'POST', '/detect-timezone', { use_ip: Boolean(useIp) });
      const zone = (res && res.suggested_timezone) || '';
      if (zone) {
        if (forWizard) {
          setWizardForm((f) => ({ ...f, timezone: zone }));
        } else {
          updatePref('timezone', zone);
          setMessage(useIp
            ? `Detected timezone ${zone} via IP/OS. Click Save Preferences to confirm.`
            : `Detected system timezone ${zone}. Click Save Preferences to confirm.`);
        }
      } else {
        setError('Could not auto-detect an IANA timezone; enter one manually (e.g. America/Chicago).');
      }
    } catch (err) {
      setError((err && err.message) || 'Timezone detection failed.');
    } finally {
      setBusy(false);
    }
  };

  const applyQuietPreset = async () => {
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const res = await restCall(ctx, 'POST', '/preset', { preset: 'quiet_human' });
      setDirtyPrefs(false);
      setPrefs(res.settings);
      await refreshState(() => true, false);
      setMessage('Applied Quiet Human Texting preset and synced notification/load controls to Hermes config.');
    } catch (err) {
      setError((err && err.message) || 'Could not apply Quiet Human preset.');
    } finally {
      setBusy(false);
    }
  };

  const savePreferences = async () => {
    if (!prefs) return;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const payload = { ...prefs };
      if (payload.quietEnabled && (!payload.quietStart || !payload.quietEnd)) {
        payload.quietStart = payload.quietStart || '22:00';
        payload.quietEnd = payload.quietEnd || '08:00';
      }
      const saved = await restCall(ctx, 'PUT', '/settings', payload);
      setDirtyPrefs(false);
      setPrefs(saved);
      await refreshState(() => true, false);
      setMessage(saved.applySystemControls
        ? 'Saved preferences and synced system notification/load controls to Hermes config.'
        : 'Saved profile preferences.');
    } catch (err) {
      setError((err && err.message) || 'Could not save preferences.');
    } finally {
      setBusy(false);
    }
  };

  // Derived status & metrics
  const effTz = stateData.effective_timezone || (prefs && prefs.timezone) || stateData.detected_system_timezone || 'UTC';
  const quota = stateData.quota || {};
  const setupNeeded = !prefs || !prefs.notificationsEnabled || !stateData.allow_gateway_injection || !(prefs.timezone || stateData.detected_system_timezone);

  let statusBadge = { tone: 'amber', label: 'Setup Needed' };
  if (prefs) {
    if (prefs.paused) {
      statusBadge = { tone: 'muted', label: 'Paused' };
    } else if (!prefs.notificationsEnabled || !stateData.allow_gateway_injection) {
      statusBadge = { tone: 'amber', label: 'Setup Needed' };
    } else if (quota.in_quiet_hours) {
      statusBadge = { tone: 'accent', label: 'In Quiet Hours' };
    } else {
      statusBadge = { tone: 'ok', label: 'Active & Armed' };
    }
  }

  const activeNN = stateData.non_negotiables.filter((i) => i.status !== 'stopped');
  const doneNNCount = activeNN.filter((i) => i.done_today).length;
  const totalNNCount = activeNN.length;
  const nnProgressPct = totalNNCount > 0 ? Math.round((doneNNCount / totalNNCount) * 100) : 0;

  const openCommitments = stateData.commitments.filter((i) => i.status === 'active' || i.status === 'queued' || i.status === 'submitted');

  const renderItemRow = (item, compact = false) => {
    const isNN = item.kind === 'non_negotiable';
    const isDone = isNN ? Boolean(item.done_today) : item.status === 'completed';
    const isPaused = item.status === 'paused';
    const nextTimeStr = formatTimeOnly(item.due_at, effTz);
    const fullDueStr = formatDateTime(item.due_at, effTz);

    return h('div', {
      key: item.id,
      style: {
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'space-between',
        gap: 12,
        padding: compact ? '10px 12px' : '13px 14px',
        borderRadius: 11,
        border: '1px solid var(--border, rgba(127, 127, 127, 0.18))',
        background: isDone
          ? 'color-mix(in srgb, var(--foreground, #888) 3%, transparent)'
          : 'color-mix(in srgb, var(--card, transparent) 88%, transparent)',
        marginBottom: 8,
        opacity: isDone ? 0.76 : 1,
      },
    },
      h('div', { style: { display: 'flex', alignItems: 'flex-start', gap: 11, flex: 1, minWidth: 0 } },
        h('input', {
          type: 'checkbox',
          checked: isDone,
          disabled: busy,
          onChange: () => toggleItemDone(item),
          'aria-label': `Mark ${item.title} ${isDone ? 'incomplete' : 'complete'}`,
          style: { width: 17, height: 17, marginTop: 2, cursor: 'pointer', accentColor: '#459457' },
        }),
        h('div', { style: { flex: 1, minWidth: 0 } },
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' } },
            h('span', {
              style: {
                fontSize: 14,
                fontWeight: 560,
                textDecoration: isDone ? 'line-through' : 'none',
                color: isDone ? 'var(--muted-foreground, #777)' : 'var(--foreground, inherit)',
              },
            }, item.title),
            !compact && h('span', {
              style: {
                fontSize: 11,
                padding: '2px 7px',
                borderRadius: 999,
                background: isNN
                  ? 'color-mix(in srgb, #459457 15%, transparent)'
                  : 'color-mix(in srgb, var(--foreground, #888) 8%, transparent)',
                border: '1px solid var(--border, rgba(127, 127, 127, 0.22))',
                color: 'var(--muted-foreground, #6e776f)',
              },
            }, isNN ? 'Daily Non-Negotiable' : 'Commitment'),
            isPaused && h('span', {
              style: { fontSize: 11, padding: '2px 7px', borderRadius: 999, background: 'color-mix(in srgb, #c8872b 16%, transparent)' },
            }, 'Paused'),
            isDone && isNN && h('span', {
              style: { fontSize: 11, padding: '2px 7px', borderRadius: 999, background: 'color-mix(in srgb, #459457 18%, transparent)' },
            }, 'Done today · resets tomorrow'),
          ),
          h('div', {
            style: {
              display: 'flex',
              gap: 10,
              flexWrap: 'wrap',
              marginTop: 4,
              fontSize: 12,
              color: 'var(--muted-foreground, #6e776f)',
            },
          },
            !isDone && h('span', null,
              item.status === 'submitted'
                ? `Nudged ${item.nudge_count || 0}x · awaiting next touch decision`
                : item.nudge_count > 0
                  ? `Nudged ${item.nudge_count}x · next at ${nextTimeStr || fullDueStr}`
                  : `Next check-in: ${fullDueStr}`,
            ),
            item.deadline_at && h('span', null, `Hard deadline: ${formatDateTime(item.deadline_at, effTz)}`),
            item.prep_hint && h('span', { style: { fontStyle: 'italic' } }, `Prep: ${item.prep_hint}`),
            !compact && item.notes && h('span', null, item.notes),
          ),
        ),
      ),
      h('div', { style: { display: 'flex', gap: 5, flexWrap: 'wrap', alignItems: 'center' } },
        !isDone && h('button', {
          type: 'button',
          disabled: busy,
          style: styles.btn('mini', busy),
          onClick: () => runItemAction(item, { action: 'snooze', snooze_minutes: 30 }, `Snoozed "${item.title}" +30m.`),
        }, '+30m'),
        !isDone && h('button', {
          type: 'button',
          disabled: busy,
          style: styles.btn('mini', busy),
          onClick: () => runItemAction(item, { action: 'snooze', snooze_minutes: 120 }, `Snoozed "${item.title}" +2h.`),
        }, '+2h'),
        !compact && !isDone && h('button', {
          type: 'button',
          disabled: busy,
          style: styles.btn('mini', busy),
          onClick: () => runItemAction(item, { action: 'snooze', until: 'tomorrow_morning' }, `Moved "${item.title}" to tomorrow morning.`),
        }, 'Tomorrow'),
        !compact && !isDone && h('button', {
          type: 'button',
          disabled: busy,
          style: styles.btn('mini', busy),
          onClick: () => runItemAction(
            item,
            { action: isPaused ? 'resume' : 'pause' },
            `${isPaused ? 'Resumed' : 'Paused'} "${item.title}".`,
          ),
        }, isPaused ? 'Resume' : 'Pause'),
        !compact && h('button', {
          type: 'button',
          disabled: busy,
          style: styles.btn('mini', busy),
          onClick: () => openEditModal(item),
        }, 'Edit'),
        !compact && h('button', {
          type: 'button',
          disabled: busy,
          style: { ...styles.btn('mini', busy), color: 'color-mix(in srgb, #d9534f 85%, var(--foreground, #c33))' },
          onClick: () => runItemAction(item, { action: 'delete' }, `Deleted "${item.title}".`),
        }, 'Delete'),
      ),
    );
  };

  // Tab 1: Overview
  // ── v7 "Grand Concierge" overview ────────────────────────────────────────
  const goTab = (tab, filter) => {
    if (filter) setItemFilter(filter);
    setActiveTab(tab);
    setMessage('');
  };

  const saveTone = async (toneName) => {
    const base = serverPrefsRef.current;
    if (!base || !TONES.includes(toneName) || busy) return;
    if (base.tone === toneName) {
      setPrefs((p) => ({ ...(p || base), tone: toneName }));
      return;
    }
    // Send only the confirmed server settings + new tone so unsaved
    // Preferences drafts are never persisted as a side effect.
    const next = { ...base, tone: toneName };
    setPrefs((p) => ({ ...(p || base), tone: toneName }));
    setBusy(true);
    setError('');
    try {
      const saved = await restCall(ctx, 'PUT', '/settings', next);
      serverPrefsRef.current = saved;
      setPrefs((p) => (dirtyRef.current ? { ...(p || saved), tone: saved.tone } : saved));
      setMessage(`Approach set to ${toneName}.`);
    } catch (err) {
      setError((err && err.message) || 'Could not save approach.');
    } finally {
      setBusy(false);
    }
  };

  const togglePaused = async () => {
    const base = serverPrefsRef.current;
    if (!base) return;
    const next = { ...base, paused: !base.paused };
    setBusy(true);
    setError('');
    try {
      const saved = await restCall(ctx, 'PUT', '/settings', next);
      serverPrefsRef.current = saved;
      setPrefs((p) => (dirtyRef.current ? { ...(p || saved), paused: saved.paused } : saved));
      await refreshState(() => true, true);
      setMessage(saved.paused ? 'Check-ins paused. Nothing will be sent until you resume.' : 'Check-ins resumed.');
    } catch (err) {
      setError((err && err.message) || 'Could not change pause state.');
    } finally {
      setBusy(false);
    }
  };

  const openPromptReviewSetup = () => {
    openQuickSetup();
    loadPromptReview();
  };

  const greeting = () => {
    let hour = new Date().getHours();
    try {
      hour = Number(new Intl.DateTimeFormat('en-US', { hour: 'numeric', hourCycle: 'h23', timeZone: effTz }).format(new Date()));
    } catch (_) { /* keep local hour */ }
    if (hour < 5) return 'Good evening';
    if (hour < 12) return 'Good morning';
    if (hour < 18) return 'Good afternoon';
    return 'Good evening';
  };

  const todayLabel = () => {
    try {
      return new Intl.DateTimeFormat('en-US', { weekday: 'long', month: 'long', day: 'numeric', timeZone: effTz }).format(new Date());
    } catch (_) {
      return new Date().toDateString();
    }
  };

  const fmt12 = (hhmm) => {
    if (!hhmm || !/^\d{1,2}:\d{2}$/.test(hhmm)) return hhmm || '';
    const [hs, ms] = hhmm.split(':');
    const hNum = Number(hs);
    const suffix = hNum >= 12 ? 'PM' : 'AM';
    const h12 = hNum % 12 === 0 ? 12 : hNum % 12;
    return ms === '00' ? `${h12} ${suffix}` : `${h12}:${ms} ${suffix}`;
  };

  const dueLabel = (item) => {
    const iso = item.due_at || item.deadline_at;
    if (!iso) return 'No date';
    return formatDateTime(iso, effTz);
  };

  const Btn = (label, onClick, variant = '', extra = {}) => h('button', {
    type: 'button',
    className: `ovc-btn ${variant}`,
    disabled: busy,
    onClick,
    ...extra,
  }, label);

  const Link = (label, onClick) => h('button', { type: 'button', className: 'ovc-link', onClick }, label, ' \u00a0↗');

  const Row = (icon, title, sub, tag, opts = {}) => h('div', {
    key: opts.key || title,
    className: `ovc-row${opts.onClick ? ' clickable' : ''}`,
    onClick: opts.onClick,
    role: opts.onClick ? 'button' : undefined,
    tabIndex: opts.onClick ? 0 : undefined,
    onKeyDown: opts.onClick ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); opts.onClick(); } } : undefined,
  },
    h('span', { className: 'ovc-row-icon' }, icon),
    h('div', { className: 'ovc-row-body' }, h('strong', null, title), sub && h('p', null, sub)),
    tag && h('span', { className: opts.time ? 'ovc-time' : 'ovc-tag' }, tag),
  );

  const activityTitle = (entry) => {
    const act = String(entry.action || 'event').replace(/_/g, ' ');
    const title = entry.item_title ? ` · ${entry.item_title}` : '';
    return act.charAt(0).toUpperCase() + act.slice(1) + title;
  };

  const activityIcon = (entry) => {
    const act = String(entry.action || '');
    if (act.includes('complete') || act.includes('done')) return '✓';
    if (act.includes('wake') || act.includes('check')) return '✧';
    if (act.includes('snooze') || act.includes('pause')) return '◌';
    return '❧';
  };

  const renderActivityRows = (limit) => stateData.recent_activity.slice(0, limit).map((entry, idx) =>
    Row(activityIcon(entry), activityTitle(entry), entry.detail || entry.outcome || '',
      formatDateTime(entry.timestamp, effTz), { key: entry.id || `${entry.timestamp}-${idx}`, time: true }));

  const renderOverviewTab = () => {
    const toneVal = (prefs && prefs.tone) || 'Balanced';
    const toneIdx = Math.max(0, TONES.indexOf(toneVal));
    const persVal = (prefs && prefs.persistence) || 'Occasional';
    const quietDesc = prefs && prefs.quietEnabled && prefs.quietStart && prefs.quietEnd
      ? `${fmt12(prefs.quietStart)} – ${fmt12(prefs.quietEnd)}`
      : 'Off';
    const nowMs = Date.now();
    const overdue = openCommitments.filter((i) => i.due_at && Date.parse(i.due_at) < nowMs);
    const remainingNN = totalNNCount - doneNNCount;
    const upcoming = [...openCommitments].sort((a, b) => {
      const da = Date.parse(a.due_at || a.deadline_at || '') || Infinity;
      const db = Date.parse(b.due_at || b.deadline_at || '') || Infinity;
      return da - db;
    });

    const handsRows = [];
    if (prefs && prefs.paused) {
      handsRows.push(Row('◌', 'Check-ins are paused', 'Nothing will be sent until you resume.', 'Paused', { key: 'paused', onClick: togglePaused }));
    }
    if (setupNeeded) {
      handsRows.push(Row('✦', 'Finish setting up your assistant', 'Choose your pace and quiet hours, and turn on background check-ins.', 'Setup', { key: 'setup', onClick: openQuickSetup }));
    }
    if (openCommitments.length) {
      const first = upcoming[0];
      handsRows.push(Row('❧', first.title, openCommitments.length > 1 ? `Plus ${openCommitments.length - 1} more commitment${openCommitments.length > 2 ? 's' : ''} in view.` : 'Your assistant is keeping this in view.', 'In progress', { key: 'c', onClick: () => goTab('items', 'commitments') }));
    }
    handsRows.push(Row('❧', 'Your daily priorities',
      totalNNCount === 0 ? 'None set yet. Add the few things that matter most.'
        : remainingNN === 0 ? 'All complete for today.'
          : `${doneNNCount} complete; ${remainingNN} still on the list.`,
      'Today', { key: 'nn', onClick: () => goTab('items', 'non_negotiables') }));
    handsRows.push(overdue.length
      ? Row('❧', `${overdue.length} overdue`, overdue.map((i) => i.title).slice(0, 3).join(', '), 'Attention', { key: 'od', onClick: () => goTab('items', 'commitments') })
      : Row('❧', 'Nothing urgent', 'There are no decisions waiting for you.', 'All clear', { key: 'ok' }));

    return h('div', { className: 'ovc-content' },
      // Daily non-negotiables
      h('section', { className: 'ovc-essentials' },
        h('div', { className: 'ovc-essentials-head' },
          h('div', null,
            h('h2', null, 'Your daily non-negotiables'),
            h('p', null, 'The things that matter most. Your assistant will help you follow through.'),
          ),
          totalNNCount > 0 && h('span', { className: 'ovc-progress' }, `${doneNNCount} of ${totalNNCount} complete`),
        ),
        activeNN.length
          ? h('div', { className: 'ovc-essential-list' }, activeNN.map((item) => {
              const done = Boolean(item.done_today);
              return h('div', { key: item.id, className: 'ovc-essential-item' },
                h('button', {
                  type: 'button',
                  className: `ovc-checkbox${done ? ' checked' : ''}`,
                  disabled: busy,
                  'aria-label': done ? `Reopen ${item.title}` : `Mark ${item.title} complete`,
                  onClick: () => toggleItemDone(item),
                }),
                h('div', { className: 'ovc-essential-text', role: 'button', tabIndex: 0, onClick: () => openEditModal(item), onKeyDown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openEditModal(item); } } },
                  h('strong', null, item.title),
                  h('p', null, done ? 'Completed today' : (item.status === 'paused' ? 'Paused' : (item.prep_hint || item.notes || 'Not done yet today.'))),
                ),
                h('span', { className: 'ovc-tag' }, done ? 'Done' : (item.status === 'paused' ? 'Paused' : 'Daily')),
              );
            }))
          : h('div', { className: 'ovc-empty' },
              h('span', null, 'No daily non-negotiables yet. Add the few things you never want to skip.'),
              Btn('＋ Add a priority', () => openCreateModal('non_negotiable'), 'small'),
            ),
        h('form', { className: 'ovc-quickadd', onSubmit: handleQuickAddNonNegotiable },
          h('input', {
            type: 'text',
            value: quickTitle,
            placeholder: 'Add another daily non-negotiable…',
            'aria-label': 'New daily non-negotiable',
            onChange: (e) => setQuickTitle(e.target.value),
          }),
          h('button', { type: 'submit', className: 'ovc-btn small', disabled: busy || !quickTitle.trim() }, 'Add'),
        ),
        h('div', { className: 'ovc-essentials-foot' },
          h('span', null, 'Resets each morning · Your assistant will check in until these are complete or deferred.'),
          Link('Manage essentials', () => goTab('items', 'non_negotiables')),
        ),
      ),

      h('div', { className: 'ovc-sectionhead' },
        h('div', null, h('div', { className: 'ovc-eyebrow' }, 'Thoughtfully prepared for you'), h('h2', null, 'Your daily briefing')),
        h('p', null, 'Everything you need, attended to.'),
      ),

      h('section', { className: 'ovc-panel' },
        h('div', { className: 'ovc-panelhead' }, h('h3', null, 'In good hands'), h('span', { className: 'ovc-meta' }, 'Your assistant')),
        ...handsRows,
        h('div', { className: 'ovc-panel-foot' },
          h('span', null, `Your assistant will follow up at the pace you choose (${persVal.toLowerCase()}).`),
          Link('View activity', () => goTab('activity')),
        ),
      ),

      h('div', { className: 'ovc-second' },
        h('section', { className: 'ovc-panel' },
          h('div', { className: 'ovc-panelhead' }, h('h3', null, 'Upcoming commitments'), Btn('＋ Add commitment', () => openCreateModal('commitment'), 'small ghost')),
          upcoming.length
            ? upcoming.slice(0, 4).map((item) => Row('♢', item.title, item.prep_hint || item.notes || 'Your assistant will help you stay on track.', dueLabel(item), { key: item.id, onClick: () => openEditModal(item) }))
            : h('p', { className: 'ovc-muted' }, 'No open commitments. Add one here, or ask Hermes in chat to track a follow-up.'),
          h('div', { className: 'ovc-panel-foot' },
            h('span', null, upcoming.length > 4 ? `${upcoming.length - 4} more not shown.` : 'Follow-ups are managed by your assistant.'),
            Link('Manage commitments', () => goTab('items', 'commitments')),
          ),
        ),

        h('section', { className: 'ovc-panel' },
          h('div', { className: 'ovc-panelhead' }, h('h3', null, 'How I assist you'), Link('Edit', () => goTab('preferences'))),
          h('div', { className: 'ovc-approach' },
            h('div', { className: 'ovc-approach-heading' }, h('b', null, 'Approach'), h('span', null, toneVal)),
            h('p', null, TONE_DESCRIPTIONS[toneVal] || 'Choose how direct your assistant sounds.'),
            h('input', {
              type: 'range', min: 0, max: TONES.length - 1, step: 1, value: toneIdx,
              disabled: busy,
              'aria-label': 'Assistant approach, from gentle to strict',
              'aria-valuetext': toneVal,
              onChange: (e) => setPrefs((p) => ({ ...(p || {}), tone: TONES[Number(e.target.value)] })),
              onPointerUp: (e) => saveTone(TONES[Number(e.target.value)]),
              onKeyUp: (e) => { if (/^(Arrow|Home|End|Page)/.test(e.key)) saveTone(TONES[Number(e.target.value)]); },
            }),
            h('div', { className: 'ovc-slider-labels' }, TONES.map((t, i) => h('span', {
              key: t,
              className: i === toneIdx ? 'on' : '',
              role: 'button',
              tabIndex: 0,
              'aria-pressed': i === toneIdx,
              onClick: () => saveTone(t),
              onKeyDown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); saveTone(t); } },
            }, t))),
          ),
          h('div', { className: 'ovc-setting' }, h('b', null, 'Follow-up pace'), h('span', null, persVal)),
          h('div', { className: 'ovc-setting' }, h('b', null, 'Quiet hours'), h('span', null, quietDesc)),
          h('div', { className: 'ovc-setting' }, h('b', null, 'Time zone'), h('span', null, effTz.replace(/\//g, ' / '))),
          h('div', { className: 'ovc-setting' }, h('b', null, 'Check-ins today'), h('span', null, `${quota.used_today != null ? quota.used_today : 0} of ${prefs && prefs.dailyLimit != null ? prefs.dailyLimit : '∞'}`)),
          h('div', { className: 'ovc-settings-actions' },
            Btn('Adjust preferences', () => goTab('preferences'), 'small'),
            Btn(prefs && prefs.paused ? 'Resume check-ins' : 'Pause check-ins', togglePaused, 'small ghost'),
          ),
        ),
      ),

      h('div', { className: 'ovc-second' },
        h('section', { className: 'ovc-panel' },
          h('div', { className: 'ovc-panelhead' }, h('h3', null, 'Recent activity'), Link('View all', () => goTab('activity'))),
          stateData.recent_activity.length
            ? renderActivityRows(4)
            : h('p', { className: 'ovc-muted' }, 'Nothing yet. Check-ins, completions and updates will appear here.'),
        ),
        h('section', { className: 'ovc-panel' },
          h('div', { className: 'ovc-panelhead' }, h('h3', null, 'Make it yours'), h('span', { className: 'ovc-meta' }, 'Personalize')),
          Row('✧', 'Shape your assistant’s approach', 'Review suggested updates to its instructions and capabilities.', null, { key: 'shape', onClick: openPromptReviewSetup }),
          !stateData.allow_gateway_injection && Row('✦', 'Turn on background check-ins', 'Let your assistant reach out on its own, within your quiet hours and daily limit.', 'Off', { key: 'bg', onClick: handleEnableBackgroundWakes }),
          h('div', { className: 'ovc-settings-actions' },
            Btn('Review assistant setup', openPromptReviewSetup, 'small'),
            Btn('Open quick setup', openQuickSetup, 'small ghost'),
          ),
        ),
      ),
    );
  };

  const renderActivityTab = () => h('div', { className: 'ovc-content' },
    h('section', { className: 'ovc-panel' },
      h('div', { className: 'ovc-panelhead' },
        h('h3', null, 'Activity'),
        Btn('Refresh', () => refreshState(() => true, true), 'small ghost'),
      ),
      stateData.recent_activity.length
        ? renderActivityRows(200)
        : h('p', { className: 'ovc-muted' }, 'No activity logged yet. Check-ins, completions, daily resets and schedule changes appear here.'),
    ),
  );

  // Tab 2: Non-Negotiables & Commitments
  const renderItemsTab = () => {
    const all = stateData.responsibilities;
    const filterDefs = [
      { id: 'all', label: 'All', count: all.length },
      { id: 'non_negotiables', label: 'Daily Non-Negotiables', count: all.filter((i) => i.kind === 'non_negotiable').length },
      { id: 'commitments', label: 'Open Commitments', count: all.filter((i) => i.kind !== 'non_negotiable' && i.status === 'active').length },
      { id: 'snoozed', label: 'Snoozed / Paused', count: all.filter((i) => i.status === 'paused' || isSnoozedFuture(i)).length },
      { id: 'completed', label: 'Completed', count: all.filter((i) => i.done_today || i.status === 'completed' || i.status === 'stopped').length },
    ];

    const filtered = all.filter((item) => {
      if (itemFilter === 'non_negotiables') return item.kind === 'non_negotiable';
      if (itemFilter === 'commitments') return item.kind !== 'non_negotiable' && item.status === 'active';
      if (itemFilter === 'snoozed') return item.status === 'paused' || isSnoozedFuture(item);
      if (itemFilter === 'completed') return Boolean(item.done_today) || item.status === 'completed' || item.status === 'stopped';
      return true;
    });

    return h('div', null,
      h('section', { style: styles.card },
        h('div', { style: styles.cardHeader },
          h('div', null,
            h('h2', { style: styles.cardTitle }, 'Non-Negotiables & Commitments'),
            h('p', { style: styles.cardHint },
              'Manage every daily habit, open loop, and scheduled commitment in one place. Chat tool updates sync automatically every 15s.',
            ),
          ),
          h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
            h('button', {
              type: 'button',
              disabled: busy,
              style: styles.btn('sage', busy),
              onClick: () => openCreateModal('non_negotiable'),
            }, '+ Add Non-Negotiable'),
            h('button', {
              type: 'button',
              disabled: busy,
              style: styles.btn('primary', busy),
              onClick: () => openCreateModal('commitment'),
            }, '+ Add Commitment'),
          ),
        ),

        h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 14 } },
          filterDefs.map((f) => {
            const active = itemFilter === f.id;
            return h('button', {
              key: f.id,
              type: 'button',
              onClick: () => setItemFilter(f.id),
              style: {
                ...styles.btn('mini', false),
                padding: '5px 11px',
                fontSize: 12.5,
                fontWeight: active ? 620 : 500,
                background: active
                  ? 'var(--foreground, #252a24)'
                  : 'color-mix(in srgb, var(--foreground, #888) 5%, transparent)',
                color: active ? 'var(--card, #f6f7f3)' : 'var(--foreground, inherit)',
              },
            }, `${f.label} (${f.count})`);
          }),
        ),

        filtered.length > 0
          ? h('div', null, filtered.map((item) => renderItemRow(item, false)))
          : h('div', {
              style: {
                padding: '24px 16px',
                textAlign: 'center',
                borderRadius: 11,
                border: '1px dashed var(--border, rgba(127, 127, 127, 0.25))',
                color: 'var(--muted-foreground, #6e776f)',
                fontSize: 13.5,
              },
            }, 'No items match this filter.'),
      ),
    );
  };

  // Tab 3: Preferences
  const renderPreferencesTab = () => {
    if (!prefs) return h('p', null, 'Loading preferences...');
    const toneIdx = Math.max(0, TONES.indexOf(prefs.tone));
    const persIdx = Math.max(0, PERSISTENCE_LEVELS.indexOf(prefs.persistence));
    const persInfo = PERSISTENCE_DETAILS[prefs.persistence] || PERSISTENCE_DETAILS.Occasional;

    const toggleRow = (label, key, subtitle = '') =>
      h('label', {
        style: {
          display: 'flex',
          alignItems: 'flex-start',
          gap: 10,
          padding: '8px 0',
          cursor: 'pointer',
        },
      },
        h('input', {
          type: 'checkbox',
          checked: Boolean(prefs[key]),
          onChange: (e) => updatePref(key, e.target.checked),
          style: { width: 16, height: 16, marginTop: 2, accentColor: '#459457' },
        }),
        h('span', { style: { flex: 1 } },
          h('span', { style: { display: 'block', fontSize: 13.5, fontWeight: 540 } }, label),
          subtitle && h('span', { style: { display: 'block', fontSize: 12, color: 'var(--muted-foreground, #6e776f)', marginTop: 2 } }, subtitle),
        ),
      );

    const selectRow = (label, key, options) =>
      h('div', {
        style: {
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: 12,
          padding: '8px 0',
          flexWrap: 'wrap',
        },
      },
        h('span', { style: { fontSize: 13.5 } }, label),
        h('select', {
          value: prefs[key] || options[0],
          onChange: (e) => updatePref(key, e.target.value),
          style: { ...styles.input, width: 'auto', minWidth: 150, padding: '6px 10px' },
        }, options.map((opt) => h('option', { key: opt, value: opt }, opt))),
      );

    return h('div', null,
      // Save bar
      h('div', {
        style: {
          ...styles.subtleCard,
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: 12,
          flexWrap: 'wrap',
          marginBottom: 16,
        },
      },
        h('span', { style: { fontSize: 13, color: dirtyPrefs ? 'var(--foreground, inherit)' : 'var(--muted-foreground, #6e776f)' } },
          dirtyPrefs ? 'You have unsaved preference changes.' : 'All preference changes saved to active profile.',
        ),
        h('button', {
          type: 'button',
          disabled: busy,
          style: styles.btn('primary', busy),
          onClick: savePreferences,
        }, busy ? 'Saving...' : 'Save Preferences'),
      ),

      // Section 1: How Hermes Follows Up
      h('section', { style: styles.card },
        h('div', { style: styles.cardHeader },
          h('div', null,
            h('h2', { style: styles.cardTitle }, 'How Hermes Follows Up'),
            h('p', { style: styles.cardHint }, 'Tune coaching voice and how persistent Butler should be. Butler decides the timing itself; these are preferences, not schedules.'),
          ),
        ),

        // Tone slider
        h('div', { style: { marginBottom: 20 } },
          h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 6 } },
            h('span', { style: { fontSize: 13.5, fontWeight: 600 } }, 'Coaching Tone'),
            h('span', { style: { fontSize: 13, fontWeight: 620 } }, prefs.tone),
          ),
          h('input', {
            type: 'range',
            min: 0,
            max: TONES.length - 1,
            step: 1,
            value: toneIdx,
            onChange: (e) => updatePref('tone', TONES[Number(e.target.value)] || 'Balanced'),
            style: { width: '100%', accentColor: '#459457', cursor: 'pointer' },
          }),
          h('div', { style: { display: 'flex', justifyContent: 'space-between', marginTop: 4, gap: 4 } },
            TONES.map((t) => h('button', {
              key: t,
              type: 'button',
              onClick: () => updatePref('tone', t),
              style: {
                border: 'none',
                background: 'transparent',
                padding: '2px 4px',
                fontSize: 12,
                cursor: 'pointer',
                fontWeight: prefs.tone === t ? 650 : 450,
                color: prefs.tone === t ? 'var(--foreground, inherit)' : 'var(--muted-foreground, #6e776f)',
              },
            }, t)),
          ),
          h('div', {
            style: {
              marginTop: 8,
              padding: '9px 12px',
              borderRadius: 9,
              background: 'color-mix(in srgb, var(--foreground, #888) 4%, transparent)',
              fontSize: 12.5,
              color: 'var(--muted-foreground, #6e776f)',
            },
          }, TONE_DESCRIPTIONS[prefs.tone] || ''),
        ),

        // Persistence slider
        h('div', null,
          h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 6 } },
            h('span', { style: { fontSize: 13.5, fontWeight: 600 } }, 'Follow-up Persistence'),
            h('span', { style: { fontSize: 13, fontWeight: 620 } }, `${prefs.persistence} · ${persInfo.badge}`),
          ),
          h('input', {
            type: 'range',
            min: 0,
            max: PERSISTENCE_LEVELS.length - 1,
            step: 1,
            value: persIdx,
            onChange: (e) => updatePref('persistence', PERSISTENCE_LEVELS[Number(e.target.value)] || 'Occasional'),
            style: { width: '100%', accentColor: '#459457', cursor: 'pointer' },
          }),
          h('div', { style: { display: 'flex', justifyContent: 'space-between', marginTop: 4, gap: 4 } },
            PERSISTENCE_LEVELS.map((p) => h('button', {
              key: p,
              type: 'button',
              onClick: () => updatePref('persistence', p),
              style: {
                border: 'none',
                background: 'transparent',
                padding: '2px 4px',
                fontSize: 12,
                cursor: 'pointer',
                fontWeight: prefs.persistence === p ? 650 : 450,
                color: prefs.persistence === p ? 'var(--foreground, inherit)' : 'var(--muted-foreground, #6e776f)',
              },
            }, p)),
          ),
          h('div', {
            style: {
              marginTop: 8,
              padding: '10px 12px',
              borderRadius: 9,
              background: prefs.persistence === 'Persistent'
                ? 'color-mix(in srgb, #459457 12%, transparent)'
                : 'color-mix(in srgb, var(--foreground, #888) 4%, transparent)',
              border: prefs.persistence === 'Persistent'
                ? '1px solid color-mix(in srgb, #459457 35%, var(--border, #666))'
                : '1px solid transparent',
              fontSize: 12.5,
            },
          },
            h('div', { style: { fontWeight: 560, marginBottom: 3 } }, `Daily Non-Negotiables: ${persInfo.nonNegotiable}`),
            h('div', { style: { color: 'var(--muted-foreground, #6e776f)' } }, `Commitments: ${persInfo.commitment}`),
          ),
        ),
      ),

      // Section 2: Guardrails & Schedule Boundaries
      h('section', { style: styles.card },
        h('div', { style: styles.cardHeader },
          h('div', null,
            h('h2', { style: styles.cardTitle }, 'Guardrails & Schedule Boundaries'),
            h('p', { style: styles.cardHint },
              `Effective timezone: ${effTz} (${stateData.effective_timezone_source || 'fallback'}) · Lifetime turn injection: ${stateData.lifetime_awareness_active ? 'active' : 'paused/off'}.`,
            ),
          ),
          !stateData.allow_gateway_injection && h('button', {
            type: 'button',
            disabled: busy,
            style: styles.btn('sage', busy),
            onClick: handleEnableBackgroundWakes,
          }, 'Enable Background Wakes'),
        ),

        h('div', {
          style: {
            padding: '9px 12px',
            borderRadius: 9,
            marginBottom: 10,
            background: stateData.allow_gateway_injection
              ? 'color-mix(in srgb, #459457 11%, transparent)'
              : 'color-mix(in srgb, #c8872b 13%, transparent)',
            fontSize: 12.5,
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            gap: 10,
            flexWrap: 'wrap',
          },
        },
          h('span', null,
            stateData.allow_gateway_injection
              ? 'Gateway wake injection (plugins.entries.butler.allow_gateway_injection) is enabled in Hermes config.'
              : 'Gateway wake injection is currently off in Hermes config — background check-ins cannot inject into chat until enabled.',
          ),
          !stateData.allow_gateway_injection && h('button', {
            type: 'button',
            disabled: busy,
            style: styles.btn('mini', busy),
            onClick: handleEnableBackgroundWakes,
          }, 'Enable Now'),
        ),

        toggleRow(
          'Enable proactive wake notifications',
          'notificationsEnabled',
          'Allows the background runner to send scheduled check-ins and follow-ups within your daily cap and quiet hours.',
        ),
        toggleRow(
          'Global Pause (pause all wakes, event capture & live turn injection)',
          'paused',
          'Immediately halts proactive check-ins and turn context injection without deleting your schedule.',
        ),
        toggleRow(
          'Inject live local-time & session-lifetime awareness into turns',
          'lifetimeAwarenessEnabled',
          'Gives Hermes accurate wall-clock time, quiet-hours awareness, and elapsed gap since your previous message.',
        ),

        h('div', {
          style: {
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
            gap: 12,
            marginTop: 10,
            paddingTop: 10,
            borderTop: '1px solid var(--border, rgba(127, 127, 127, 0.16))',
          },
        },
          h('div', null,
            h('label', { style: { display: 'block', fontSize: 12.5, fontWeight: 560, marginBottom: 5 } }, 'Daily wake cap (max proactive nudges/day)'),
            h('input', {
              type: 'number',
              min: 0,
              max: 1000,
              step: 1,
              placeholder: 'Unlimited',
              value: prefs.dailyLimit ?? '',
              onChange: (e) => updatePref('dailyLimit', e.target.value === '' ? null : Number(e.target.value)),
              style: styles.input,
            }),
          ),
          h('div', null,
            h('label', { style: { display: 'block', fontSize: 12.5, fontWeight: 560, marginBottom: 5 } }, 'Max unanswered proactive nudges'),
            h('input', {
              type: 'number', min: 0, max: 100, step: 1,
              value: prefs.maxUnanswered ?? 5,
              onChange: (e) => updatePref('maxUnanswered', Number(e.target.value)),
              style: styles.input,
            }),
            h('span', { style: { display: 'block', fontSize: 11.5, color: 'var(--muted-foreground, #6e776f)', marginTop: 3 } },
              'Stops after this many non-silent Butler follow-up replies go unanswered; silent checks do not count. A failed channel delivery may still consume the gate. 0 disables it.'),
          ),
          h('div', null,
            h('label', { style: { display: 'block', fontSize: 12.5, fontWeight: 560, marginBottom: 5 } }, 'Minimum minutes between proactive messages'),
            h('input', {
              type: 'number', min: 0, max: 1440, step: 1,
              value: prefs.minWakeGapMinutes ?? 1,
              onChange: (e) => updatePref('minWakeGapMinutes', Number(e.target.value)),
              style: styles.input,
            }),
            h('span', { style: { display: 'block', fontSize: 11.5, color: 'var(--muted-foreground, #6e776f)', marginTop: 3 } },
              'Owner-set floor between wakes; 0 disables this gate.'),
          ),
          h('div', null,
            h('label', { style: { display: 'block', fontSize: 12.5, fontWeight: 560, marginBottom: 5 } }, 'Confirmed IANA Timezone'),
            h('input', {
              type: 'text',
              placeholder: stateData.detected_system_timezone || 'America/Chicago',
              value: prefs.timezone || '',
              onChange: (e) => updatePref('timezone', e.target.value),
              style: styles.input,
            }),
            h('div', { style: { display: 'flex', gap: 6, marginTop: 6, flexWrap: 'wrap' } },
              h('button', { type: 'button', disabled: busy, style: styles.btn('mini', busy), onClick: () => detectTimezone(false, false) }, 'Use System Timezone'),
              h('button', { type: 'button', disabled: busy, style: styles.btn('mini', busy), onClick: () => detectTimezone(true, false) }, 'Verify via IP'),
            ),
          ),
        ),

        h('div', {
          style: {
            marginTop: 12,
            paddingTop: 10,
            borderTop: '1px solid var(--border, rgba(127, 127, 127, 0.16))',
          },
        },
          toggleRow(
            'Enforce Quiet Hours (defer non-urgent wakes until morning)',
            'quietEnabled',
            'Any check-in landing inside quiet hours automatically shifts to the end of the quiet window.',
          ),
          prefs.quietEnabled && h('div', { style: { display: 'flex', gap: 12, flexWrap: 'wrap', marginTop: 6 } },
            h('label', { style: { fontSize: 12.5 } },
              'Quiet start ',
              h('input', {
                type: 'time',
                value: prefs.quietStart || '22:00',
                onChange: (e) => updatePref('quietStart', e.target.value),
                style: { ...styles.input, width: 140, marginLeft: 6 },
              }),
            ),
            h('label', { style: { fontSize: 12.5 } },
              'Quiet end ',
              h('input', {
                type: 'time',
                value: prefs.quietEnd || '08:00',
                onChange: (e) => updatePref('quietEnd', e.target.value),
                style: { ...styles.input, width: 140, marginLeft: 6 },
              }),
            ),
          ),
        ),
      ),

      // Section 3: Hermes System Noise & Load Controls
      h('section', { style: styles.card },
        h('div', { style: styles.cardHeader },
          h('div', null,
            h('h2', { style: styles.cardTitle }, 'Hermes System Noise & Load Controls'),
            h('p', { style: styles.cardHint },
              'Keep Hermes feeling like a calm human texter by muting noisy engine banners, tool spam, and restart notices.',
            ),
          ),
          h('button', {
            type: 'button',
            disabled: busy,
            style: styles.btn('sage', busy),
            onClick: applyQuietPreset,
          }, 'Apply Quiet Human Texting Preset'),
        ),

        toggleRow('Sync notification & load controls to active Hermes config.yaml on save', 'applySystemControls'),
        toggleRow('Mute provider fallback, rate-limit & engine warning banners', 'suppressWarningNotifications'),
        selectRow('Tool progress messages (display.tool_progress)', 'toolProgressMode', ['off', 'new', 'all']),
        toggleRow('Send interim assistant commentary during tool chains', 'interimAssistantMessages'),
        toggleRow('Send busy / queued-turn acknowledgements', 'busyAckNotifications'),
        toggleRow('Send long-running status pings', 'longRunningNotifications'),
        selectRow('Background process notifications', 'backgroundProcessNotifications', ['off', 'error', 'concise', 'result', 'all']),
        selectRow('Memory save notifications', 'memoryNotifications', ['off', 'on', 'verbose']),
        toggleRow('Allow periodic skill-creation nudges', 'skillNudgesEnabled'),
        toggleRow('Send static Gateway online startup/restart banners on messaging platforms', 'gatewayRestartNotifications'),
        toggleRow('Stamp inbound gateway messages with local time', 'gatewayMessageTimestamps'),
        toggleRow('Concise natural status style & auto-expiring ephemeral notices', 'naturalStatusStyle'),

        stateData.hermes_live_config && stateData.hermes_live_config.available && h('div', {
          style: {
            marginTop: 10,
            padding: '8px 11px',
            borderRadius: 8,
            background: 'color-mix(in srgb, var(--foreground, #888) 4%, transparent)',
            fontSize: 12,
            color: 'var(--muted-foreground, #6e776f)',
          },
        }, `Live Hermes config: warnings_muted=${String(stateData.hermes_live_config.suppress_warning_notifications)}, tool_progress=${stateData.hermes_live_config.tool_progress}, interim=${String(stateData.hermes_live_config.interim_assistant_messages)}, skill_nudge_interval=${stateData.hermes_live_config.skill_creation_nudge_interval}, gateway_timestamps=${String(stateData.hermes_live_config.gateway_message_timestamps)}.`),
      ),

      // Section 4: Privacy & Capture Gates
      h('section', { style: styles.card },
        h('h2', { style: { ...styles.cardTitle, marginBottom: 6 } }, 'Privacy & Event Capture Gates'),
        h('p', { style: { margin: '0 0 10px', fontSize: 12.5, color: 'var(--muted-foreground, #6e776f)' } },
          'Admitted DM collection requires both the separate plugin opt-in collect_event_history and the activity switch below, with pause off. Collected messages are plaintext and are not automatically deleted. Turning either switch off stops future collection; history replay remains disabled.',
        ),
        toggleRow('Allow event collection when plugin opt-in collect_event_history is also enabled', 'activityEnabled'),
        h('div', { style: { marginTop: 12 } },
          h('button', {
            type: 'button',
            disabled: busy,
            style: styles.btn('primary', busy),
            onClick: savePreferences,
          }, busy ? 'Saving...' : 'Save Preferences'),
        ),
      ),
    );
  };

  // Add / Edit Item Modal
  const renderItemModal = () => {
    if (!itemModal) return null;
    const isNN = itemModal.kind === 'non_negotiable';
    return h('div', {
      style: styles.modalBackdrop,
      onClick: (e) => { if (e.target === e.currentTarget && !busy) setItemModal(null); },
    },
      h('form', { style: styles.modalPanel, onSubmit: handleSaveItemModal },
        h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 } },
          h('h3', { style: { margin: 0, fontSize: 17, fontWeight: 640 } },
            itemModal.mode === 'create' ? 'Add Tracked Item' : 'Edit Tracked Item',
          ),
          h('button', { type: 'button', style: styles.btn('mini', busy), onClick: () => setItemModal(null) }, '✕'),
        ),

        h('div', { style: { marginBottom: 12 } },
          h('label', { style: { display: 'block', fontSize: 12.5, fontWeight: 560, marginBottom: 5 } }, 'Type'),
          h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 } },
            h('button', {
              type: 'button',
              onClick: () => setItemModal((m) => ({ ...m, kind: 'non_negotiable' })),
              style: {
                ...styles.btn(isNN ? 'sage' : 'secondary', false),
                justifyContent: 'center',
                padding: '9px 10px',
              },
            }, 'Daily Non-Negotiable (resets every morning)'),
            h('button', {
              type: 'button',
              onClick: () => setItemModal((m) => ({ ...m, kind: 'commitment' })),
              style: {
                ...styles.btn(!isNN ? 'sage' : 'secondary', false),
                justifyContent: 'center',
                padding: '9px 10px',
              },
            }, 'One-off / Recurring Commitment'),
          ),
        ),

        h('div', { style: { marginBottom: 12 } },
          h('label', { style: { display: 'block', fontSize: 12.5, fontWeight: 560, marginBottom: 5 } }, 'Title'),
          h('input', {
            type: 'text',
            required: true,
            value: itemModal.title,
            onChange: (e) => setItemModal((m) => ({ ...m, title: e.target.value })),
            placeholder: isNN ? 'e.g. 45m deep work before checking inbox' : 'e.g. Send revised launch deck to Sarah',
            style: styles.input,
          }),
        ),

        h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 12 } },
          h('div', null,
            h('label', { style: { display: 'block', fontSize: 12.5, fontWeight: 560, marginBottom: 5 } }, 'Optional First Check-in'),
            h('input', {
              type: 'datetime-local',
              value: itemModal.due_at,
              onChange: (e) => setItemModal((m) => ({ ...m, due_at: e.target.value })),
              style: styles.input,
            }),
          ),
          h('div', null,
            h('label', { style: { display: 'block', fontSize: 12.5, fontWeight: 560, marginBottom: 5 } }, 'Optional Hard Deadline'),
            h('input', {
              type: 'datetime-local',
              value: itemModal.deadline_at,
              onChange: (e) => setItemModal((m) => ({ ...m, deadline_at: e.target.value })),
              style: styles.input,
            }),
          ),
        ),
        h('p', { style: { margin: '-4px 0 12px', fontSize: 12, color: 'var(--muted-foreground, #6e776f)' } },
          `Leave "First Check-in" blank and Butler will wake right away and choose the follow-up timing itself in ${effTz} (your ${(prefs && prefs.persistence) || 'Occasional'} setting is a preference hint, not a schedule).`,
        ),

        h('div', { style: { marginBottom: 12 } },
          h('label', { style: { display: 'block', fontSize: 12.5, fontWeight: 560, marginBottom: 5 } }, 'Optional Prep Hint / Action Context'),
          h('input', {
            type: 'text',
            value: itemModal.prep_hint,
            onChange: (e) => setItemModal((m) => ({ ...m, prep_hint: e.target.value })),
            placeholder: "e.g. 'Draft the email first before nudging me'",
            style: styles.input,
          }),
        ),

        h('div', { style: { marginBottom: 16 } },
          h('label', { style: { display: 'block', fontSize: 12.5, fontWeight: 560, marginBottom: 5 } }, 'Optional Notes'),
          h('input', {
            type: 'text',
            value: itemModal.notes,
            onChange: (e) => setItemModal((m) => ({ ...m, notes: e.target.value })),
            placeholder: 'Any extra context or link...',
            style: styles.input,
          }),
        ),

        h('div', { style: { display: 'flex', justifyContent: 'flex-end', gap: 8 } },
          h('button', { type: 'button', style: styles.btn('secondary', busy), onClick: () => setItemModal(null) }, 'Cancel'),
          h('button', { type: 'submit', disabled: busy, style: styles.btn('primary', busy) },
            busy ? 'Saving...' : (itemModal.mode === 'create' ? 'Save Item' : 'Update Item'),
          ),
        ),
      ),
    );
  };

  // 3-Step Quick Setup Modal
  const renderQuickSetupModal = () => {
    if (!wizardOpen) return null;
    return h('div', {
      style: styles.modalBackdrop,
      onClick: (e) => { if (e.target === e.currentTarget && !busy) setWizardOpen(false); },
    },
      h('div', { style: styles.modalPanel },
        h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 } },
          h('span', { style: { fontSize: 12, fontWeight: 620, color: 'var(--muted-foreground, #6e776f)', textTransform: 'uppercase', letterSpacing: '0.05em' } },
            `Quick Setup · Step ${wizardStep} of 4`,
          ),
          h('button', { type: 'button', disabled: busy, 'aria-label': 'Close quick setup', style: styles.btn('mini', busy), onClick: () => setWizardOpen(false) }, '✕'),
        ),
        error && h('p', { role: 'alert', style: { color: 'var(--destructive, #c33)' } }, error),

        wizardStep === 1 && h('div', null,
          h('h3', { style: { margin: '0 0 6px', fontSize: 18 } }, '1. Confirm your timezone & calm texting defaults'),
          h('p', { style: { margin: '0 0 14px', fontSize: 13, color: 'var(--muted-foreground, #6e776f)' } },
            'Hermes uses your local clock so morning check-ins and quiet hours land accurately.',
          ),
          h('label', { style: { display: 'block', fontSize: 12.5, fontWeight: 560, marginBottom: 5 } }, 'IANA Timezone'),
          h('div', { style: { display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap' } },
            h('input', {
              type: 'text',
              value: wizardForm.timezone,
              onChange: (e) => setWizardForm((f) => ({ ...f, timezone: e.target.value })),
              placeholder: stateData.detected_system_timezone || 'America/Chicago',
              style: { ...styles.input, flex: 1, minWidth: 200 },
            }),
            h('button', { type: 'button', disabled: busy, style: styles.btn('secondary', busy), onClick: () => detectTimezone(false, true) }, 'Use System TZ'),
            h('button', { type: 'button', disabled: busy, style: styles.btn('secondary', busy), onClick: () => detectTimezone(true, true) }, 'Verify via IP'),
          ),

          h('label', { style: { display: 'flex', gap: 10, alignItems: 'flex-start', marginBottom: 12, cursor: 'pointer' } },
            h('input', {
              type: 'checkbox',
              checked: wizardForm.quietEnabled,
              onChange: (e) => setWizardForm((f) => ({ ...f, quietEnabled: e.target.checked })),
              style: { marginTop: 3, accentColor: '#459457' },
            }),
            h('span', { style: { fontSize: 13 } },
              h('strong', null, 'Protect Quiet Hours'),
              ` (${wizardForm.quietStart}–${wizardForm.quietEnd}) — defer late nudges to morning.`,
            ),
          ),

          h('label', { style: { display: 'flex', gap: 10, alignItems: 'flex-start', marginBottom: 18, cursor: 'pointer' } },
            h('input', {
              type: 'checkbox',
              checked: wizardForm.applyQuietPreset,
              onChange: (e) => setWizardForm((f) => ({ ...f, applyQuietPreset: e.target.checked })),
              style: { marginTop: 3, accentColor: '#459457' },
            }),
            h('span', { style: { fontSize: 13 } },
              h('strong', null, 'Apply Quiet Human Texting Preset (recommended)'),
              ' — mutes noisy warning banners, tool progress spam, and restart notices.',
            ),
          ),

          h('div', { style: { display: 'flex', justifyContent: 'flex-end', gap: 8 } },
            h('button', { type: 'button', style: styles.btn('primary', false), onClick: () => setWizardStep(2) }, 'Next: Tone & Cadence →'),
          ),
        ),

        wizardStep === 2 && h('div', null,
          h('h3', { style: { margin: '0 0 6px', fontSize: 18 } }, '2. Pick how Hermes follows up'),
          h('p', { style: { margin: '0 0 12px', fontSize: 13, color: 'var(--muted-foreground, #6e776f)' } },
            'Choose your coaching voice and how persistently Hermes checks in until your daily non-negotiables are done.',
          ),

          h('div', { style: { marginBottom: 14 } },
            h('div', { style: { fontSize: 12.5, fontWeight: 600, marginBottom: 6 } }, 'Tone'),
            h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap' } },
              TONES.map((t) => h('button', {
                key: t,
                type: 'button',
                onClick: () => setWizardForm((f) => ({ ...f, tone: t })),
                style: styles.btn(wizardForm.tone === t ? 'sage' : 'secondary', false),
              }, t)),
            ),
          ),

          h('div', { style: { marginBottom: 16 } },
            h('div', { style: { fontSize: 12.5, fontWeight: 600, marginBottom: 6 } }, 'Persistence'),
            h('div', { style: { display: 'flex', flexDirection: 'column', gap: 7 } },
              PERSISTENCE_LEVELS.map((p) => {
                const selected = wizardForm.persistence === p;
                const info = PERSISTENCE_DETAILS[p];
                return h('div', {
                  key: p,
                  role: 'radio',
                  'aria-checked': selected,
                  tabIndex: 0,
                  onClick: () => setWizardForm((f) => ({ ...f, persistence: p })),
                  onKeyDown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setWizardForm((f) => ({ ...f, persistence: p })); } },
                  style: {
                    padding: '10px 12px',
                    borderRadius: 10,
                    cursor: 'pointer',
                    border: selected
                      ? '1.5px solid #459457'
                      : '1px solid var(--border, rgba(127, 127, 127, 0.25))',
                    background: selected
                      ? 'color-mix(in srgb, #459457 13%, transparent)'
                      : 'transparent',
                  },
                },
                  h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 } },
                    h('span', { style: { fontWeight: 620, fontSize: 13.5 } },
                      p === 'Persistent' ? 'Persistent — Keep at it; timing stays model-chosen ★' : p,
                    ),
                    h('span', { style: { fontSize: 11.5, color: 'var(--muted-foreground, #6e776f)' } }, info.badge),
                  ),
                  h('div', { style: { fontSize: 12, color: 'var(--muted-foreground, #6e776f)', marginTop: 3 } }, info.nonNegotiable),
                );
              }),
            ),
          ),

          h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8 } },
            h('button', { type: 'button', style: styles.btn('secondary', false), onClick: () => setWizardStep(1) }, '← Back'),
            h('button', { type: 'button', style: styles.btn('primary', false), onClick: () => setWizardStep(3) }, 'Next: Daily Non-Negotiables →'),
          ),
        ),

        wizardStep === 3 && h('div', null,
          h('h3', { style: { margin: '0 0 6px', fontSize: 18 } }, '3. Set your Daily Non-Negotiables'),
          h('p', { style: { margin: '0 0 12px', fontSize: 13, color: 'var(--muted-foreground, #6e776f)' } },
            'Add 3+ daily habits or anchor priorities that reset every morning. Leave blank if you prefer to add them later.',
          ),

          h('div', { style: { display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 10 } },
            wizardForm.nonNegotiables.map((val, idx) => {
              const placeholders = [
                'e.g. 45m deep work on core product before messages',
                'e.g. Inbox zero & top 3 customer replies',
                'e.g. 30m workout / walk outside',
              ];
              return h('input', {
                key: idx,
                type: 'text',
                value: val,
                placeholder: placeholders[idx] || `Daily non-negotiable #${idx + 1}`,
                'aria-label': `Daily non-negotiable ${idx + 1}`,
                onChange: (e) => {
                  const value = e.target.value;
                  setWizardForm((f) => {
                    const next = [...f.nonNegotiables];
                    next[idx] = value;
                    return { ...f, nonNegotiables: next };
                  });
                },
                style: styles.input,
              });
            }),
          ),

          h('button', {
            type: 'button',
            style: { ...styles.btn('mini', false), marginBottom: 16 },
            onClick: () => setWizardForm((f) => ({ ...f, nonNegotiables: [...f.nonNegotiables, ''] })),
          }, '+ Add another'),

          h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8 } },
            h('button', { type: 'button', style: styles.btn('secondary', busy), onClick: () => setWizardStep(2) }, '← Back'),
            h('button', {
              type: 'button',
              disabled: busy,
              style: styles.btn('primary', busy),
              onClick: loadPromptReview,
            }, 'Next: Optional Prompt Review →'),
          ),
        ),
        wizardStep === 4 && h('div', null,
          h('h3', null, '4. Optional prompt review'),
          h('p', null, 'Review and optionally apply selected changes before enabling. Existing text must stay unchanged; edit or remove the suggested additions. AGENTS.md is project-scoped and requires choosing a project folder separately.'),
          h('section', { style: { ...styles.subtleCard, marginBottom: 12 } },
            h('strong', null, 'Project AGENTS.md'),
            h('p', null, 'Choose the actual project working directory. This is optional; no project file is changed until you create it.'),
            h('button', { type: 'button', disabled: busy, style: styles.btn('secondary', busy), onClick: chooseProject }, 'Choose project folder'),
            projectFolder && h('p', null, `Selected: ${projectFolder.path}`),
            projectFolder && projectExisting !== null && h('div', null,
              h('p', null, 'AGENTS.md already exists. It will not be overwritten; review the recommendation and edit the file manually if desired.'),
              h('details', null, h('summary', null, 'Existing AGENTS.md'), h('pre', { style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } }, projectExisting)),
            ),
            projectFolder && h('label', null, 'Suggested project instructions (editable)', h('textarea', { value: projectText, disabled: busy, rows: 7, style: { ...styles.input, fontFamily: 'monospace' }, onChange: (e) => setProjectText(e.target.value) })),
            projectFolder && projectExisting === null && h('button', { type: 'button', disabled: busy || !projectText.trim(), style: styles.btn('secondary', busy), onClick: applyProjectAgents }, 'Create AGENTS.md in selected folder'),
            projectMessage && h('p', { role: 'status' }, projectMessage),
          ),
          reviewError && h('p', { role: 'alert', style: { color: 'var(--destructive, #c33)' } }, reviewError),
          busy && h('p', null, 'Working…'),
          reviewApplied && h('p', { role: 'status' }, 'Selected prompt changes applied. You can now enable the assistant.'),
          !busy && reviewItems && !reviewItems.length && h('p', null, 'No prompt changes available.'),
          !reviewApplied && reviewItems && reviewItems.map((item) => h('section', { key: item.id, style: { ...styles.subtleCard, marginBottom: 12 } },
            h('label', null,
              h('input', { type: 'checkbox', checked: item.selected, disabled: busy, onChange: (e) => setReviewItems((items) => items.map((row) => row.id === item.id ? { ...row, selected: e.target.checked } : row)) }),
              ' ', h('strong', null, item.path),
            ),
            item.description && h('p', null, item.description),
            h('details', null, h('summary', null, 'Current content'), h('pre', { style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } }, item.current)),
            h('details', null, h('summary', null, item.proposed === item.originalProposed ? 'Proposed diff' : 'Original proposed diff (edited below)'), h('pre', { style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } }, item.diff || 'No differences.')),
            h('label', null, 'Proposed content (editable)', h('textarea', { value: item.proposed, disabled: busy, rows: 8, style: { ...styles.input, fontFamily: 'monospace' }, onChange: (e) => setReviewItems((items) => items.map((row) => row.id === item.id ? { ...row, proposed: e.target.value } : row)) })),
          )),
          h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' } },
            h('button', { type: 'button', disabled: busy, style: styles.btn('secondary', busy), onClick: () => setWizardStep(3) }, '← Back'),
            !reviewApplied && h('button', { type: 'button', disabled: busy, style: styles.btn('secondary', busy), onClick: loadPromptReview }, 'Refresh review'),
            !reviewApplied && h('button', { type: 'button', disabled: busy || !reviewItems || !reviewItems.some((item) => item.selected), style: styles.btn('secondary', busy), onClick: applyPromptReview }, 'Apply selected changes'),
            h('button', { type: 'button', disabled: busy, style: styles.btn('primary', busy), onClick: handleCompleteWizard }, busy ? 'Working…' : (reviewApplied ? 'Enable Butler' : 'Skip review and enable')),
          ),
        ),
      ),
    );
  };

  const navTabs = [
    ['overview', 'Your overview'],
    ['items', `Priorities & commitments (${stateData.responsibilities.length})`],
    ['activity', 'Activity'],
    ['preferences', dirtyPrefs ? 'Preferences •' : 'Preferences'],
  ];
  const statusText = !prefs ? 'Connecting…'
    : prefs.paused ? 'Check-ins paused'
      : setupNeeded ? 'Setup needed'
        : quota.in_quiet_hours ? 'Quiet hours'
          : 'Your assistant is available';

  return h('div', { className: 'ovc' },
    h('style', null, OVC_CSS),
    h('main', { className: 'ovc-app' },
      h('header', { className: 'ovc-top' },
        h('div', { className: 'ovc-brand' }, h('span', { className: 'ovc-brand-mark' }, '❧'), ' BUTLER'),
        h('div', { className: 'ovc-right' },
          h('span', { className: `ovc-status ${statusBadge.tone}` }, h('i', { className: 'ovc-dot' }), statusText),
          h('button', { type: 'button', className: 'ovc-toplink', onClick: () => goTab('preferences') }, 'Preferences \u00a0⚙'),
        ),
      ),
      h('nav', { className: 'ovc-menu', 'aria-label': 'Butler sections' },
        navTabs.map(([id, label]) => h('button', {
          key: id,
          type: 'button',
          className: activeTab === id ? 'active' : '',
          'aria-current': activeTab === id ? 'page' : undefined,
          onClick: () => goTab(id),
        }, label)),
      ),
      activeTab === 'overview' && h('section', { className: 'ovc-hero', style: { backgroundImage: `url(${HERO_IMAGE})` } },
        h('div', { className: 'ovc-hero-content' },
          h('div', { className: 'ovc-eyebrow' }, todayLabel().replace(', ', ' \u00a0·\u00a0 ')),
          h('h1', null, `${greeting()}${prefs && prefs.displayName ? `, ${prefs.displayName}` : ''}.`),
          h('p', null, 'Consider it taken care of.'),
          h('div', { className: 'ovc-flourish', 'aria-hidden': 'true' }, '—— ❦ ——'),
        ),
      ),
      h('div', { className: 'ovc-quick' },
        Btn('✧ \u00a0 Check in now', () => handleCheckInNow(null), 'primary'),
        Btn('＋ Add a priority', () => openCreateModal('non_negotiable')),
        Btn('＋ Add a commitment', () => openCreateModal('commitment')),
        Btn('✦ \u00a0 Quick setup', openQuickSetup),
      ),
      h('div', { className: 'ovc-alerts' },
        error && h('div', { role: 'alert', className: 'ovc-alert err' },
          h('span', null, error), h('button', { type: 'button', className: 'ovc-link', onClick: () => setError('') }, 'Dismiss')),
        message && h('div', { role: 'status', className: 'ovc-alert ok' },
          h('span', null, message), h('button', { type: 'button', className: 'ovc-link', 'aria-label': 'Dismiss message', onClick: () => setMessage('') }, '✕')),
        busy && h('div', { className: 'ovc-busy' }, 'Working…'),
      ),
      !prefs && !error && h('p', { className: 'ovc-muted ovc-pad' }, 'Loading your assistant…'),
      prefs && activeTab === 'overview' && renderOverviewTab(),
      prefs && activeTab === 'items' && h('div', { className: 'ovc-content ovc-legacy' }, renderItemsTab()),
      prefs && activeTab === 'activity' && renderActivityTab(),
      prefs && activeTab === 'preferences' && h('div', { className: 'ovc-content ovc-legacy' }, renderPreferencesTab()),
      h('div', { className: 'ovc-goldline' }),
      h('div', { className: 'ovc-footer' }, 'With every detail considered.'),
    ),
    renderItemModal(),
    renderQuickSetupModal(),
  );
}

function ProfilePage({ ctx }) {
  const profile = useValue(host.state.profile);
  return h(Page, { key: String(profile), ctx });
}

export default {
  id: 'butler',
  name: 'Butler - Proactive Assistant',
  defaultEnabled: false,
  register(ctx) {
    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      data: { path: '/proactive-assistant' },
      render: () => h(ProfilePage, { ctx }),
    });
    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      data: { path: '/proactive-assistant', label: 'Butler', codicon: 'sparkle' },
    });
  },
};
