# Proposal: opt-in delivery observation for Hermes

**Status:** design for upstream discussion; not implemented or supported today. No monkeypatching or edits to a live Hermes installation. Source inspection is of a local hermes-agent checkout at `3b421578588243f920886e6fe79a6faa87ea026e`; line numbers below are navigation aids, not a claim about other versions. Official documentation index: https://hermes-agent.nousresearch.com/docs/llms.txt.

## Goal and limits

Provide an owner-authorized observer with a fact about an outbound message **after a participating transport reports its result**, including cron result and failure notices. This is not an event for generated text or a guarantee a person saw it. Initial coverage is explicitly the instrumented gateway/cron text paths below, not all visible messages. An adapter acknowledgment is not proof of display or reading. No claim is made for Desktop frontend events, CLI/TUI rendering, browser/dashboard UI, bot/room chat, arbitrary plugin adapters, direct provider SDK sends, media, edits, stream drafts, or independently running processes unless and until each producer explicitly adopts the contract. A standalone process only emits if it runs an instrumented Hermes version, has its own consent/config and loads an observer; the gateway hook does not magically observe another process.

## Confirmed local trace and proposed boundaries

| Local source and observed behavior | Proposed integration boundary and scope |
|---|---|
| `hermes_cli/plugins.py:111` lists supported hooks, including stream/interim and incoming `pre_gateway_dispatch`; those are not a post-delivery fact. | Add a new observer hook `on_delivery_event` to the supported plugin contract, not an alias for generation or inbound events. Check plugin dispatch and discovery behavior in each process before implementation. |
| `cron/scheduler_delivery.py:1826` `_deliver_result` resolves targets and, for external workers, hands off to `cron.delivery_queue.enqueue_and_wait`; otherwise it wraps, extracts media and redacts text before per-target routing. `cron/delivery_queue.py:328` `drain` invokes the send then terminalizes the queue row; `gateway/run.py:4430` drains per profile. | Do **not** emit on enqueue, queue terminalization, mirror/seed, or merely on job completion. Carry execution/job and origin metadata with the delivery operation into the actual per-target sender. The gateway drain must emit under the correct profile. Cron system/failure notices use `for_failure` and the actual resolved target. |
| `cron/scheduler_delivery.py:1291` `_live_send_text` sends through `gateway.delivery.DeliveryRouter._deliver_to_platform`; it distinguishes filtering, success, missing evidence, timeout in flight, and `PartialDeliveryError`. `:1495` handles live delivery; `:1563` `_standalone_send` calls `tools.send_message_tool._send_to_platform`. | For cron, instrument the actual physical-send completion in `gateway/delivery.py`'s `DeliveryRouter._deliver_to_platform` (transport send around `:334`) and the standalone `_send_to_platform` path, with an explicit operation context propagated by cron. Avoid a second event at `_live_send_text` or queue drain. Verify exact chunk/result behavior during implementation: aggregate only acknowledged chunks and report partial/unknown honestly. A timeout with a still-running send is **unknown**, not success; do not resend to obtain an event. |
| `gateway/platforms/base.py` contains direct `self.send` calls (e.g. `:4329`), and concrete adapters implement/override sending. `tools/send_message_tool.py:556,606,688` has adapter, chunk, and standalone routes. | These bypass a single router. Phase 1 does **not** claim coverage for every ordinary gateway message or every tool send. To extend coverage, inventory each direct send, streaming/edit and adapter override, then route each through an explicit shared delivery-observation helper with the same operation context; instrument concrete successful transport boundaries, not abstract `BasePlatformAdapter.send` alone. Prevent router/helper double emission. |
| `cron/scheduler_delivery.py:714` `_deliver_to_bot_chat` is a separate path; `_deliver_result` also treats room targets specially. | Bot/room delivery requires separate owner-side instrumentation and tests; excluded initially. Likewise `_send_media_via_adapter` is separate; metadata-only media support is future work. |

**Unknown / must verify before merge:** exact transport return evidence and chunk identity per adapter, whether each standalone sender can supply reliable message IDs, how stream edits and partial visibility map to a final message, and whether hook dispatch actually runs in each worker/profile context. A successful `SendResult` without a provider receipt remains an acknowledgment of uncertain strength. Existing adapters differ; no synthetic IDs should be presented as provider IDs.

## Proposed v1 contract

`on_delivery_event(event)` is a new, immutable, observer-only plugin hook. This is a proposed public API, not a currently available hook. Each event represents one logical text delivery to one actual target, not one job or one chunk; include acknowledged chunks if a multi-chunk operation partly succeeds. One logical operation may have multiple targets and therefore multiple events. Invoke after the final known transport outcome; a timeout whose underlying operation may continue yields at most an `unknown` event, never a fabricated success. If an eventual result becomes known, emit a distinct revision with the same `delivery_id` and monotonically increasing `revision`; consumers upsert, not append duplicate observations. If revisions cannot be implemented reliably, document unknown as terminal and omit later claims.

Example, **illustrative fields only** (not a current API):

