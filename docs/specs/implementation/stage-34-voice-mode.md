# Stage 34: Voice Mode (TTS, STT, Conversation Mode)

## Status

DONE

## Goal

Ship the work described in [`../31-voice-mode.md`](../31-voice-mode.md) in
nine reviewable phases:

1. **TTS provider plumbing** — `LLMProvider.synthesize()`, executor JSON-RPC,
   `InferenceRouter.route_synthesize()`, LiteLLM-backed implementation,
   `text_to_speech` routing slot.
2. **TTS + STT API endpoints** — `POST /api/v1/tts/synthesize` with caching,
   `POST /api/v1/stt/transcribe` for the web mic flow, shared audio
   preprocessing module.
3. **Agent voice configuration** — `AgentLLMConfig.voice`,
   `LLMProviderConfig.default_voice`, system fallback settings,
   `voice_resolution.py`.
4. **Settings UI** — `text_to_speech` row in routing matrix, model-aware
   voice picker for system default.
5. **Agent form voice picker** — voice field in `AgentForm.svelte` mirroring
   the settings picker.
6. **Speaker button on assistant messages** — `ChatMessage.svelte` toolbar
   button, single-instance audio player store.
7. **Microphone button + record-preview-send** — `MicRecorder.svelte`,
   audio attachment kind, transcribe-on-send pipeline.
8. **Sentence-buffered streaming** — server-side sentence buffer,
   `tts_sentence_ready` WS frame, `enable_tts/disable_tts` inbound frames.
9. **Conversation mode overlay** — full-bleed overlay, VAD-driven mic
   loop, sequential audio queue, transcript drawer, pre-flight checks.

Each phase ships with migrations + bootstrap helpers (where needed) +
tests + UI in the same PR. Phases land in order; later phases can be
drafted in parallel with the ones in front of them.

## Dependencies

- [`../31-voice-mode.md`](../31-voice-mode.md)
- [`../05-integrations.md`](../05-integrations.md) (existing STT routing
  pattern: `LLMProvider.transcribe()`, `InferenceRouter.route_transcribe()`,
  executor `llm.transcribe`)
- [`../02-agent-model.md`](../02-agent-model.md) (`AgentLLMConfig`)
- [`../09-ui-ux.md`](../09-ui-ux.md) (chat composer, message toolbar)
- [`../10-api-spec.md`](../10-api-spec.md) (REST + WebSocket surface)
- Stages 0–16 complete (foundational infra including provider routing
  and artifact store)

## Scope

### In scope

- `LLMProvider.synthesize()` Protocol method and `LiteLLMProvider`
  implementation.
- Executor JSON-RPC `llm.synthesize` and `InferenceRouter.route_synthesize()`.
- New `text_to_speech` slot in `ModelRoutingPolicy`; route eligibility
  filter on the server and in the UI.
- New `tts_cache` metadata table + bootstrap + Alembic migration.
- `POST /api/v1/tts/synthesize` and `POST /api/v1/stt/transcribe` REST
  endpoints.
- Shared `cognis/audio/preprocessing.py` module lifted from
  `cognis/channels/inbound.py`.
- `AgentLLMConfig.voice` and `LLMProviderConfig.default_voice` fields,
  serializers, and form handling.
- New settings keys `tts.default_voice`, `tts.enabled`,
  `tts.cache_ttl_days`.
- `cognis/core/voice_resolution.py` and shared call sites.
- Settings page TTS routing row + voice picker.
- Agent form voice picker.
- `ChatMessage.svelte` speaker button + global single-instance audio store.
- Composer microphone button, recording UI, audio attachment pill,
  transcribe-on-send pipeline.
- WebSocket `enable_tts`/`disable_tts` inbound frames, `tts_sentence_ready`
  outbound frame.
- `cognis/core/sentence_buffer.py` and `WebSocketTurnObserver` integration.
- Conversation mode overlay component, VAD integration, audio queue
  helper, transcript drawer, pre-flight checks.
- Tests: unit, contract, UI.

### Out of scope

