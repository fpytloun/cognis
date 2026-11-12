# Speech and audio

Cognis uses the `text_to_speech` and `speech_to_text` model routes for speech.
Provider credentials remain in the secret store. Inference can run on the
controller or on an assigned executor; agents never receive provider keys.

## ElevenLabs

1. Add an **ElevenLabs** provider in Settings → Providers.
2. Select secret-based authentication in the provider editor
   (`config.auth_config.mode: "secret"` and `config.auth_config.secret_name`).
   The default endpoint is
   `https://api.elevenlabs.io`; a versioned `/v1` endpoint also works.
3. Discover and save the models. Discovery marks synthesis and transcription
   capabilities explicitly. Select a synthesis model such as
   `eleven_multilingual_v2` for `text_to_speech`, and `scribe_v2` (or `scribe_v1`)
   for batch `speech_to_text`. `scribe_v2_realtime` also supports live conversation
   transcription. When selected for recordings/uploads, Cognis decodes audio
   to bounded mono 16 kHz PCM and uses that same model's realtime protocol;
   FFmpeg must be installed on the configured inference host.
   After adding discovered models, save the provider before selecting them in
   Settings → Routing → **Speech to text**. Transcription models are not offered
   as chat or text-to-speech models.
4. In the agent editor, select **TTS provider**, **TTS model**, and **Provider voice**
   under **Providers & models**. The model picker includes speech-capable models
   from the selected provider; the voice catalogue follows that provider/model.
   **Inherit system TTS settings** uses the `text_to_speech` route instead.
   This selection is independent of the agent's chat model. An unsupported saved
   voice falls back to an available voice with a visible warning.
   Preview generates a short sample and may incur a charge.

ElevenLabs synthesis supports MP3 and PCM output. Browser playback and
deliverable audio use MP3. Unsupported format choices fail explicitly.
Transcription language selection is supported; prompt hints are currently
rejected for ElevenLabs rather than silently ignored.

Other providers continue to use the existing LiteLLM speech adapter or
OpenAI-compatible speech endpoint. They can be extended without adding another
message, tool, or deliverable speech policy.

## Streaming support

Conversation mode and message reading can play audio while it is being generated:

| Provider | Streaming speech | Live transcription |
| --- | --- | --- |
| ElevenLabs | TTS models, including v3/v4 through Text-to-Dialogue | `scribe_v2_realtime` |
| OpenAI | `gpt-4o-mini-tts`, `tts-1`, `tts-1-hd` | `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, `gpt-live-transcribe`, `gpt-transcribe` |

Choose the model explicitly in the existing speech routes. Batch ElevenLabs
models such as `scribe_v1` and `scribe_v2`, and OpenAI `whisper-1`, do not become
live models automatically. Other providers retain completed-recording playback.
Ordinary composer recordings and uploaded files continue using file transcription.

Live playback uses mono 24 kHz PCM. Complete audio is finalized into the requested
replay format; MP3 finalization requires the controller's existing FFmpeg runtime.
If encoding or browser streaming support is unavailable before generation starts,
Cognis uses its completed-audio path. Provider keys stay on the configured
controller or executor, including when controllers forward executor requests.

Live microphone sends and streamed speech delivery revalidate the authenticated
document owner. Account or server changes stop the transport without replaying it.

Speech sessions have bounded buffers and a two-minute provider-session limit.
Transcription is committed at the end of an utterance; partial captions are not
submitted as messages. ElevenLabs long utterances use serialized internal commits
before the provider's automatic segment boundary, preserving their combined text.
Interruption discards live playback/capture; it never replays an uncertain paid
request. A disconnected preview can leave its already-started, shared speech part
finishing for cache reuse. It does not start another generation.

## Native agent management

Use `manage_agents` with `action="settings_update"` and typed
`settings.tts_provider_id`, `settings.tts_model`, and `settings.tts_voice` fields.
Read back the stored preferences in `llm_config` through `get` or `settings_get`.
Omitted fields are preserved; set all three to `null` to remove the explicit
binding and use the existing inherited speech settings. A selected provider
requires a model. These fields never change the agent's chat model or the global
`text_to_speech` route.

`settings.voices` replaces the provider-ID-to-voice-ID fallback map (`{}` clears
it); `settings.voice` is the legacy fallback voice. These fallbacks apply only
according to the existing speech precedence, not over an explicit TTS binding.
Voice IDs remain provider/model-specific; unavailable saved voices use the same
runtime fallback and warning as the UI. Saving preferences does not synthesize
audio or query a provider account.

For shipped system agents, use the same three TTS fields inside
`system_overrides_update` → `overrides.llm_config`. As with other system-agent
overrides, a supplied `llm_config` replaces the entire stored override object:
read `system_overrides_get` first and preserve unrelated overrides. Setting that
object to `null` restores the shipped configuration.

## Reading assistant messages

Click the speaker beside an assistant message. Once ready, the same control
becomes play/pause, with 10-second seek buttons grouped beside it. These controls
remain available while paused. Press the speed number to cycle through
0.75×, 1×, 1.25×, 1.5×, 1.75×, and 2×. Messages and deliverable players share
this speed for the current browser session; changing it does not generate audio
again. Completed audio preserves pitch; live PCM speed changes also change pitch.
When playback finishes it collapses back to the speaker. While generation is
pending, clicking cancels it. Long messages are split into ordered audio
parts. By default, Cognis reads the original message without an LLM rewrite.

Settings → Account → **Adapt assistant messages for listening** is off by
default. When enabled, `audio_narrative` rewrites a message into spoken prose
before synthesis. This adds latency and model cost, preserves a script artifact,
and does not change the message. It applies to the speaker button and
conversation mode. Without adaptation, conversation mode streams sentences;
with adaptation, it waits for a complete response and rewrites it once.

The `audio_narrative` routing slot inherits `companion_lite` when unset. It can
select an explicit text model or a named route. Configure a lightweight text
model unless the material needs a more capable one.

Conversation mode checks route configuration before requesting microphone
permission. Interruption stops playback and requests backend turn cancellation
before returning to listening. Escape closes the overlay and releases the
microphone. Configuration checks do not replace live provider or device testing.

Listening starts with two rising notes and ends with the same notes in
reverse order. The open button primes cue audio before loading the overlay.
A softer pulse repeats every eight seconds while transcribing, waiting for the
assistant, or preparing audio, and stops during playback. Microphone input is gated during the start cue and disabled before
the stop cue, so the cues are not part of the recording. Browser autoplay rules
can require a tap before tones are available.

After detecting speech, conversation mode allows about three seconds of silence
before handing off the turn. Quieter speech keeps the same utterance open, and
room noise is measured for 700 ms **before** the ready tone, without recording or
starting live transcription. This keeps opening words out of calibration.
If the browser cannot measure microphone activity, tap the orb to finish manually.
A thinking pause shows
**Still listening**; tap the orb to finish immediately instead of waiting.

Conversation mode follows the admitted Chat v2 turn, including canonical
snapshot recovery, rather than legacy token or message-completion events.
The overlay distinguishes **Transcribing**, **Waiting for the assistant**, and
**Speaking**. It returns to listening only after the turn, synthesis requests,
and playback have all finished. You can tap the orb to interrupt while waiting
as well as during playback. A slow-turn warning does not automatically reopen
the microphone over a still-running agent.

Once tools appear in the admitted voice turn, the orb shares its area with the
same tool cards and tool groups as chat, using your grouping and visibility
preferences. Activity uses about a
third of the width on desktop. On phones, it appears below the orb and is capped
at a third of the screen height. Longer activity scrolls inside that area.

New deliverables from that voice turn are read automatically after the message,
using **Reading**, not a podcast rewrite. Existing recordings for the exact
revision are reused; generating a new recording can incur speech charges.
Completed parts play in order while later parts are prepared. The microphone
stays off until narration finishes. Failed attempts are not automatically
regenerated: open the document's Reading controls to inspect or retry them.
Interrupting or closing the call stops playback and observation; an already
started durable recording can finish in the background for reuse.

This remains an utterance-based, half-duplex loop. Supported live STT models
transcribe microphone audio during capture, then submit only the committed
utterance. Other models receive an uploaded recording after a pause. The agent's
sentences can play before their synthesis finishes. This is not full duplex:
echo-aware spoken barge-in and native speech-to-speech reasoning are not enabled.

## Deliverable narration and podcasts

Open the **speaker icon** in a document's toolbar: chat, Library, attachment previews, or
interactive standalone pages. Markdown, plain text, HTML, and Rich deliverables
share the same controls. Saved text artifacts also support narration and podcasts;
binary artifacts without readable text show an explicit unsupported-format notice.
All work is pinned to the displayed revision.
The icon highlights active playback and distinguishes playing from paused audio.
The opaque popup has **Reading** and **Podcast** tabs, each using its current
generating recording's completed parts or its latest completed recording.
There is no recording-version selector. A failed newer
attempt does not remove an existing usable recording.

Failures distinguish provider access, exhausted credits, rate limits, rejected
requests, unavailable providers, incomplete audio streams, and internal Cognis
errors when that evidence is available. Raw provider messages, submitted text,
and credentials are not included. Failed generation is never automatically
replayed; starting another attempt can incur charges.

Opening these non-modal controls does not lock or reflow the document.
Public document links expose speaker options without signing in. Visitors can
listen to recordings for the exact shared revision or request narration and a
shorter, default, or longer podcast using the owner's configured voice and routes,
billed to the owner. Requests are rate-limited and deduplicated across visitors
and links into at most four public generation plans per source revision. Visitors
cannot choose arbitrary voices, change the source, or cancel recordings. Failed
public plans require owner intervention rather than unlimited paid retries.
Expired or revoked links cannot request audio or refresh recording URLs.

Speech uses the author agent's TTS provider, model, and voice when those settings
are accessible to the generating user; otherwise it uses the configured defaults.
It never borrows another user's private provider configuration or credentials.
Configure defaults in **Settings → Models → Task-type routing**:
**Speech / voice (`text_to_speech`)** for synthesis and **Podcast writing
(`audio_narrative`)** for scripts. Configuration is separate from the listening controls.
The author agent's independent TTS settings are in its **Providers & models** editor.

Standalone pages outside the managed public viewer require a signed-in account
with independent document access to play private recordings or generate new ones.
Downloaded offline HTML does not offer generation controls.

- **Generate narration** reads its text without an LLM rewrite. Markdown headings,
  links, and table cells become spoken text; code blocks are identified rather
  than recited as punctuation.
- **Generate podcast** uses `audio_narrative` to create a single-narrator episode
  with an opening, connected explanation, transitions, and a conclusion—not a
  bullet-summary readout or invented conversation. It is an adaptation, not the
  authoritative document. Source caveats must remain, and the source language
  is preserved.

Choose **Shorter**, **Default**, or **Longer**; **Default** is selected initially.
All three show approximate durations before generation. Estimates use the authorized
revision's readable text, not a paid planning/model call. They are ranges, not runtime
promises: speaking pace, language, and the material affect the result.

The same source-derived word budget is stored with the job and passed to script
generation. Short material is not stretched to a fixed six-minute episode.
Long sources have bounded, condensed budgets; longer does not mean verbatim reading.
If estimates cannot load, retry them; cancellation remains available independently.
Existing API clients can still submit explicit `target_minutes` instead of `length`,
but not both. Accepted jobs keep their original budget on retries and restarts.
Script and input limits bound work,
but provider billing depends on generated tokens and speech characters.
Generation is explicit and may incur both model and speech charges.
Scripts are retained internally for safe reuse; the player does not link to a
transcript download.

Jobs run in the background and survive refresh/navigation. The panel shows
progress and cancellation. Playback is available after the first complete audio
part; a shorter initial part reduces the wait. Later parts append in order. If
playback catches generation, it waits for the next part without restarting the
current one or claiming that the job is complete. Pausing or switching players
prevents automatic playback takeover. The player shows inline play/pause, circular 10-second back/forward controls,
a position slider, a playback-speed selector, and an audio download.
Seeking works across part boundaries, and position is saved in the browser.
Closing the popup does not stop playback.
Audio and scripts are durable related artifacts;
the source deliverable is unchanged.

### Formats and general-purpose use

Length is independent of subject matter: the same controls apply to daily
briefings, news, study material, reports, and other readable documents.
There is no separate hard-coded use-case pipeline.

NotebookLM's [Audio Overview formats](https://support.google.com/gemininotebook/answer/16212820?hl=en)
are the design reference for a future format layer:

| Format | Listening experience |
| --- | --- |
| Deep Dive | Two hosts connect and explain the material |
| Brief | One speaker gives key takeaways in under two minutes |
| Critique | Two hosts constructively evaluate the material |
| Debate | Two hosts examine contrasting perspectives |

Focus/audience instructions and output language are also useful cross-cutting
controls, rather than separate daily-news or study modes. Multi-voice formats
and interactive voice Q&A are **not implemented by this length-control change**.
Current podcasts remain source-grounded, single-narrator episodes.

Cancellation prevents further publication to the job but cannot guarantee a
provider refunds an already-started request. If a worker is interrupted, Cognis
does not automatically replay paid generation. Stale work becomes
**interrupted**; use **Generate again** explicitly. Repeating a submission with
the same idempotency key returns that job, not another chargeable job.
Viewing a job rechecks access to both its owner and source revision.
Empty, truncated, blocked, or overlong scripts fail before speech synthesis and
show a script-specific recovery message. Reasoning-only model responses are
never treated as spoken text.

## Agent tools

Enable `speech_synthesize` and `speech_transcribe` in the agent's tool policy.

- `speech_synthesize` accepts text, voice, format, and speed. It returns durable
  audio artifact references and URLs; it does not play or send audio.
- `speech_transcribe` accepts an authorized audio artifact, with optional
  language and provider-supported prompt hints. It returns text and available
  language/timing information.

`artifact_read` also retains its convenient audio-transcription behavior.
Speech-tool use follows normal tool authorization, guardrails, and auditing.
Audio artifacts are not made public automatically.

## Storage and operational boundaries

The shared speech service owns cache identity, actor access, script generation,
chunking, and publication. Cache identity includes content, owner, acting agent
scope, provider, model, voice, format, speed, latency mode, and retention class.
Cached artifacts are authorized again before a URL is signed.
Concurrent requests coalesce paid chunk and script generation through shared
database leases. Repeated message adaptation and matching podcast requests reuse
retained scripts; completed deliverable chunks remain reusable beyond the message
cache TTL. Disconnects stop subsequent chunks; an accepted chunk may finish and
be retained for replay. A waiting request never automatically restarts an
interrupted shared attempt.
Message audio uses the existing expiring `tts` namespace; tool and deliverable
outputs use durable artifact storage.
Cross-controller reuse requires a shared database and shared artifact storage
(for example the configured S3 backend), not separate controller-local disks.

The audio-job table stores metadata and artifact references only. Source text,
scripts, and audio stay in their existing durable content stores.
The existing renewable maintenance lease owns queue execution; atomic status
transitions fence cancellation and concurrent claims. No DB transaction spans
model inference or object-store I/O. Application shutdown owns cancellation of
the worker; provider inference retains its existing retry policy.