```json
{
  "schema_version": 1,
  "event_id": "opaque-stable-event-id",
  "delivery_id": "opaque-stable-logical-delivery-id",
  "revision": 1,
  "occurred_at": "2026-11-11T12:00:00Z",
  "profile_id": "opaque-profile-id",
  "surface": "gateway",
  "source_kind": "cron_result",
  "outcome": "accepted",
  "evidence": "provider_message_id",
  "target": {"platform": "telegram", "chat_id": "opaque", "thread_id": null},
  "session_id": null,
  "cron": {"job_id": "opaque", "execution_id": "opaque"},
  "provider_message_ids": ["opaque"],
  "text": null,
  "text_state": "not_authorized"
}
```

- `source_kind`: `cron_result`, `cron_failure`, or `other` (only when an instrumented producer can establish it). IDs and target/thread are nullable when unknown; never infer them from message text. `surface` names the actual instrumented transport scope.
- `outcome`: `accepted` (adapter reports success; not read), `partial`, `failed`, `suppressed`, or `unknown`. `evidence`: `provider_message_id`, `adapter_ack`, or `none`; do not upgrade a bare acknowledgment. Omit raw provider response, exception text and credentials. Only accepted or evidenced partial content is eligible for an observer to treat as a visible-message candidate; failed, suppressed and unknown are not proof of visibility.
- Default `text` is null. An explicit separate content grant may supply only the final post-policy, post-redaction text actually sent, or only the confirmed subset for partial sends. `text_state` distinguishes `not_authorized`, `available`, and `unavailable`; unknown is not an empty message. Do not include attachment bytes, local paths, tokens, raw events or hidden prompts. Text can still contain private information even after redaction.
- Assign an opaque `delivery_id` before the first attempt, stable across retries, queue handoff and fallback for the same profile/target/logical message. `event_id` is stable for the pair `(delivery_id, revision)`; persist IDs at the operation's durable boundary where one exists. Retries and queue drain must not create a second logical delivery. Different targets and distinct messages need distinct IDs; don't dedupe by text or cron execution alone. Consumer dedupe is profile-scoped by `(delivery_id, revision)`; retain a bounded TTL, upsert latest revision, and handle out-of-order arrival. No exactly-once claim: best-effort dispatch can lose events on crash or backpressure; replay, if later added, must preserve IDs and consent at replay time.

## Security and operational behavior

Off by default, per profile and per named plugin, with separate explicit grants for metadata and message content and optional platform/target/source allowlists. Scope consent to the actual destination/profile; a primary gateway servicing a satellite must not expose satellite events to a primary observer without that satellite's grant. Consent must precede collection; revoke immediately for future deliveries, clear pending content, and require explicit policy for retained data. This is a privacy boundary, not an authorization mechanism for sending messages. Group chats and third-party content warrant explicit owner consent and applicable participant/privacy policy.

Callbacks cannot alter, veto, delay, retry or cause a send. Queue bounded immutable copies outside the transport loop, with per-observer isolation, timeout, exception containment and bounded concurrency; overflow drops observer events with aggregate counters, never message delivery. Protect against reentrancy: observer-triggered sends must not recursively feed the same observer without an explicit policy. No event text in routine logs or metrics. Protect any opted-in spool with profile-scoped OS permissions, encryption where supported, strict retention/deletion, size limits and no remote export without separate consent. The minimal v1 is in-memory best effort, no durable content spool; loss on restart is explicit.

## Rollout and acceptance test

1. Upstream review of contract, evidence mapping, profile isolation, callback threading, privacy and ID lifecycle; add hook documentation and feature/version detection. Disabled means zero observable behavior change. Ship behind an opt-in flag; no monkeypatch or private-method interception by Butler.
2. Implement cron gateway live, restart-safe queued handoff, and explicitly instrumented standalone text sends; provide source/context propagation and one ownership point for each physical path. Publish a precise coverage matrix and adapter-specific evidence. Do not market this as universal.
3. In an isolated test profile with a real instrumented gateway, a controlled test channel and a recording observer, create and run an actual cron result job and a failure-notice job. Assert the channel receives the expected redacted text and observer gets exactly one logical event per actual target, with correct `source_kind`, profile, execution/job IDs, transport evidence and authorized content. Repeat with external worker/queue drain, gateway restart, and standalone cron process independently configured; compare provider receipts, queue state and observer IDs. Exercise multi-target, chunk partial failure, filtered/suppressed output, in-flight timeout, failed transport, fallback, duplicate queue drain, missing receipt, disabled/revoked consent, profile isolation, slow/throwing/recursive observer and overflow. Assert no false accepted event, duplicate logical event or extra send; original delivery latency/outcome remains unaffected. Also assert Desktop rendering and uninstrumented independent processes produce no event, documenting the gap. This is an acceptance plan, not a test result.
4. Extend one surface at a time only after its inventory and end-to-end proof; add capability reporting and coverage tests for direct adapter calls, tool sends, media, streaming and Desktop separately. Until then, any consumer must label its observations incomplete and never use absence of an event as evidence that no user-visible message exists.