- Local Piper binary hosting. (Use HTTP-based providers only.)
- Voice cloning / voice creation.
- TTS during workflow deliverables.
- Realtime WebSocket audio transport (sentence-buffered HTTP only).
- Per-agent TTS provider override (only voice override).
- Voice preview cards in UI (deferred).

## Phased rollout inside this stage

| Phase | Name | Notes |
|-------|------|-------|
| 34.1 | TTS provider plumbing | Protocol method, LiteLLM impl, executor RPC, routing slot |
| 34.2 | TTS + STT API endpoints | `tts/synthesize` (cached), `stt/transcribe`, shared audio preprocessing |
| 34.3 | Agent voice configuration | Domain fields, voice resolution helper, system settings |
| 34.4 | Settings UI | `text_to_speech` routing row + system voice picker |
| 34.5 | Agent form voice picker | Voice field in `AgentForm.svelte` |
| 34.6 | Speaker button | Per-message toolbar button + global audio store |
| 34.7 | Microphone + record preview | Composer mic button + iMessage-style preview + STT-on-send |
| 34.8 | Sentence-buffered streaming | Server sentence buffer + WS frames |
| 34.9 | Conversation mode | Overlay UI + VAD loop + audio queue |

## Deliverables

### 34.1 TTS provider plumbing

Domain:

- `cognis/models/config.py` — add `TextToSpeechResult` (mirrors
  `SpeechToTextResult`). Add `text_to_speech: ModelRoutingEntry` to
  `ModelRoutingPolicy`. Add `default_voice: str | None = None` to
  `LLMProviderConfig`.

Provider Protocol:

- `cognis/providers/base.py` — add `synthesize()` to `LLMProvider`
  Protocol, mirroring the `transcribe()` signature.

LiteLLM implementation:

- `cognis/providers/llm/litellm.py` — implement `LiteLLMProvider.synthesize()`
  modeled exactly on `transcribe()` (lines 1173-1277):
  - Resolve model via `_resolve_model_for_task("text_to_speech", ...)`.
  - Resolve voice via `voice_resolution.resolve_voice(...)`.
  - When `_should_route_to_executor(provider)` is true, delegate to
    `self._inference_router.route_synthesize(...)`.
  - Otherwise call `await litellm.aspeech(model=..., input=text,
    voice=..., response_format=..., speed=..., api_base=..., api_key=...)`.
  - Wrap with the shared retry helper and circuit breaker.
  - Return `TextToSpeechResult`.

Executor side:

- `cognis/executor/inference.py` — add `synthesize()` handler modeled on
  `transcribe()` (lines 247-315). Calls `litellm.aspeech()` and returns
  base64-encoded audio + metadata.
- `cognis/executor/runner.py` (or wherever JSON-RPC dispatch lives) — wire
  `llm.synthesize` to the new handler.

Inference router:

- `cognis/providers/llm/inference_router.py` — add `route_synthesize()`
  modeled on `route_transcribe()` (lines 168-205). Selects executor by
  `executor_labels` matching, sends JSON-RPC, decodes base64.

Settings route filter:

- `cognis/api/routes/settings.py` — add `_looks_like_tts_model()`
  (mirrors `_looks_like_transcription_model` line 126-128). Match
  substrings `tts`, `speech-1`, `eleven`, `piper`. Extend
  `_route_model_is_eligible()` (line 131-141) for
  `task_type="text_to_speech"`.

Tests:

- `tests/unit/test_litellm_synthesize.py` — mock `litellm.aspeech`, verify
  voice resolution, response shape, executor routing path, retry behavior.
- `tests/unit/test_inference_router_synthesize.py` — mock executor WS,
  verify JSON-RPC and base64 decode.
- `tests/unit/test_api_contracts.py` — `TextToSpeechResult` round-trip,
  `ModelRoutingPolicy.text_to_speech` round-trip.

### 34.2 TTS + STT API endpoints

DB + bootstrap:

- `cognis/store/migrations/versions/<rev>_tts_cache.py` — create
  `tts_cache` table: `(message_id, voice, model, artifact_id, owner_email,
  created_at)`, primary key `(message_id, voice, model)`.
