import { ROUTES_AREA, SIDEBAR_NAV_AREA, host, useValue } from '@hermes/plugin-sdk';
import { createElement as h, useEffect, useState } from 'react';

function Page({ ctx }) {
  const [prefs, setPrefs] = useState(null);
  const [responsibilities, setResponsibilities] = useState([]);
  const [meta, setMeta] = useState({
    detected_system_timezone: '',
    effective_timezone: '',
    effective_timezone_source: '',
    lifetime_awareness_active: false,
    hermes_live_config: null,
  });
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const refreshState = (aliveGuard = () => true) =>
    ctx.rest.get('/api/plugins/overseer/state')
      .then(data => {
        if (!aliveGuard()) return;
        setPrefs(data.settings);
        setResponsibilities(data.responsibilities || []);
        setMeta({
          detected_system_timezone: data.detected_system_timezone || '',
          effective_timezone: data.effective_timezone || '',
          effective_timezone_source: data.effective_timezone_source || '',
          lifetime_awareness_active: Boolean(data.lifetime_awareness_active),
          hermes_live_config: data.hermes_live_config || null,
        });
      })
      .catch(err => { if (aliveGuard()) setError(err?.message || 'Could not load Overseer state.'); });

  useEffect(() => {
    let alive = true;
    setPrefs(null); setResponsibilities([]); setMessage(''); setError('');
    refreshState(() => alive);
    return () => { alive = false; };
  }, [ctx]);

  const set = (key, value) => { setPrefs(current => ({ ...current, [key]: value })); setMessage(''); };

  const detectTimezone = async (useIp) => {
    setBusy(true); setError(''); setMessage('');
    try {
      const res = await ctx.rest.post('/api/plugins/overseer/detect-timezone', { use_ip: Boolean(useIp) });
      const zone = res.suggested_timezone || '';
      if (zone) {
        setPrefs(current => ({ ...current, timezone: zone }));
        setMessage(useIp
          ? `Detected timezone ${zone} (IP: ${res.detected_ip_timezone || 'n/a'}, OS: ${res.detected_system_timezone || 'n/a'}). Click Save preferences to confirm.`
          : `Detected OS timezone ${zone}. Click Save preferences to confirm.`);
      } else {
        setError('Could not auto-detect an IANA timezone; enter one manually (e.g. America/Chicago).');
      }
    } catch (err) {
      setError(err?.message || 'Timezone detection failed.');
    } finally {
      setBusy(false);
    }
  };

  const applyQuietPreset = async () => {
    setBusy(true); setError(''); setMessage('');
    try {
      const res = await ctx.rest.post('/api/plugins/overseer/preset', { preset: 'quiet_human' });
      setPrefs(res.settings);
      await refreshState();
      setMessage('Applied Quiet Human Texting preset and synced notification/load controls to Hermes config.');
    } catch (err) {
      setError(err?.message || 'Could not apply preset.');
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    setBusy(true); setError(''); setMessage('');
    try {
      const saved = await ctx.rest.put('/api/plugins/overseer/settings', prefs);
      setPrefs(saved);
      await refreshState();
      setMessage(saved.applySystemControls
        ? 'Saved preferences and synced system notification/load controls to Hermes config.'
        : 'Saved profile preferences (enable Sync to Hermes config to apply system notification/load switches).');
    } catch (err) {
      setError(err?.message || 'Could not save preferences.');
    } finally {
      setBusy(false);
    }
  };

  const box = { border: '1px solid var(--border, #444)', borderRadius: 8, padding: 16, marginBottom: 16 };
  const rowStyle = { display: 'block', marginBottom: 10 };
  const btnRow = { display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 };
  const field = (label, key, type = 'text', extra = {}) => h('label', { style: rowStyle }, label, ' ',
    h('input', { type, value: prefs[key] ?? '', onChange: e => set(key, type === 'number' ? (e.target.value === '' ? null : Number(e.target.value)) : e.target.value), ...extra }));
  const select = (label, key, options) => h('label', { style: rowStyle }, label, ' ',
    h('select', { value: prefs[key], onChange: e => set(key, e.target.value) }, options.map(opt => h('option', { key: opt, value: opt }, opt))));
  const toggle = (label, key) => h('label', { style: rowStyle },
    h('input', { type: 'checkbox', checked: Boolean(prefs[key]), onChange: e => set(key, e.target.checked) }), ' ', label);

  return h('div', { style: { padding: 24, maxWidth: 820 } },
    h('h1', null, 'Overseer — Proactive Assistant'),
    error && h('p', { role: 'alert' }, error),
    !prefs && !error && h('p', null, 'Loading...'),

    prefs && h('section', { style: box },
      h('h2', null, 'Time & Session Lifetime Awareness'),
      h('p', null, `Active timezone: ${meta.effective_timezone || 'UTC'} (${meta.effective_timezone_source || 'fallback'}). OS detected: ${meta.detected_system_timezone || 'unavailable'}. Live pre_llm_call injection: ${meta.lifetime_awareness_active ? 'active' : 'paused/off'}.`),
      h('p', null, 'Injects the user local wall-clock time, quiet-hours status, session age, and exact elapsed time since the previous user and assistant messages into each primary turn so pacing stays accurate across rapid chats and overnight gaps.'),
      toggle('Enable live local-time & session-lifetime awareness in turns', 'lifetimeAwarenessEnabled'),
      field('Confirmed IANA timezone', 'timezone', 'text', { placeholder: meta.detected_system_timezone || 'America/Chicago' }),
      h('div', { style: btnRow },
        h('button', { type: 'button', disabled: busy, onClick: () => detectTimezone(false) }, 'Use System Timezone'),
        h('button', { type: 'button', disabled: busy, onClick: () => detectTimezone(true) }, 'Verify Timezone via IP'))),

    prefs && h('section', { style: box },
      h('h2', null, 'System Notification & Load Controls'),
      h('p', null, 'Controls noisy built-in Hermes notices through public Hermes config keys (provider fallback/rate-limit warnings, tool progress spam, interim narration, skill creation nudges, memory notices, and static Gateway online restart banners).'),
      h('div', { style: btnRow },
        h('button', { type: 'button', disabled: busy, onClick: applyQuietPreset }, 'Apply Quiet Human Texting Preset')),
      toggle('Sync notification & load controls to active Hermes config.yaml on save', 'applySystemControls'),
      toggle('Mute provider fallback, rate-limit & engine warning banners (suppress_warning_notifications + credits_notices)', 'suppressWarningNotifications'),
      select('Tool progress messages (display.tool_progress)', 'toolProgressMode', ['off', 'new', 'all']),
      toggle('Send interim assistant commentary during tool chains (display.interim_assistant_messages)', 'interimAssistantMessages'),
      toggle('Send busy / queued-turn acknowledgements (display.busy_ack_enabled)', 'busyAckNotifications'),
      toggle('Send long-running status pings (display.long_running_notifications)', 'longRunningNotifications'),
      select('Background process notifications (display.background_process_notifications)', 'backgroundProcessNotifications', ['off', 'error', 'concise', 'result', 'all']),
      select('Memory save notifications (display.memory_notifications)', 'memoryNotifications', ['off', 'on', 'verbose']),
      toggle('Allow periodic skill-creation nudges (skills.creation_nudge_interval)', 'skillNudgesEnabled'),
      toggle('Send static Gateway online startup/restart banners on messaging platforms (gateway_restart_notification)', 'gatewayRestartNotifications'),
      toggle('Stamp inbound gateway messages with local time (gateway.message_timestamps.enabled)', 'gatewayMessageTimestamps'),
      toggle('Concise natural status style & auto-expiring ephemeral notices', 'naturalStatusStyle'),
      h('p', null, 'Note: Hermes core supports toggling static Gateway online restart banners on or off per platform; custom restart banner templates are not exposed as a public hook, while natural status style governs assistant-authored updates.'),
      meta.hermes_live_config && meta.hermes_live_config.available && h('p', null,
        `Live Hermes config: warnings_muted=${String(meta.hermes_live_config.suppress_warning_notifications)}, tool_progress=${meta.hermes_live_config.tool_progress}, interim=${String(meta.hermes_live_config.interim_assistant_messages)}, skill_nudge_interval=${meta.hermes_live_config.skill_creation_nudge_interval}, gateway_timestamps=${String(meta.hermes_live_config.gateway_message_timestamps)}.`)),

    h('section', { style: box }, h('h2', null, 'Tracked Responsibilities (read-only)'),
      responsibilities.length ? h('ul', null, responsibilities.map(item => h('li', { key: item.id }, h('strong', null, item.title), ' — ', item.status, ' — ', item.due_at, ' (', item.timezone, ')'))) : h('p', null, 'No responsibilities found.')),

    prefs && h('section', { style: box }, h('h2', null, 'Coaching & Privacy Preferences'),
      h('p', null, 'Admitted DM collection requires both the separate plugin opt-in collect_event_history and the activity switch below, with pause off. The plugin opt-in is not controlled or displayed here. Collected messages are plaintext and are not automatically deleted. Turning either switch off stops future collection but does not erase existing records; history replay remains disabled.'),
      h('p', null, 'Pause stops both event collection and live turn lifetime injection. Scheduled wake delivery remains disabled pending authenticated public session APIs.'),
      select('Tone', 'tone', ['Gentle', 'Supportive', 'Balanced', 'Direct', 'Strict']), select('Persistence', 'persistence', ['Minimal', 'Occasional', 'Regular', 'Persistent']),
      field('Daily limit', 'dailyLimit', 'number', { min: 0, max: 1000, step: 1 }),
      toggle('Quiet hours preference', 'quietEnabled'), field('From', 'quietStart', 'time'), field('To', 'quietEnd', 'time'),
      toggle('Pause Overseer (stops event collection & live turn injection)', 'paused'),
      toggle('Allow event collection when plugin opt-in is also enabled', 'activityEnabled'), toggle('Wake notifications preference (wake scheduler disabled)', 'notificationsEnabled'),
      h('button', { type: 'button', disabled: busy, onClick: save }, 'Save preferences'), h('p', { role: 'status' }, message)));
}
function ProfilePage({ ctx }) {
  const profile = useValue(host.state.profile);
  return h(Page, { key: String(profile), ctx });
}
export default {
  id: 'overseer', name: 'Proactive Assistant', defaultEnabled: false,
  register(ctx) {
    ctx.register({ id: 'page', area: ROUTES_AREA, data: { path: '/proactive-assistant' }, render: () => h(ProfilePage, { ctx }) });
    ctx.register({ id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/proactive-assistant', label: 'Proactive Assistant', codicon: 'sparkle' } });
  },
};
