# Turn Performance Optimization — Investigation, Telemetry, and Continuation

- **Status:** instrumentation implemented, reviewed, tested, committed, and smoke-verified; activation in the long-lived messaging gateway requires a native restart
- **Last updated:** 2026-07-20 17:20 BRT
- **Canonical implementation commit:** `bce663a9a250c74429b084b8c7f36179a7ba1a04`
- **Published branch:** [`ammichael/perf/turn-telemetry`](https://github.com/ammichael/hermes-agent/tree/perf/turn-telemetry)

This document is the source of truth for continuing the response-time optimization work. It records the evidence, decisions, implementation, verification, activation gate, and next experiments. Do not reconstruct the investigation from chat history when this document and the live metrics are available.

## 1. Objective

Reduce the perceived and total response time of Hermes/N in long-lived WhatsApp conversations without degrading:

- answer quality or reasoning where it matters;
- prompt-cache stability;
- durable memory or conversation continuity;
- tool-result recoverability and replay;
- failure recovery, verification gates, or safety;
- privacy of messages, identities, local paths, and tool data.

The immediate strategy is **measure first, then remove proven latency**. Global reductions to reasoning, context, compression thresholds, or tool availability are not accepted without stage-level evidence.

## 2. Baseline and causal findings

### 2.1 Original conversation-level baseline

The first investigation measured completed turns in a long-lived WhatsApp conversation:

- no tools: median **17.52 s**, p90 **19.73 s**;
- turns with tools: median **150.35 s**, p90 **425.93 s**;
- turns with three or more tools: median **159.17 s**, p90 **443.26 s**, maximum **917.26 s**;
- enabling all tool schemas increased a simple request from about **4,849** to **20,289** input tokens;
- long sessions reached roughly **137K–209K prompt tokens per model call**, with **87%–97% cache reads**.

A later seven-day read-only snapshot of the target chat showed:

- no tools: median **12.9 s**, one model call on average;
- with tools: median **176.2 s**, **14.67 model calls** on average;
- three or more tools: median **187.04 s**, **15.62 model calls** on average.

The absolute values vary by session and workload, but the causal shape is stable: **the number of sequential model calls multiplies turn latency**.

### 2.2 Stage telemetry evidence

Historical stage telemetry existed for twelve target-chat turns before its producer was lost during a runtime update:

- median whole turn: **57,146 ms**;
- median accumulated model time: **49,186 ms**;
- median tool work: **630 ms**;
- median first delta: **7,218 ms**;
- original tool-result content: **2,590,264 chars**;
- model-facing tool-result content: **651,433 chars**;
- tool-context reduction: **74.9%**.

This is the strongest evidence so far: tools accounted for roughly 1% of the median instrumented turn, while sequential model work dominated.

### 2.3 Live provider-call evidence

A same-day aggregate of 1,928 real `gpt-5.6-sol` calls showed:

- per-call median: **9.8 s**;
- p90: **22.6 s**;
- p99: **50.3 s**;
- maximum: **176 s**;
- median input: **123,793 tokens**;
- median cache share: **98.5%**.

Retries and fallback did not explain the normal median on that day:

- one API retry;
- zero observed stream-kill events;
- zero observed fallback activations.

They remain candidates for tail latency, but not for the dominant normal path.

### 2.4 Fixed fresh-call payload

A current WhatsApp prompt-size probe measured approximately:

- tool schemas: **66,054 bytes** (~16.5K tokens);
- system prompt: **43,086 bytes** (~10.8K tokens);
- skills index: **17,918 bytes** (~4.5K tokens);
- memory + profile: ~**1.1K tokens**;
- total fixed payload: roughly **32.9K tokens**, before conversation history.

The high cache-read share confirms that prompt caching is working. Cache reads reduce cost, but cached prefill and a growing suffix still have latency. Do not treat “98% cached” as “free.”

## 3. Challenge review and decisions

Claude Fable independently challenged the diagnosis and returned **CONDITIONAL APPROVE** for the strategy:

- bounding/spill, tool parallelization, and auxiliary compression already existed in the live path;
- the missing piece was reliable stage telemetry and parameter tuning, not reimplementing those mechanisms;
- retries/stale/fallback could explain tail outliers but required exact log evidence;
- model-call multiplicity, compression, verification nudges, continuations, and other extra calls needed separate causal counters.

The first telemetry candidate received **BLOCK** because:

1. model-attempt durations used an outer timestamp and would grow cumulatively across retries;
2. early returns and propagated exceptions could skip final telemetry;
3. a crash during HMAC-key creation could leave telemetry permanently disabled;
4. model labels could persist absolute local paths;
5. a partial JSONL write could corrupt the following row;
6. spill-size parsing could false-positive on ordinary tool content.

All six findings were corrected. Fable's exact-candidate revalidation returned **APPROVE**, with no P0/P1 findings.

## 4. Implemented telemetry

### 4.1 Files

- `agent/performance_telemetry.py`
  - content-free accumulator;
  - profile-local HMAC session pseudonyms;
  - multiprocess-safe private JSONL writer;
  - incomplete-key recovery;
  - partial-line isolation and rotation;
  - aggregate tool-result sizing without retaining content.
- `agent/conversation_loop.py`
  - public wrapper that finalizes telemetry on normal return, early return, exception, interrupt, and alternate-runtime return;
  - separate inspectable `_run_conversation_impl` for existing source invariants;
  - monotonic per-attempt model timing and TTFT;
  - tool-batch timing and aggregate size measurement;
  - compression timing across every live compression call site.
- `agent/turn_finalizer.py`
  - normal-path completion and finalization timing;
  - idempotent coordination with the public wrapper.
- `tests/agent/test_performance_telemetry.py`
  - privacy, permissions, HMAC, key recovery, multiprocess writes, spill accounting, and path redaction.
- `tests/agent/test_conversation_performance_wrapper.py`
  - early-return and exception finalization with content-leak assertions.
- `tests/run_agent/test_run_agent.py`
  - source-based retry invariant now inspects `_run_conversation_impl` rather than the wrapper.

### 4.2 Per-turn record

The JSONL row contains only bounded metrics and sanitized labels:

- schema version and UTC start time;
- HMAC-derived `session_ref` (never raw session/chat/user IDs);
- whole-turn, prologue, model, tool, compression, finalization, and unattributed milliseconds;
- model-call count, tool-call count, compression count, and API-call count;
- per-attempt model duration, TTFT, success flag, sanitized provider/model labels, and request character count;
- per-tool-batch wall duration, call count, original chars, model-facing chars, and spill count;
- sanitized exit-reason code and completed flag.

It never stores:

- prompts or assistant text;
- tool arguments or results;
- raw user, chat, session, request, or tool-call IDs;
- base URLs;
- local paths;
- credentials, headers, cookies, or secrets.

### 4.3 Storage and failure behavior

- metrics directory mode: `0700`;
- metric/key/lock file mode: `0600`;
- key creation and writes use no-follow opens where supported;
- inter-process `flock` protects key creation, rotation, and append;
- lock acquisition is bounded;
- a short key left by a crashed process is recreated under the lock;
- a partial trailing line is isolated before the next valid JSON row;
- telemetry failure is fail-quiet and cannot fail the user turn;
- if a valid pseudonym key cannot be obtained, no row is persisted.

## 5. Verification evidence

### 5.1 Automated gates

- primary runtime suite: **436 passed**;
- focused and adjacent telemetry/finalizer/tool-output suites: **119 passed**;
- Python compilation: passed;
- whitespace/diff check: passed;
- Fable candidate revalidation: **APPROVE**.

### 5.2 Real smoke

A fresh Hermes process loaded the committed code and answered the exact smoke prompt correctly. One and only one telemetry record was appended:

- schema: `1`;
- completed: `true`;
- API calls: `1`;
- model calls: `1`;
- TTFT: **3,196 ms**;
- model duration: **3,458 ms**;
- turn duration: **3,767 ms**;
- forbidden top-level fields: none;
- metric file mode: `0600`;
- metrics directory mode: `0700`.

This proves the committed source works in a real provider path. It does not prove that the already-running messaging gateway has reloaded it.

## 6. Activation gate

The long-lived messaging gateway must be restarted using the native `/restart` command. Do not restart the gateway from inside an active agent turn: doing so can interrupt final persistence and interact badly with resume behavior.

After restart, verify:

1. gateway health returns normally;
2. one simple WhatsApp turn completes;
3. the latest metrics timestamp advances;
4. the new row has a HMAC session reference, one or more model calls, and no forbidden fields;
5. the conversation resumes with memory and role alternation intact.

The read-only collector is:

```bash
python3 ~/.hermes/scripts/hermes-conversation-perf.py \
  --chat-id '<exact-chat-id>' \
  --days 7 \
  --no-append
```

Do not paste raw chat IDs or raw JSONL rows into shared reports. Report aggregate metrics only.

## 7. Next measurement phase

Collect at least **20 post-restart target-chat turns** covering:

- short no-tool answer;
- one fast read-only tool;
- multiple independent tools in one batch;
- dependent tools that must remain serial;
- small and large tool output;
- short and long conversation state;
- a compression event, if naturally reached;
- a real retry/failure, if one occurs naturally.

For each class, report median, p90, maximum, and sample count for:

- TTFT;
- per-attempt model duration;
- accumulated model duration per turn;
- model/API calls per turn;
- tool wall time;
- compression time and frequency;
- original vs model-facing tool chars;
- retries/fallbacks and exit reason;
- finalization and unattributed time;
- whole-turn duration.

Separate **per-call latency** from **per-turn accumulated model time**. A 10-second model call repeated fifteen times is a 150-second turn even when every individual call is healthy.

## 8. Experiment order

Run experiments one variable at a time, using the same model, reasoning setting, prompt class, and warmed-session conditions.

### Experiment 1 — identify extra model calls

Classify every call after the first as:

- required after a tool result;
- retry/fallback;
- empty/truncated-response recovery;
- verification nudge;
- continuation;
- budget summary;
- compression-related;
- other.

**Primary target:** eliminate or batch calls that are not required for correctness. This has the highest expected impact.

### Experiment 2 — tool-result context cap

Current effective setting is `tool_output.max_bytes = 30000`.

Compare the baseline with `20000` in a reproducible tool-heavy session. Accept only if:

- target median improves by at least **15%**;
- task completion and factual quality remain intact;
- no read/spill recovery loops appear;
- cache-read share does not regress materially;
- no durable-memory behavior changes.

### Experiment 3 — reasoning policy for trivial turns

Current global reasoning is `medium`. Do not lower it globally first. Test a scoped policy for trivial no-tool turns only after call classification is available.

Accept only if simple-turn latency improves without reducing correctness on tool selection, ambiguity handling, or complex turns.

### Experiment 4 — compression timing

Current compression threshold is `0.4`, with `gpt-5.6-terra` via `openai-codex` as the auxiliary compression route.

Measure frequency and duration before changing anything. If inline compression is a significant outlier source, evaluate idle-time compression between turns with session locking. Do not lower the threshold merely to shrink context: more frequent compression invalidates cache and can increase latency.

### Experiment 5 — fixed tool-schema payload

Tool schemas are a large fixed prefix, but mostly cache-read. Any reduced toolset must be selected **at session creation** and remain byte-stable for that conversation. Never mutate tool availability mid-session to chase latency.

This experiment is lower priority than eliminating unnecessary model rounds.

## 9. Current hypotheses, ranked

1. **High confidence:** sequential model-call count is the dominant cause of slow tool-heavy turns.
2. **High confidence:** model-facing tool-output bounding already helps materially; tuning the 30K cap may provide an additional but smaller gain.
3. **Medium confidence:** verification/continuation/recovery calls create avoidable rounds in some turn classes.
4. **Medium confidence:** inline compression contributes to long-session outliers.
5. **Low confidence for normal median, higher for tail:** stale/retry/fallback events cause isolated 300–900 s turns.
6. **Lower priority:** fixed tool schemas affect cold/post-compression calls, but cache preservation makes them less important than repeated calls.

## 10. Explicit non-goals

Do not:

- delete or weaken durable memory to reduce prompt size;
- mutate system prompt or toolset mid-conversation;
- truncate tool results without recoverable private spill;
- remove retry/verification safety gates without causal evidence;
- use raw observer-hook request payloads for this telemetry;
- persist message content, arguments, results, paths, or raw IDs;
- tighten provider stale timeouts globally based only on tail anecdotes;
- claim improvement from one smoke run.

## 11. Rollback

The instrumentation is behavior-neutral and fail-quiet. If a regression appears:

1. preserve the metrics artifact for diagnosis;
2. revert commit `bce663a9a` as one unit;
3. restart the gateway natively;
4. verify a simple no-tool turn and a tool turn;
5. retain this document and the review findings so a corrected implementation does not repeat the retry/early-return bugs.

Rollback must not remove or modify durable conversation memory.

## 12. Repository and publication state

- local implementation commit: `bce663a9a250c74429b084b8c7f36179a7ba1a04`;
- remote branch read-back confirmed at the same SHA;
- branch URL: <https://github.com/ammichael/hermes-agent/tree/perf/turn-telemetry>;
- PR creation URL: <https://github.com/ammichael/hermes-agent/pull/new/perf/turn-telemetry>;
- the local `main` branch was ahead of and behind `fork/main`, so the change was intentionally pushed to a dedicated branch rather than force-pushing `main`;
- unrelated pre-existing changes in `cron/scheduler.py` and `tests/cron/test_cron_script.py` were preserved and excluded from the performance commit.

## 13. Continuation checklist

When resuming this work:

1. Read this document.
2. Confirm the installed commit and gateway restart state.
3. Confirm the latest telemetry timestamp is post-restart.
4. Collect at least twenty target-chat turns.
5. Produce a stage-level baseline with sample counts.
6. Classify extra model calls before changing config.
7. Run one-variable experiments in the order above.
8. Re-run focused tests and a real smoke after every accepted change.
9. Challenge-review the exact final diff before commit.
10. Commit and push only performance-scoped files; do not absorb unrelated cron work.
11. Update this document with measured before/after results and the next decision.
