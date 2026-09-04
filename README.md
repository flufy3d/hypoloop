# hypoloop

Three roles, one loop, running on **whichever CLI agent you already have installed**.

| Role | Job | Can edit files? |
|---|---|---|
| **Hypothesizer** | Reads the code, proposes **falsifiable** hypotheses | ❌ |
| **Challenger** | Attacks them — counterexamples, corrections, rival hypotheses | ❌ |
| **Verifier** | Edits, builds its own evidence, pushes claims toward *proven* | ✅ **only this one** |

Each round feeds the previous round's **evidence and verdicts** back in, so the loop
converges instead of three agents talking past each other.

hypoloop doesn't call models itself. It drives
[agy](https://antigravity.google) (Antigravity CLI, Gemini),
[codex](https://developers.openai.com/codex/cli) (OpenAI Codex CLI), or
[claude](https://claude.com/claude-code) (Claude Code CLI) — **whichever you have;
none is required**. You can also run the same task twice on different backends.

```
$ cd my-project
$ hypoloop "make the visuals crisper" --rounds 2 --commit

Backend: agy (Antigravity CLI)  hypothesizer/challenger gemini-3.8-flash-medium  verifier gemini-3.8-flash-high
Quota before: 5h window 92.149% left · weekly 98.692% left

=== Round 1/2 ===
  hypothesizer (read-only)  125s, 190,898 tokens  → 3 hypotheses
  challenger   (read-only)  208s, 254,372 tokens  → 3 critiques (0 rejected, 2 revise), 1 new hypothesis
  verifier     (write)      549s, 655,129 tokens  → 1 supported, 3 refuted; 1 file changed
=== Round 2/2 ===
  ...                                             → 3 supported, 0 refuted; 3 files changed

Committed 3 files (one commit for the whole run).

Quota after: 5h 76.936% · weekly 96.156%   (this run: −15.213pp / −2.536pp)
Tokens: verifier 1,285,923 · hypothesizer 194,133 · challenger 158,625 — total 1,638,681
```

That's a real run on a Three.js web game, not a mockup. In round 1 the hypothesizer
proposed "raise bloom threshold 0.25 → 0.80 to kill the grey haze." The challenger
computed why that's fatal: `LuminosityHighPassShader` uses BT.601 weights, so cyan
`0x00ffff` has luma 0.701 and magenta `0xff00ff` only 0.413 — 0.80 would **decapitate
every neon in the game**. The verifier confirmed the rebuttal by measurement: in-game
cyan glow pixels collapsed 34728 → 19310 (−44.4%) while sky luminance didn't move
(18.83 → 18.79). **Refuted and reverted — not one line survived.**

Same round, "EffectComposer lacks MSAA so `antialias:true` does nothing" was kept:
`renderTarget.samples` 0 → 4, Laplacian edge-noise variance on the track wireframe
352.71 → 244.30. Re-measured later with a completely independent harness on different
frames and regions: −42.3%. Same direction, same magnitude.

**That's the whole point**: three of four hypotheses were wrong, and none of the wrong
ones made it into the code.

## Install

Zero third-party dependencies — Python standard library only (3.8+).

```bash
pip install git+https://github.com/flufy3d/hypoloop
```

Then install at least one CLI agent and log into it:

```
$ hypoloop backends
name    what it is                  installed  default models
agy*    Antigravity CLI (Gemini)    yes        gemini-3.8-flash-medium / gemini-3.8-flash-high
codex   OpenAI Codex CLI            yes        gpt-5.6-terra:medium / gpt-5.6-terra:high
claude  Claude Code CLI             yes        sonnet / opus
```

If the binary isn't found, point at it with `HYPOLOOP_AGY` / `HYPOLOOP_CODEX` /
`HYPOLOOP_CLAUDE`. (agy isn't on PATH after install — run `agy install` once.)

## Use

```bash
hypoloop "task description"              # run in the target project dir, 2 rounds
hypoloop "task" -C /path/to/project
hypoloop "task" --backend codex          # run it on a different CLI
hypoloop "task" --rounds 3 --commit      # one commit on a hypoloop/<run> branch
hypoloop "task" --dry-run                # print the prompts, spend nothing
hypoloop "task" --rounds 5 --until "zero unavoidable deaths, difficulty constants untouched"
hypoloop backends / quota / history
```

| Flag | What it does |
|---|---|
| `--backend {agy,codex,claude}` | Which CLI to use. **Defaults to the first one installed, and says so at startup** |
| `--rounds N` / `--hypotheses N` | Rounds (default 2) / hypotheses per round (default 3) |
| `--model` / `--verifier-model` | Override per role; empty means the backend's own mid/high tier |
| `--evidence-hint` | Advice for the verifier. **Empty by default, and usually should stay empty** (see below) |
| `--until "<condition>"` | Stop early once met (see below) |
| `--commit` | Whole run collapses into **one** commit on a `hypoloop/<run>` branch |
| `--allow-dirty` | Run even if the target isn't a clean git repo |
| `--save-config` | Save these settings as the project default (models stored per backend) |

## Design notes

### 1. "Only the verifier writes" is a mechanism, not a prompt request

A prompt saying "don't edit files" breaks the day the model ignores it — silently.
So: read-only roles run in each CLI's own read-only mode (agy `--mode plan`, codex
`-s read-only`, claude `--permission-mode plan`), **and** `guard.py` fingerprints the
worktree before and after each read-only step and aborts the round on any mismatch.

The second layer matters more with multiple backends: **when you add a new one, nobody
knows how airtight its read-only mode really is.** The fingerprint is ours and holds
for all of them. (Salvage calls are inside the fingerprint too — otherwise a read-only
role's resume would be the one unwatched write path in the system.)

The fingerprint covers *content*, not just filenames: `git status --porcelain` only
lists untracked paths, so untracked file contents are hashed separately.

### 2. Falsifiability is forced by the schema

Every hypothesis **must** carry `predicted_observable`: if this is true, which
measurable quantity moves, and how. "It'll look nicer" is not an answer. The verifier
returns `supported` / `refuted` / `inconclusive` per hypothesis with **concrete
before/after readings**, and reverts anything it refuted.

`inconclusive` is a first-class outcome — "couldn't measure it" beats inventing a number.

**How to gather evidence is the verifier's problem.** It has a full shell: run existing
tests, write a throwaway script, start a local server, install a headless browser. The
only boundary is that tooling and temp artifacts must not be left in the target repo.

### 3. Backends are pluggable — adding one is two steps

The contract lives in `backends/base.py` and asks for four things:

1. Run a prompt non-interactively with the prompt **on stdin** (not argv — Windows caps
   a command line at 32767 chars, and a prompt carrying code context will eventually
   blow up on some large project with an "argument too long" error that points nowhere
   near the real cause)
2. **Structured output** against a supplied JSON Schema
3. A **read-only mode**
4. Per-call usage, ideally remaining quota too

To add one (opencode, aider, whatever): write a `Backend` subclass, add a line to
`_MODULES` in `backends/__init__.py`. **The loop, role prompts, guard, ledger and
report don't change at all.**

| | agy | codex | claude |
|---|---|---|---|
| Structured output | `--json-schema <file>` → `structured_output` | `--output-schema <file>` + `-o` last message | `--json-schema '<JSON literal>'` → `structured_output` |
| Read-only | `--mode plan` | `-s read-only` (**OS-level sandbox**) | `--permission-mode plan` |
| Write | `--mode accept-edits` | `-s workspace-write` + network on | `--permission-mode acceptEdits` |
| Resume | `--conversation <id>` | `exec resume <thread_id>` | `--resume <session_id>` |
| Two tiers | flash-medium / flash-high | same model, `model_reasoning_effort`, written `model:effort` | sonnet / opus |

### 4. Every CLI has a way to fail *silently*. All of these were hit for real

**agy**

| Trap | Symptom | Fix |
|---|---|---|
| Ignores the child process cwd | Without `--add-dir` the agent works inside `~/.gemini/antigravity-cli/scratch`, **reporting `status=SUCCESS` the whole way**: "file created" — under scratch; "read the code" — it saw an empty directory | Always pass `--add-dir <target>` |
| Headless auto-denies permissions | *"a tool required the read_file permission that headless mode cannot prompt for, so it was auto-denied"* — the read-only roles can't even read | Give every role `--dangerously-skip-permissions`; read-only is enforced by `--mode plan` + the fingerprint, not by withholding this flag |
| `--disable-slash-commands` silently voids `--mode` | agy warns once, then the read-only role **quietly becomes a writing role** | Don't use that flag |

**codex**

| Trap | Symptom | Fix |
|---|---|---|
| OpenAI strict schemas | Every object needs `additionalProperties: false` and every field in `required`, else HTTP 400 `invalid_json_schema` — the step dies without spending a token | `strictify()` mechanically rewrites the schema to a strict copy in a temp file. **The originals are untouched** — one backend's serialization limits must not rewrite the system's semantics (see §7 on why `goal` is optional) |
| Exit code masks the real cause | The upstream 400 is spelled out in the event stream, but hardcoding "exit code 1" first shows a message with no relation to the actual problem | Read `error`/`turn.failed` from the event stream **first**, fall back to the exit code; and dig the innermost human-readable `message` out of the nested JSON |
| No `--print-timeout` | On timeout we must kill the process, and `capture_output` then yields nothing — **not even `thread_id`**, so salvage is dead | Stream stdout to disk and parse that. After the kill you can still recover `thread_id` and any already-complete structured message |
| `exec resume` rejects `-s` / `-C` | Sandbox and cwd are inherited from the original session. Passing them fails immediately, with "to pass '-s' as a value, use '-- -s'" — unrelated to the real cause | Only send the flags it accepts. **Found by actually running a "remember this word → resume and ask for it" round trip**: agy and claude passed, codex was silently broken. Checking the first three argv entries would never have caught it |
| No `total_tokens` | You add it yourself, and `cached_input_tokens` is a **subset** of `input_tokens` — summing all three inflates the bill | `total = input + output` |

**claude**

| Trap | Symptom | Fix |
|---|---|---|
| `--json-schema` wants a literal, not a path | A path gives "--json-schema is not valid JSON", which reads like a broken schema but is a wrong argument shape | Read the file, pass the contents |
| `--dangerously-skip-permissions` tears down plan mode | It *is* bypassPermissions | Read-only roles don't get it. claude has no "headless auto-denies reads" problem, so plan mode already allows reads |
| cache_read distorts the bill | It's a separate pricing tier (~1/10 of fresh input) and dwarfs everything in long sessions | Excluded from `total_tokens`, reported in its own column |

### 5. Quota is the **real** number — and all three probes are free

Three completely different mechanisms, so there's no shared abstraction — only a shared
reading type (`quota.py`):

| | How | Costs quota? | Sturdiness |
|---|---|---|---|
| **agy** | `agy -v=3 models` dumps HTTP bodies into `~/.gemini/antigravity-cli/log/cli-*.log`; read `v1internal:retrieveUserQuotaSummary` | No (lists models, generates nothing) | `-v=3` is an **undocumented** glog flag and can vanish |
| **codex** | `account/rateLimits/read` on `codex app-server` | No (reads account state) | An **official** protocol method (`codex app-server generate-json-schema` exports the whole protocol) — sturdier than agy's path |
| **claude** | `GET /api/oauth/usage`, borrowing the token Claude Code already maintains | No | Same number you see typing `/usage` in claude |

Everything is normalized to "percent remaining": agy natively reports
`remainingFraction`, codex and claude report `used_percent`, converted in their own
backends. That makes "this run cost X" work identically across all three.

The claude token is **read-only**: never stored, logged, written back, or refreshed —
refresh tokens rotate, and racing to refresh would evict Claude Code's own copy. A test
watches the source for any write-back.

codex only reports **integer** percentages, so a two-round run can show "0 percentage
points consumed." The output says so explicitly, otherwise you'd think it was free.

agy's quota is **per model group** (Gemini in one, Claude+GPT in another), so you must
pick the group matching your model or you'll report someone else's numbers.

The approach and its pitfalls come from
[flufy3d/taiji](https://github.com/flufy3d/taiji)'s `hub/service/quota.py`.

**A failing probe never affects the run**: it says "couldn't read it" and why. It never
invents a number, and never blocks the task.

### 6. Environment facts are auto-detected; **task conclusions are never pre-fed**

`--evidence-hint` is easy to misuse. It looks like a handy place to dump everything you
know — until you dump this in:

> "Tiers in this project come from pickups, not mileage — the mileage axis is empty."

That sentence is **the hypothesizer's job**. Pre-feed it and the round can no longer
demonstrate that the system finds problems on its own; it just recites your answer.
This was hit for real. Hence four layers:

| Layer | Produced by | Example |
|---|---|---|
| **Backend facts** (this CLI's properties) | `Backend.env_notes()` | "agy's built-in browser tool can't install its driver on this machine" |
| **Machine facts** | `environ.py` auto-detection | "ms-playwright is cached, so installing playwright won't re-download 100MB" |
| **Evidence hints** (your optional nudge) | `--evidence-hint`, **empty by default** | "this service needs docker-compose up first" |
| **Task conclusions** (what to change) | **Only** the three roles | — |

The first two are separate because "agy's browser is broken" is true only for agy —
repeating it under codex is pure noise and actively misleading. Both layers report only
what was **actually observed** on this machine: no network, no quota, no guessing. A
test asserts the injected block contains nothing project-specific.

### 7. Lessons burned into the code

**Trust the payload, not the envelope.** A verifier call once returned
`status: ERROR` + "The stream was interrupted" while `returncode` was 0 and
`structured_output` was complete — the agent had finished, the CLI just never reset the
envelope. Checking only `status` threw away 4 supported + 1 refuted verdicts, paid for a
pointless resume, and wrote a lie into a commit message ("round 2: aborted").
`Call.degraded` accepts any complete payload; `audit.py` then guards the whole *class* of
this bug by comparing disk — every step writes `<stem>.raw.json` (raw) and `<stem>.json`
(accepted), and anything with a usable payload but no accepted file gets reported loudly
**before** the commit. Tested against 5 historical runs: caught the one incident, silent
on the other four.

**Accepting a verdict ≠ accepting a correction.** The challenger showed a displacement
was computed as full collider width (3.70 m) instead of the correct 4.35 m. The verifier
accepted the `revise` verdict and wrote 3.70 into the code anyway — a 9% unavoidable-death
hole found only in the next round. Root cause was the schema: the verdict was structured,
the numeric correction was buried in free text. Now `corrections` is a first-class
required field (`id`/`what`/`wrong`/`correct`/`basis`), the verifier must account for each
one (`applied`/`rejected`/`not_applicable`), and coverage is **checked mechanically** by
id. Rejections are allowed — invisible ones aren't.

### 8. `--until`: stop when done, but **always err toward one more round**

`--rounds 5` on a task that converges in round 2 burns a million+ tokens per extra round.
Worse, with nothing left to fix it will manufacture three hypotheses and start touching
things it shouldn't.

```bash
hypoloop "fix the unavoidable spawns" --rounds 5 \
  --until "zero unavoidable deaths across all speeds, difficulty constants untouched"
```

Write a **decidable** condition. "It got better" isn't one.

The verifier decides, but **with readings**: when `goal.met=true`, `evidence` must
address every clause. The evidence is printed verbatim so you can challenge it on the
spot. **The failure direction must be "run another round," never "stop early wrongly"** —
an extra round costs tokens; a wrong stop leaves unverified changes in someone's repo.
So it does *not* stop when: no `--until`; the round errored; `goal` is missing or `null`
(it's optional in the schema — missing means never judged); `met` isn't boolean `true`;
or `evidence` is empty (and it says explicitly that the missing evidence is why).

`goal` being optional is the foundation of that safety, which is why it survived codex's
strict schema as "required but nullable" — `null` maps one-to-one onto the old "missing,"
and the reader is unchanged (§4).

## Where things land

Everything under `~/.hypoloop/`:

```
~/.hypoloop/
  runs/<timestamp>-<task>/
    roundN-{hypotheses,critique,verification}.json   # structured outputs
    roundN-*.prompt.md                               # exact prompts sent, reproducible
    roundN-*.raw.json                                # raw backend response, incl. usage
    report.md
  ledger.jsonl                                       # token ledger across runs
  projects/<hash>.json                               # per-project defaults (models per backend)
```

**Not one byte lands in the target project** — only the verifier's legitimate source
edits. Hard constraint: when you point this at someone else's repo, it must not add
anything to their `git status`.

(Models are stored per backend for the same reason: `--model gemini-3.8-flash-high
--save-config` saves a name that means something to exactly one CLI. Reusing it under
`--backend codex` would hand codex a Gemini model name.)

## Development

```bash
pip install -e .
python -m unittest discover -s tests -t .
```

Every fixture is a verbatim capture of a real response. Hand-written fixtures only prove
the parser matches what's in my head, not what these CLIs actually emit.

## License

MIT
