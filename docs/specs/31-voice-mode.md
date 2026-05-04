# Cognis: Voice Mode (TTS, STT, Conversation Mode)

## Purpose

This spec defines first-class **voice** support across Cognis: text-to-speech
synthesis of assistant messages, speech-to-text dictation in the web chat, and
a bidirectional **conversation mode** that combines both into a hands-free
assistant experience.

The design treats TTS and STT as routable model tasks alongside the existing
LLM, classifier, evaluator, image generation, and STT routes. They share the
same provider abstraction (`LLMProvider`) and the same controller/executor
location semantics. Per-agent voice selection layers on top of a system-wide
default voice.

Related specs: [`05-integrations.md`](05-integrations.md),
[`02-agent-model.md`](02-agent-model.md), [`09-ui-ux.md`](09-ui-ux.md),
[`10-api-spec.md`](10-api-spec.md), [`11-deployment.md`](11-deployment.md).

## Motivation

STT already exists in Cognis but only as an inbound channel feature: when a
Signal user sends a voice note, the channels module transcribes it before
dispatching the turn. The web UI has no microphone button and no way for a
user to dictate a message. There is no TTS pathway anywhere in the codebase —
assistant messages cannot be read aloud, and there is no notion of an agent
voice.

Voice mode in the web UI is increasingly expected by users. Once the
infrastructure exists, **conversation mode** (continuous push-to-listen,
sentence-buffered TTS playback, then auto-listen) becomes a natural extension
that turns Cognis into a usable hands-free assistant for tasks like driving,
cooking, walking, or accessibility scenarios where typing is not ideal.

The constraint shaping this design: TTS providers vary enough (OpenAI's
`/v1/audio/speech`, ElevenLabs voices, Azure Neural Voices, Bedrock Polly,
self-hosted Piper HTTP servers) that hand-rolling per-provider HTTP clients
is wasteful. LiteLLM already abstracts these behind a single `aspeech()`
SDK call. Cognis routes through LiteLLM and inherits all currently and
future-supported TTS providers.

## Design Principles

### 1. TTS is a routable task, just like STT

Mirror the existing STT routing surface end to end:

- A new `text_to_speech` slot in `ModelRoutingPolicy`.
- A new `LLMProvider.synthesize()` method on the provider Protocol.
- Executor-side routing through `InferenceRouter.route_synthesize()` when the
  provider is configured with `location="executor"`.
- Settings UI route picker filtered to TTS-eligible models.

This keeps the mental model simple and means future TTS backends added to
LiteLLM appear in Cognis automatically.

### 2. LiteLLM as the single backend