- `cognis/bootstrap.py` — `_ensure_tts_cache_table()` registered in
  `run_schema_bootstrap()`.
- `cognis/store/queries.py` — `get_tts_cache_entry`,
  `insert_tts_cache_entry`, `delete_expired_tts_cache_entries`.

Shared audio preprocessing:

- New module `cognis/audio/preprocessing.py`. Move helpers from
  `cognis/channels/inbound.py:113-204` (`_normalized_audio_filename`,
  `_transcode_audio_for_stt`, `_prepare_audio_for_stt`,
  `SUPPORTED_AUDIO_MIME_TYPES`). Update channels module to import from
  here. Behavior unchanged.

TTS endpoint:

- `cognis/api/routes/tts.py` — `POST /api/v1/tts/synthesize`:
  - Validate request (text length cap, optional `message_id`,
    `agent_id`, `voice`, `format`, `speed`).
  - Resolve voice via `voice_resolution.resolve_voice(...)`.
  - When `message_id` provided: look up `tts_cache`. On hit, return
    `cached=true` with a fresh signed URL. On miss, synthesize, save
    bytes to `ArtifactStore` namespace `"tts"`, insert cache row,
    return `cached=false`.
  - When `message_id` is missing: synthesize and return a one-shot
    signed URL (no cache row).
  - Honor `tts.enabled`; return 503 with a clear error when disabled.
- `cognis/api/models.py` — `TtsSynthesizeRequest`, `TtsSynthesizeResponse`.

STT endpoint:

- `cognis/api/routes/stt.py` — `POST /api/v1/stt/transcribe`:
  - Accept multipart `file` or JSON `{"artifact_id": "..."}`.
  - Validate MIME / size via the shared helpers.
  - Normalize via the shared `_prepare_audio_for_stt` (matches channel
    behavior, including executor-side ffmpeg routing).
  - Call `LLMProvider.transcribe(...)`.
  - Return `{text, language, duration_seconds, model}`.
- `cognis/api/models.py` — `SttTranscribeResponse`.

Wiring:

- `cognis/api/app.py` — register both routers under `/api/v1`.
- `cognis/api/routes/system.py` (or equivalent reconciler hook) — add a
  TTS-cache pruning task using `tts.cache_ttl_days`.

Tests:

- `tests/unit/test_tts_route.py` — cache hit/miss, signed URL response,
  `tts.enabled=false` behavior, voice resolution from agent vs settings,
  rejection on missing TTS routing.
- `tests/unit/test_stt_route.py` — multipart and `artifact_id` paths,
  MIME validation, transcribe failure surfaces.
- `tests/unit/test_audio_preprocessing.py` — round-trip from existing
  channel tests against the lifted module.
- `tests/unit/test_api_contracts.py` — request/response coverage.
- `tests/unit/test_ui_contract_sync.py` — TS interfaces in
  `ui/src/lib/types/api.ts`.

### 34.3 Agent voice configuration

Domain:

- `cognis/models/agent.py` — add `voice: str | None = None` to
  `AgentLLMConfig` (existing field block, no new schema).

Voice resolution helper:

- `cognis/core/voice_resolution.py` (new) — `resolve_voice(*, agent,
  provider, explicit) -> str`. Implements the fallback chain documented
  in spec 31. Pure function; no I/O beyond the `tts.default_voice` setting
  read passed in by the caller.

Settings keys:

- New keys read via the existing settings helpers:
  - `tts.default_voice` (string, default `null`)
  - `tts.enabled` (bool, default `true`)
  - `tts.cache_ttl_days` (int, default `30`)
- `cognis/config.py` — `COGNIS_TTS_CACHE_TTL_DAYS` env var seeds initial
  value if the row is absent.

Tests:

- `tests/unit/test_voice_resolution.py` — explicit > agent > provider >
  system default > hard default.
- `tests/unit/test_agent_definition_round_trip.py` — `voice` field
  serializes through the existing JSON column.

### 34.4 Settings UI

UI:

- `ui/src/routes/(app)/settings/+page.svelte` — extend
  `ROUTING_METADATA` (line 100-141) with the `text_to_speech` entry.
  Update `emptyModelRouting()` and `emptyRoutingForm()` (line 119-141).
  Add `looksLikeTtsModel()` mirroring `looksLikeTranscriptionModel()`
  (line 430-505); extend `routeModelOptions()` for TTS.
- Below the TTS model row, render a voice picker. Use a small helper
  `ui/src/lib/voices.ts` (new) that maps `(provider, model)` to a list
  of known voices (OpenAI: `alloy, ash, ballad, coral, echo, fable, nova,
  onyx, sage, shimmer, verse`); fall back to a free-text input. Persist
  to the `tts.default_voice` setting through the existing settings API.
- Add a `tts.enabled` toggle in the same panel.

Tests:

- Snapshot/contract tests for the routing matrix entries.
- Component test for `looksLikeTtsModel()` and the voice picker.

### 34.5 Agent form voice picker

UI:

- `ui/src/lib/components/agents/AgentForm.svelte` — below the model
  picker (line 761-794), add a Voice field. Reuse the helper from 34.4.
  Add an explicit "Use system default" option. Apply existing
  `canEditField()` gating for shared agents.
- TS interface `Agent` in `ui/src/lib/types/api.ts` extended with
  `llm_config.voice`.

Tests:

- Component test: voice field saves into `agent.llm_config.voice`,
  respects shared-agent gating.
- `tests/unit/test_ui_contract_sync.py` updated.

### 34.6 Speaker button on assistant messages

Audio store:

- `ui/src/lib/stores/audio-player.ts` (new) — small Svelte store with a
  single `current` audio element. `play(url, key)` stops any other
  playback before starting new. Exposes reactive `currentKey` and
  `isPlaying` for UI binding.

Component:

- `ui/src/lib/components/ChatMessage.svelte` — import `Volume2` and
  `Square` icons. Add `speaking` state derived from the audio store.
  Insert speaker button **before** the copy button in the assistant
  footer toolbar (line 264-294). Click handler:
  1. If the current message key is playing → call `stop()` from the store.
  2. Otherwise → POST `/api/v1/tts/synthesize` with `{message_id,
     agent_id}` and the connection's chosen voice; play the returned
     audio URL through the store.
- Hide the button when `tts.enabled=false`. Disable with tooltip when
  TTS routing is unconfigured.

Tests:

- Component test: toggle behavior, single-instance enforcement,
  disabled-state rendering.
- Mock fetch test for the synthesize call shape.

### 34.7 Microphone button and record-preview-send

Components:

- `ui/src/lib/components/composer/MicRecorder.svelte` (new) —
  `MediaRecorder`-based recorder. States: `idle` (mic icon), `recording`
  (red dot, timer pill, stop icon). Prefers
  `audio/webm;codecs=opus`; falls back to `audio/mp4` on Safari.
  `getUserMedia` requested on first activation; denial path shows a
  toast and hides the button for the session.
- `ui/src/lib/components/composer/ComposerAttachments.svelte` (existing,
  extended) — render `kind === "audio_recording"` as a play / pause /
  delete pill with duration. Use a local Blob URL for client-side
  playback before send.

Composer wiring:

- `ui/src/routes/(app)/chat/[conversationId]/+page.svelte` — add the
  mic button on the leading edge of the composer pill alongside the
  paperclip (line 3523-3582). On stop, upload to
  `POST /api/v1/artifacts/upload` with the recorded blob and append the
  resulting `{artifact_id, kind:"audio_recording", filename, duration}`
  to `composerAttachments`.
- Send pipeline: when an audio recording is present in attachments,
  call `/api/v1/stt/transcribe` with the `artifact_id`, replace the
  composer body with the transcript (or append, when text was typed),
  drop the audio recording from the outgoing turn payload, and submit
  via `wsClient.sendMessage(...)` as text only. STT failure surfaces
  inline ("Couldn't transcribe — try again or type instead") and
  blocks send.

