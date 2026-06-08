# TODOS — Voice Language Coach

Deferred work from the 2026-06-08 CEO review. Ordered by priority. These were
explicitly considered and deferred — not forgotten.

## P1 — The retention loop (next milestone)

These four are one coherent milestone: they make it a product people return to.
**E2/E3/E4 all depend on E1.** Build E1 first.

### E1 — Accounts + persistence (enabler)
- **What:** Login (or anonymous device-token) + a DB that stores each session's transcript and report.
- **Why:** Every session currently vanishes on tab close. Nothing longitudinal can exist without this.
- **Pros:** Unlocks E2/E3/E4; turns a stateless demo into a product.
- **Cons:** ~2 days; auth + DB surface to maintain; new deploy considerations (migrations).
- **Context:** Report data (`coach.REPORT_SCHEMA`) is already structured and currently discarded. Start with the boring choice: anonymous device-token + a single `sessions` table (transcript JSON + report JSON + timestamps), layer real auth later. Reviewed 2026-06-08; deferred in favor of in-session depth.
- **Effort:** L → CC: M · **Priority:** P1 · **Depends on:** nothing.

### E3 — Spaced-repetition review deck (highest-leverage feature)
- **What:** Turn report corrections + vocab into SRS-scheduled flashcards.
- **Why:** The report already generates this data and throws it away. Closes the practice loop.
- **Pros:** Strong "oh nice" feature built on existing data; the core learning mechanic.
- **Cons:** SRS scheduling + review UI (~2 days); needs storage.
- **Context:** Start with SM-2 or Leitner over the `corrections[]` and `new_vocabulary[]` arrays. Depends on E1.
- **Effort:** L → CC: M · **Priority:** P1 · **Depends on:** E1.

### E2 — Progress dashboard over time
- **What:** Trend fluency score, CEFR estimate, practice minutes across sessions.
- **Why:** Progress visibility is the core retention driver for a learning app.
- **Pros:** Cheap given stored reports; motivating.
- **Cons:** Needs stored history (E1); chart UI.
- **Context:** Reuses report fields already computed. Depends on E1.
- **Effort:** M → CC: S · **Priority:** P1 · **Depends on:** E1.

### E4 — Long-term learner memory
- **What:** Coach summarizes recurring weak spots and targets them in future sessions.
- **Why:** Personalization that compounds ("you keep mixing up ser/estar — let's drill that").
- **Pros:** Compounding stickiness; differentiator.
- **Cons:** Token-budget management; summarization quality; needs accumulated history.
- **Context:** Summarize past corrections into the system prompt. Depends on E1 + E3 data.
- **Effort:** M → CC: S · **Priority:** P1 · **Depends on:** E1, ideally E3.

## P2 — Engagement & reach

### Daily-streak / engagement loop
- **What:** Streaks, daily goal, gentle reminders.
- **Why:** Habit formation. **Cons:** Can feel gimmicky; needs E1. **Depends on:** E1. · P2.

### Shareable / exportable report
- **What:** Export report as PDF/link. **Why:** Sharing + record-keeping. **Cons:** Low retention value. · P2.

### Mobile PWA / iOS Safari hardening
- **What:** Make AudioWorklet + getUserMedia reliable on mobile Safari; installable PWA.
- **Why:** Most language practice happens on phones. **Cons:** Safari audio quirks are fiddly. · P2.

## P3 — Quality escalation

### Real pronunciation-assessment model
- **What:** Replace the Deepgram-confidence proxy (shipping in PLAN.md Phase 2) with a dedicated model (Azure Speech / Speechace / phoneme alignment).
- **Why:** The confidence proxy is honestly labeled "approximate" but isn't a true pronunciation score. If users want real feedback, this is the upgrade.
- **Cons:** New vendor + cost + integration (~3 days). **Trigger:** if the proxy proves too weak in real use. · P3.

### Shared-store rate limiting (when scaling beyond one instance)
- **What:** Move the `/ws` anti-abuse counter from in-process to a shared store (Redis).
- **Why:** The Release-1 counter is per-process; deploying with `uvicorn --workers N` or multiple containers makes the real cap N× the configured value (silent weakening of credit-burn protection).
- **Cons:** Adds a dependency + deploy surface. **Trigger:** the moment you run more than one worker/instance. **Context:** flagged in eng review (CQ-1); single-worker assumption documented in PLAN.md T3. · P3.

### STT buffer-and-replay on reconnect
- **What:** Instead of dropping the in-flight utterance on STT reconnect, buffer mic frames during the gap and replay them once reconnected.
- **Why:** Preserves short utterances across brief socket blips (better UX than "say that again").
- **Cons:** Buffer sizing/ordering complexity; Deepgram may still mis-segment a stitched stream. **Context:** Release 1 chose drop+resignal (ARCH-2) for honesty + simplicity; this is the richer alternative if reconnects prove common. · P3.

### Productionize the streaming engine as reusable infra
- **What:** Extract the STT→LLM→TTS orchestration into a provider-agnostic, multi-tenant, observable voice engine.
- **Why:** The pipeline is the genuinely reusable asset. **Cons:** Big; only worth it if a second consumer appears. · P3.