`LiteLLMProvider` is the only TTS implementation. It supports OpenAI,
ElevenLabs, Azure, Bedrock, and any other provider LiteLLM ships. Cognis does
not run local Piper binaries; self-hosted setups run Piper as an HTTP server
that exposes an OpenAI-compatible endpoint and connects through the standard
OpenAI-compatible provider config. This matches Cognis's existing posture for
LLMs and STT (we consume APIs, we don't host inference engines in-process).

### 3. Per-agent voice with system fallback

Voice is part of agent identity. Each agent may carry an optional
`voice` string (e.g. `"alloy"`, `"nova"`, an ElevenLabs voice ID). When
absent, the system-wide `tts.default_voice` setting applies. When that is also
absent, a hard-coded provider-aware default is used (`"alloy"` for OpenAI).

Voice is metadata only — it is never part of the LLM prompt and never
exposed to the model. It is a delivery decoration set per agent.

### 4. STT-first for web mic input

Web microphone recordings are always transcribed to text before submission.
The transcript replaces the user message body; the audio blob is uploaded as
a transient artifact, used only for transcription, and not retained as a
turn attachment. This matches the channel inbound path and produces a
predictable cost profile, predictable model behavior across all LLM
providers, and a recoverable text record in conversation history.

The audio attachment route through `turn_scheduler.py` (which already accepts
audio input for audio-capable models) is preserved for non-voice attachments
and for future expansion, but is not used by the mic UI.

### 5. iMessage-style record-preview-send

Recording a voice message in the web UI follows the iMessage flow:

1. Tap the mic button → recording starts; live duration timer + waveform
   appear in the composer attachments tray.
2. Tap stop → the recording becomes an audio attachment pill with play /
   pause / delete and a duration display.
3. The user can keep typing additional text alongside the recording.
4. Tap send → the controller transcribes the audio, replaces the message body
   with the transcript, drops the audio from the outgoing turn, and submits
   normally.

This reuses the existing `composerAttachments` plumbing rather than
introducing a parallel audio-only flow.

### 6. Cached TTS playback

Each click on the speaker button on an assistant message could otherwise
re-synthesize the same text. Synthesized audio is cached as an artifact under
namespace `"tts"` keyed by `(message_id, voice, model)`. First click
synthesizes and caches; subsequent clicks stream from the cache. Cache rows
have a TTL (default 30 days) and are invalidated when the message content
changes (rare).

### 7. Conversation mode is bidirectional and first-class

Voice is not just two new buttons. The product offers an explicit
**Conversation Mode** that turns chat into an audio-first interaction:

- Tap a dedicated Conversation button → full-bleed overlay opens.
- Microphone activates immediately; client-side VAD detects end of speech.
- Server transcribes and submits the turn.
- Assistant streams; tokens are sentence-buffered and synthesized as soon as
  each sentence is complete; audio plays in sequence.
- When playback finishes, the mic auto-restarts; the loop continues.
- Mute, switch back to text, or close to exit.

Conversation mode is opt-in per session — TTS streaming only happens when
the client signals it. This avoids paying TTS cost for users who never want
voice output.

### 8. Sentence-buffered streaming for low latency

Waiting for an entire assistant message before starting playback adds many
seconds of perceived latency. In conversation mode, the controller emits a
new WebSocket frame `tts_sentence_ready` for each completed sentence (tracked
by a small server-side sentence buffer that respects code-block boundaries
and strips markdown formatting). The client requests TTS synthesis for each
sentence and queues the audio for sequential playback.

For manual single-message playback (the speaker button), no streaming is
used — the message is already complete, and a single synthesis call is
simpler and more cache-friendly.

### 9. Voice is metadata, not authority

A few things voice is **not**:

- Voice does not influence agent personality or memory. It is a delivery
  decoration only.
- Voice is never an LLM prompt attribute. The agent does not "know" its
  voice (and could not reliably control it via prompt anyway).
- TTS does not run during workflow steps for delivered deliverables. The
  channel deliverable contract from
  [`21-workflow-deliverables.md`](21-workflow-deliverables.md) is unchanged.
  Voice mode is a presentation layer over assistant messages in a
  conversation, not a part of workflow output semantics.

### 10. Speaker button vs. conversation mode

The speaker button on a single message and conversation mode are distinct
UX modes:

| Feature | Speaker button | Conversation mode |
|---|---|---|
| Trigger | Per-message toolbar | Dedicated overlay |
| TTS scope | One message | All assistant messages in the session |
| Cache behavior | Always cache by `message_id` | Cache when `message_id` known; transient otherwise |
| Streaming | Synthesize whole message | Sentence-buffered streaming |
| Mic input | None | Continuous with VAD |
| Exit | Click stop | Close overlay or end conversation |

Both modes use the same `/api/v1/tts/synthesize` endpoint. The conversation
mode adds the mic loop and VAD on top.

## Domain Model

### `TextToSpeechResult`

```python
class TextToSpeechResult(BaseModel):
    audio_bytes: bytes
    content_type: str               # "audio/mpeg" | "audio/opus" | "audio/wav" | ...
    model: str
    voice: str
    duration_seconds: float | None
```

Mirrors `SpeechToTextResult` in shape and lives in `cognis/models/config.py`.

### `ModelRoutingPolicy` extension

```python
class ModelRoutingPolicy(BaseModel):
    default: ModelRoutingEntry
    classifier: ModelRoutingEntry
    compaction: ModelRoutingEntry
    evaluator: ModelRoutingEntry
    speech_to_text: ModelRoutingEntry
    text_to_speech: ModelRoutingEntry      # NEW
    image_generation: ModelRoutingEntry
    attachment_analysis: ModelRoutingEntry
```

A `text_to_speech` row in the existing `model_routing` table (no schema
change) selects the provider/model used by the TTS endpoint. The
`ModelRoutingEntry` shape is reused unchanged.

### `LLMProviderConfig` extension

```python
class LLMProviderConfig(BaseModel):
    # ... existing fields ...
    default_voice: str | None = None       # NEW; provider-level fallback voice
```

Persisted inside the existing `llm_providers.config` JSON column. No schema
change.

### `AgentLLMConfig` extension

```python
class AgentLLMConfig(BaseModel):
    provider_id: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    model_routing: dict[str, str] | None = None
    voice: str | None = None               # NEW; per-agent voice override
```

Persisted inside the existing `agents.llm_config` JSON column. No schema
change.

### TTS cache rows

Cached audio is stored in the artifact store under namespace `"tts"`.
Object id is derived deterministically from `(message_id, voice, model)`:

```python
def tts_cache_object_id(message_id: str, voice: str, model: str) -> str:
    digest = hashlib.sha256(f"{message_id}|{voice}|{model}".encode()).hexdigest()[:32]
    return f"tts_{digest}"
```

The artifact store handles signed-URL generation, retention, and deletion.
A small `tts_cache` metadata row tracks `(message_id, voice, model,
artifact_id, created_at)` for fast lookup and TTL-based pruning. Pruning
runs as part of the existing background reconciliation loop, governed by
the new `tts.cache_ttl_days` setting (default 30).

## Provider Contract

### `LLMProvider.synthesize()`

```python
async def synthesize(
    self,
    text: str,
    *,
    voice: str,
    model: str | None = None,
    task_type: str = "text_to_speech",
    response_format: str = "mp3",          # mp3 | opus | aac | flac | wav | pcm
    speed: float = 1.0,
) -> TextToSpeechResult: ...
```

Behaviors:

- Resolve provider via `model_routing.text_to_speech` unless an explicit
  `model` is passed. The resolution path is identical to STT.
- Resolve API base, API key, and any provider-specific config through the
  existing helpers used by `transcribe()` and `complete()`.
- When the provider is configured with `location="executor"`, delegate to
  `InferenceRouter.route_synthesize(...)`. The executor receives the same
  resolved arguments and runs `litellm.aspeech()` locally.
- Wrap the call with the shared retry helper used elsewhere in the provider
  (exponential backoff + jitter) and the LiteLLM circuit breaker.

### Executor JSON-RPC method

A new JSON-RPC method `llm.synthesize` mirrors the existing `llm.transcribe`
shape (request: text, voice, model, response_format, speed; response:
`{audio_b64, content_type, model, voice, duration_seconds}`). The
controller's `InferenceRouter.route_synthesize()` selects an executor by
`executor_labels` matching, sends the request, and decodes the audio from
base64 into bytes.

### LiteLLM behavior

`litellm.aspeech()` already supports OpenAI, ElevenLabs, Azure, and Bedrock
TTS endpoints. The `LiteLLMProvider.synthesize()` implementation:

1. Resolves the model string and voice (per the fallback chain).
2. Calls `await litellm.aspeech(model=prefixed_model, input=text,
   voice=voice, response_format=response_format, speed=speed,
   api_base=..., api_key=...)`.
3. Reads `result.read()` (or equivalent) into bytes.
4. Determines `content_type` from `response_format`.
5. Returns `TextToSpeechResult`.

Edge case: providers with non-OpenAI-shaped voice IDs (ElevenLabs voice IDs,
custom Azure neural voices) pass through verbatim because LiteLLM forwards
the `voice` parameter to the underlying provider unchanged.

### Voice resolution

```python
async def resolve_voice(
    *,
    agent: AgentDefinition | None,
    provider: LLMProviderConfig | None,
    explicit: str | None = None,
) -> str:
    if explicit:
        return explicit
    if agent and agent.llm_config and agent.llm_config.voice:
        return agent.llm_config.voice
    if provider and provider.default_voice:
        return provider.default_voice
    system_default = await get_setting("tts.default_voice", default=None)
    if system_default:
        return system_default
    return "alloy"   # hard fallback
```

The same helper is used by `/api/v1/tts/synthesize` and by the conversation
mode TTS pipeline. Lives in `cognis/core/voice_resolution.py`.

## API Surface

### `POST /api/v1/tts/synthesize`

Request:

```json
{
  "text": "Hello, how can I help?",
  "message_id": "msg_abc123",
  "agent_id": "research-agent",
  "voice": "nova",
  "format": "mp3",
  "speed": 1.0
}
```

`message_id` and `agent_id` are optional. When `message_id` is provided, the
endpoint checks the TTS cache before synthesizing and returns the cached
artifact when available. When `agent_id` is provided and `voice` is not, the
agent's voice (or system fallback) is used. When neither is provided, the
hard system default is used.

Response:

```json
{
  "audio_url": "/api/v1/artifacts/content/tts/tts_<digest>/speech.mp3?exp=...&sig=...",
  "content_type": "audio/mpeg",
  "duration_seconds": 3.4,
  "voice": "nova",
  "model": "openai/tts-1",
  "cached": true
}
```

The audio URL is a short-lived signed URL produced by the existing artifact
store. The client streams audio directly from that URL.

### `POST /api/v1/stt/transcribe`

Request: `multipart/form-data` with either:

- A `file` field containing the audio blob.
- An `artifact_id` field referencing an already-uploaded artifact.

Optional fields: `language`, `prompt`, `model`.

Response:

```json
{
  "text": "send a calendar invite for tomorrow at three",
  "language": "en",
  "duration_seconds": 4.2,
  "model": "openai/whisper-1"
}
```

This endpoint is the web equivalent of the channel STT path. The existing
audio-preprocessing helpers in `cognis/channels/inbound.py` (MIME validation,
ffmpeg-based normalization to WAV) are lifted into a shared module
`cognis/audio/preprocessing.py` so both the channel pipeline and this
endpoint share one implementation.

### Settings additions

Three new keys in the `settings` table (key/value):

| Key | Type | Default | Description |
|---|---|---|---|
| `tts.default_voice` | string | `null` | System fallback voice when an agent has none set. |
| `tts.enabled` | bool | `true` | Master kill-switch for TTS endpoints and UI buttons. |
| `tts.cache_ttl_days` | int | `30` | Retention for cached TTS artifacts. |

## WebSocket Protocol Additions

The chat WebSocket gains two inbound message types and one outbound frame
type for conversation mode.

### Inbound (client → server)

```json
{"type": "enable_tts", "voice": "nova"}
{"type": "disable_tts"}
```

`enable_tts` is sent by the client when the user enters conversation mode.
The connection records `tts_enabled=True` and the chosen `voice` for the
duration of the session. `disable_tts` clears both flags.

### Outbound (server → client)

```json
{
  "type": "tts_sentence_ready",
  "message_id": "msg_abc123",
  "sentence_index": 0,
  "text": "Hello, how can I help today?"
}
```

Emitted only when `tts_enabled=True` on the connection. The
`WebSocketTurnObserver` wraps `on_token` to feed a per-message sentence
buffer (lives in `cognis/core/sentence_buffer.py`); when a sentence boundary
is detected the frame is emitted with the cleaned sentence text (markdown
stripped, code blocks excluded). The client calls `/api/v1/tts/synthesize`
for each sentence and enqueues the audio in a sequential player.

This decoupling — boundary detection on the server, synthesis as a normal
HTTP call — keeps the cache path intact for streamed sentences and lets the
client back-pressure through HTTP rather than over the WS frame stream.

## UI Surface

See [`09-ui-ux.md`](09-ui-ux.md) for the general UX spec. Voice-specific
additions:

### Speaker button on assistant messages

A speaker icon appears in the assistant-message footer toolbar next to the
copy button. While idle the icon is `Volume2`; while playing it becomes
`Square` (stop). Clicking it toggles playback for that message. Only one
message plays at a time across the workspace; starting a second pauses any
running one.

When TTS is disabled at the system level (`tts.enabled=false`), the button
is hidden. When TTS is enabled but the routing has no `text_to_speech`
target configured, the button is shown but disabled with a tooltip linking
to settings.

### Microphone button in the composer

A microphone icon sits on the leading edge of the composer pill alongside
the paperclip. Tapping it requests `getUserMedia({audio:true})` on first
use, then begins recording. While recording, a live timer pill appears in
the composer attachments tray. Tapping stop ends recording and the pill
becomes an audio attachment with play / pause / delete and duration.

Sending the message uploads the recording, calls
`/api/v1/stt/transcribe`, replaces the composer body with the transcript
(or appends, when text was already typed), drops the audio from the outgoing
turn, and submits normally.

### Conversation mode

A new conversation button in the chat header opens a modal overlay:

- Animated orb with three states: `listening`, `processing`, `speaking`.
- Live transcript drawer (collapsible).
- Bottom controls: mute, switch back to text, end conversation.
- Pre-flight check: TTS and STT routes configured, mic permission granted.
  When unmet, an inline setup prompt appears instead of starting the loop.

Lifecycle:

1. On open: send `{type:"enable_tts", voice}` over the chat WebSocket.
2. Start `MediaRecorder`. Run client-side VAD
   (`@ricky0123/vad-web`, with an `AudioContext.AnalyserNode` RMS-threshold
   fallback).
3. On VAD silence: stop recording, upload + transcribe, submit turn.
4. While the assistant streams: receive `tts_sentence_ready` frames, fire
   `POST /api/v1/tts/synthesize` for each, enqueue audio in a sequential
   player.
5. After the last queued audio ends and the assistant message is complete,
   restart the mic. Repeat.
6. On close, ESC, or "End": send `{type:"disable_tts"}`, stop everything.

### Settings UI

The settings page gains a `text_to_speech` row in the routing matrix
(metadata description: "Voice synthesis for assistant messages and
conversation mode. Use models like `tts-1`, `tts-1-hd`,
`eleven_multilingual_v2`, or your own Piper-compatible HTTP server.").

Below the model dropdown: a model-aware voice picker for the system default
voice. For OpenAI TTS models it renders a dropdown of the documented OpenAI
voices; for other models it renders a free-text input with a helper link to
the provider's voice catalog.

### Agent form

`AgentForm.svelte` gains a Voice field below the model picker. When TTS
routing is configured the field renders the same model-aware picker used in
settings, with an explicit "Use system default" option. When TTS is not
configured the field is disabled with helper text linking to settings.

## Audio Preprocessing

The existing channel-side helpers (`_normalized_audio_filename`,
`_transcode_audio_for_stt`, `_prepare_audio_for_stt`) move from
`cognis/channels/inbound.py` into a new module
`cognis/audio/preprocessing.py`. The channel pipeline imports from there;
the new STT route imports from there. Behavior is unchanged.

ffmpeg is required wherever STT runs — controller or executor — and the
existing routing rules (`fix(channels): route voice audio conversion through
executors`, see git history) continue to apply: when STT is configured to
run on an executor, the audio bytes are sent over the wire and ffmpeg runs
on the executor host.

## Telemetry

Prometheus counters (labelled with `provider` and `model`):

- `cognis_tts_synthesize_total`
- `cognis_tts_synthesize_errors_total`
- `cognis_tts_cache_hits_total`
- `cognis_tts_cache_misses_total`
- `cognis_stt_transcribe_total` (extended with `source="web"|"channel"`)
- `cognis_stt_transcribe_errors_total`
- `cognis_conversation_mode_sessions_total`
- `cognis_conversation_mode_turns_total`

Histograms:

- `cognis_tts_audio_bytes` (per synthesis)
- `cognis_tts_synthesize_seconds` (per synthesis)
- `cognis_stt_audio_seconds` (input duration)

Logs contain ids, voice, model, durations, and reason codes. Logs **never**
contain transcribed text, synthesized text, or audio bytes — these fall
under the same content redaction rules as message content.

## Security and Privacy

- TTS and STT artifacts inherit existing artifact-store authorization and
  signed-URL logic. URLs expire on the same TTL as image artifacts.
- `tts.default_voice` and per-agent `voice` values are not secrets and are
  exposed in API responses.
- Voice does not bypass guardrails. The assistant message is already
  evaluated through the existing Intaris path before it streams; TTS
  synthesizes the already-emitted text, so no new review surface is needed.
- Mic recordings uploaded for STT are stored in the standard `attachments`
  namespace. They are not retained as turn attachments after the transcript
  is produced; they are eligible for the standard artifact retention sweep.
- Cached TTS artifacts respect the user-isolation rules of the artifact
  store (per-owner namespace). A user cannot retrieve another user's cached
  audio.

## Deployment

New environment variable:

| Variable | Default | Description |
|---|---|---|
| `COGNIS_TTS_CACHE_TTL_DAYS` | `30` | Override for the `tts.cache_ttl_days` setting on first start. |

Existing variables are unchanged. ffmpeg remains a runtime dependency for
STT (controller-side or executor-side, depending on routing).

## Migration and Backward Compatibility

- New tables: `tts_cache` only (small metadata row). New schema bootstrap
  helper `_ensure_tts_cache_table` and matching Alembic migration.
- No schema change to `agents`, `llm_providers`, `model_routing`,
  `settings`, or `sessions`. New JSON keys in existing config columns
  deserialize as `None` on legacy rows.
- New `text_to_speech` routing entry is optional; absence renders TTS
  buttons disabled with a settings link, but does not affect any other
  Cognis functionality.
- Removing TTS configuration is safe: synthesize endpoint returns a
  clear error and the UI degrades gracefully.

## Non-Goals

- **Local Piper subprocess hosting.** Cognis consumes APIs only. Self-hosted
  Piper users run an HTTP server (e.g., `wyoming-piper` behind a small
  OpenAI-compatible shim, or `piper-streaming-tts`) and configure it as a
  normal OpenAI-compatible provider.
- **Voice cloning, voice creation, or training.** Cognis selects from the
  voice catalog the underlying provider exposes.
- **TTS during workflow steps.** The deliverable contract from
  [`21-workflow-deliverables.md`](21-workflow-deliverables.md) is
  unchanged. Channel-delivered deliverables are still text; voice mode is a
  presentation layer for conversation messages only.
- **Server-side audio storage of mic recordings beyond artifact retention.**
  The recording is a transient input to STT, not a durable record.
- **Real-time bidirectional WebSocket audio streaming** (e.g.,
  OpenAI Realtime API). The conversation-mode design uses the existing
  chat WebSocket plus `/api/v1/tts/synthesize` HTTP calls per sentence.
  A future spec may add a Realtime-style transport when the latency or
  interruption semantics justify the protocol surface.
- **Speech translation.** STT returns text in the spoken language; the
  agent can translate via prompt if asked. No dedicated translation route.

## Open Questions

- **Server-side VAD as a fallback for browsers without WASM-capable mic
  pipelines.** Out of scope for v1. Browser support for VAD is acceptable
  on all current desktop browsers and recent mobile Safari.
- **Per-agent TTS provider override** (e.g., one agent on ElevenLabs, others
  on OpenAI). Currently routing is system-wide. Adding a per-agent
  TTS-provider override is doable later by adding `tts_provider_id` to
  `AgentLLMConfig` and threading it through `voice_resolution`. Defer
  until a clear use case appears.
- **Streaming audio response formats** (pcm chunks, `tts_streaming`). LiteLLM
  is starting to expose this for some providers. Defer until provider
  support is uniform; sentence-buffered HTTP is a good baseline.
- **Voice cards in the UI** (preview each voice with a short sample before
  saving). Useful UX polish, not a v1 blocker; deferred.