Backend:

- No backend changes required. The `POST /api/v1/artifacts/upload` route
  already accepts `audio/*` (cognis/api/routes/artifacts.py:20-29).

Tests:

- `MicRecorder.test.ts` — mocked `MediaRecorder`, start/stop, blob
  upload, attachment append.
- Composer integration test — recording + send → STT call → text
  message submission with no audio attachment in the outgoing turn.
- E2E flow test under `tests/integration/` (gated on a real STT
  provider availability flag).

### 34.8 Sentence-buffered streaming

Sentence buffer:

- `cognis/core/sentence_buffer.py` (new) — `SentenceBuffer` class.
  Methods: `feed(token: str) -> list[str]` (returns any newly completed
  sentences) and `flush() -> str | None`. Respects code-block boundaries
  (do not emit sentences inside ``` fences). Strips markdown formatting
  from emitted sentences (links, bold, italics, headings) — pass plain
  text to TTS. Minimum sentence length to avoid emitting one-word frames.

WebSocket integration:

- `cognis/api/websocket.py` — add inbound message types `enable_tts` and
  `disable_tts` to the message loop (line 944-1027). Track
  `connection.tts_enabled: bool` and `connection.tts_voice: str | None`.
- `WebSocketTurnObserver` (line 171-374) — wrap `on_token` to feed a
  per-message `SentenceBuffer` when `tts_enabled` is true. On each
  completed sentence, emit a `tts_sentence_ready` frame
  `{type, message_id, sentence_index, text}`. On `on_message_complete`,
  flush the buffer (emit any trailing partial sentence) and reset.

Tests:

- `tests/unit/test_sentence_buffer.py` — boundary detection, code-block
  exclusion, markdown stripping, flush behavior.
- `tests/unit/test_websocket_tts.py` — `enable_tts`/`disable_tts`
  state transitions; `tts_sentence_ready` frames emitted only when
  enabled; correct `sentence_index` ordering across streamed tokens.

### 34.9 Conversation mode

Audio queue helper:

- `ui/src/lib/utils/audio-queue.ts` (new) — sequential `Audio()` queue.
  `enqueue(blobUrl)`, `clear()`, `isPlaying`, `onIdle()` callback.

Component:

- `ui/src/lib/components/chat/ConversationMode.svelte` (new) — modal
  overlay. States: `listening`, `processing`, `speaking`. Animated orb,
  collapsible transcript drawer, bottom controls (mute, switch to text,
  end conversation).

Lifecycle:

- On open: pre-flight check (TTS + STT routes configured, mic permission
  granted). When unmet, show inline setup prompt instead of starting.
- Send `{type:"enable_tts", voice}` over the chat WebSocket (voice =
  active agent's voice ?? system default).
- Start `MediaRecorder`. Use `@ricky0123/vad-web` (npm dep) for VAD; fall
  back to `AudioContext.AnalyserNode` RMS-threshold + 1.5s silence window
  on browsers where the WASM init fails.
- On VAD silence: stop recording, upload + transcribe, submit turn via
  `wsClient.sendMessage(...)`.
- On `tts_sentence_ready`: fire `POST /api/v1/tts/synthesize` with
  `{message_id, sentence_index, text, voice}` and enqueue the audio.
  Without a `message_id` (rare race), fall back to a transient call.
- On `audioQueue.onIdle()` after `message_complete`: orb → `listening`,
  restart the recorder.
- On close, ESC, or "End conversation": send `{type:"disable_tts"}`,
  stop recorder, clear audio queue, close overlay.

Wiring:

- New entry button in the chat header (icon: `Headphones`). Hidden when
  TTS or STT routes are not configured (with a tooltip linking to
  settings).
- Reuse `ui/src/lib/stores/audio-player.ts` for "single audio playing"
  invariant (entering conversation mode pauses any single-message
  playback).

Tests:

- `ConversationMode.test.ts` — VAD mock, state transitions,
  `enable_tts/disable_tts` lifecycle, queue ordering.
- `tests/integration/test_voice_conversation.py` — end-to-end mock with
  fake mic input → transcribe → assistant stream → sentence frames →
  TTS responses → playback completion → mic restart.

## Acceptance criteria

- All deliverables in 34.1–34.9 land with migrations + bootstrap +
  tests + UI in the same PR per phase.
- `LLMProvider.synthesize()` exists in the Protocol; `LiteLLMProvider`
  implements it with controller-side and executor-routed paths.
- `text_to_speech` routing entry round-trips through API and UI; the
  settings page filters models by TTS-eligibility.
- `POST /api/v1/tts/synthesize` returns a signed URL for the
  synthesized audio; second call with the same `(message_id, voice,
  model)` returns `cached=true` and serves from the artifact store.
- `POST /api/v1/stt/transcribe` returns the transcript for an uploaded
  audio file or referenced artifact; channels and the new endpoint
  share the audio preprocessing module.
- Per-agent `voice` field round-trips through the agent API and form;
  the system fallback is honored when an agent has no voice set.
- Speaker button toggles per-message playback; only one message plays
  at a time across the workspace; the button is hidden when TTS is
  disabled and disabled with a tooltip when TTS routing is unconfigured.
- Microphone button records, previews, and on send transcribes →
  submits the resulting text without an audio attachment in the
  outgoing turn.
- `tts_sentence_ready` frames are emitted only when `tts_enabled=true`
  on the connection, and a sentence buffer correctly skips code blocks
  and strips markdown.
- Conversation mode runs the listen → transcribe → speak → listen loop
  end-to-end with VAD; pre-flight check blocks entry when TTS/STT
  routes or mic permission are unavailable.
- `tests/unit/test_api_contracts.py` and
  `tests/unit/test_ui_contract_sync.py` pass.
- Telemetry counters in spec 31 emit and contain no message text or
  audio bytes.

## Risks and mitigations

- **LiteLLM `aspeech` provider coverage gaps.** Verified at
  implementation time. If a target provider is not yet supported by
  LiteLLM, fall back to direct HTTP from `LiteLLMProvider.synthesize()`
  using the existing `httpx` client (mirrors how `transcribe()` falls
  back to direct `/v1/audio/transcriptions` HTTP calls today).
- **Browser audio format inconsistency.** MediaRecorder produces
  `audio/webm;codecs=opus` on Chromium/Firefox and `audio/mp4` on
  Safari. The shared preprocessing helper normalizes via ffmpeg before
  STT; documenting the ffmpeg requirement on controllers and
  STT-routed executors prevents production surprises.
- **iOS Safari audio autoplay restrictions.** Auto-played audio after
  streaming can be blocked without a user gesture. Conversation mode
  shows a one-time "Tap to enable audio" prompt on first activation
  per session. The single-message speaker button is always
  user-initiated and unaffected.
- **VAD false positives in noisy environments.** The fallback RMS
  threshold is exposed via a hidden setting for debugging; the WASM
  VAD library handles noise robustly enough for v1. A future spec may
  add server-side endpointing.
- **Cache key collisions across voice changes.** `(message_id, voice,
  model)` cache key prevents collisions when an agent's voice changes;
  message edits already produce a new `message_id`. Voice changes
  produce a fresh cache row on next play.
- **Cost protection.** No per-user TTS quota in v1. A future cost
  guardrails spec covers TTS alongside LLM and image generation.
- **Streaming sentence detection for code-heavy assistant output.**
  The sentence buffer skips fenced code blocks; assistant messages
  that are mostly code emit no `tts_sentence_ready` frames, which is
  the correct behavior (no value reading code aloud).
- **Channel module import breakage.** Lifting helpers into
  `cognis/audio/preprocessing.py` is a refactor with risk; covered by
  the existing channel inbound test suite plus a new
  `test_audio_preprocessing.py`. Run channel contract tests in CI for
  phase 34.2.

## Stage exit

Mark this stage file DONE. Add a follow-up note that
`voice_resolution.resolve_voice` is the canonical voice resolver going
forward and that any new voice-aware surface (e.g. notifications, channel
adapters) must route through it.
