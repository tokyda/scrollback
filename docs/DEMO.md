# Scrollback: three-minute demo script

This script follows the demo outline in DESIGN §8. Steps 1, 2 and 5 use the synthetic sample
and the README. Steps 3 and 4 use the live Telegram bot on the owner's real export. Timings are
targets. Each step lists what to show, the exact command or message, and what to say.

## Before the demo (10 minutes ahead)

1. **Synthetic warehouse ready** (for step 1 and the fallback):
   ```bash
   uv run scrollback ingest data/sample/user_data_tiktok.json --config config.sample.yaml
   ```
2. **Start the bot** on the real configuration, in its own terminal, and leave it running:
   ```bash
   uv run scrollback serve bot
   ```
   Check that the start-up line shows Haiku E6b, `aggregate_only` and the current commit. If it
   says another bot holds the lock, stop that one first: only one process can poll the token.
3. **Warm it up.** Send `/today` from the phone once, so the knowledge index and the API
   connection are warm and the first live answer isn't slow. Delete the chat messages
   afterwards if you don't want them on screen.
4. **Screens.**
   - Terminal: large font, in the repository folder, history cleared.
   - Browser: the README on GitHub, scrolled to "Estimating watch time without durations";
     a second tab at "Results".
   - Phone: Telegram open in the bot chat, mirrored to the screen (QuickTime "New Movie
     Recording" with the iPhone as camera works on a Mac), or the Telegram desktop app.
5. **Notifications off** on the laptop and phone.

Expected API spend for the whole demo: under $0.01.

## 0:00 – 0:30 · The problem: the export has no durations

**Show** the synthetic export in the terminal:

```bash
jq '."Your Activity"."Watch History".VideoList[:3]' data/sample/user_data_tiktok.json
```

**Say:**
- "This is what TikTok's data export gives you: a timestamp and a link for each view. This
  file is synthetic, generated to have the same shape as a real export."
- "There is no duration field anywhere. So 'how much did I watch last week?' has no direct
  answer. The data only says when each video started."
- Point at the three times (11:45:26, 11:46:01, 11:46:17, newest first): "35 seconds, then 16
  seconds. The gaps between views are all we have."

## 0:30 – 1:00 · How watch time is estimated

**Show** the README section "Estimating watch time without durations" (the timeline diagram).

**Say:**
- "Each view is credited with the gap to the next view, capped. A long gap means the person
  looked away, not a four-minute video."
- "A gap of more than 30 minutes ends a session. The last view in a session has no next view,
  so it gets the median of the others."
- "I calibrated the cap against my iPhone's Screen Time. A 180-second cap came out about 5%
  above Screen Time, 120 seconds about 8% below. Screen Time includes comments and browsing,
  so the estimate should sit below it: I use 120. The cost is that long videos are
  undercounted, and the agent says so."
- "This definition lives in the warehouse and in a metrics file, not in the model's head.
  That turns out to be the biggest lever on accuracy."

## 1:00 – 1:50 · A live question on my real data

**Send** in Telegram (free text):

> What share of my watch time is after midnight, and how does last month compare with the month before?

**While it runs (about 5–10 seconds), say:** "This is my real history. The bot runs Claude
Haiku with tools: it searches the metric definitions, writes SQL against the marts, and every
query passes a guard that only allows read-only, aggregate results."

**When the answer arrives, point at:**
1. The number and the comparison.
2. **Assumptions**: the exact dates it used for "last month" and "the month before".
3. **Metrics used**: `late_night_share` and estimated watch time.
4. The **caveat** that watch time is an estimate.
5. Tap **Show SQL**: "Every number traces back to a query I can read."
6. The footer: trace ID, model and cost, about a tenth of a cent.

**Backup question** if the first answer is slow or odd:

> How did my watch time per day change over the last 7 days of data? Show a chart.

(Or simply send `/week`, which always returns a seven-day table and a chart.)

## 1:50 – 2:20 · A question it cannot answer

**Send:**

> Which creator do I watch most?

**Point at** the refusal: it leads with "can't answer", explains that the export has video
links but no creator names, and offers what it can answer instead, without inventing a
number.

**Say:** "Refusing cleanly is part of being trustworthy. The eval set has unanswerable
questions and trap questions, and a refusal with a made-up number in it counts as a failure."

**Optional, if time allows:**

> How long have I watched TikTok today?

"The export always ends before today. The right answer is 'no data for today', not 0 minutes.
That was the hardest bad case to fix."

## 2:20 – 3:00 · How I know it works: the eval ladder

**Show** the README "Results" table, then scroll to "Three instructive bad cases".

**Say:**
- "96 questions with gold SQL, on synthetic data so anyone can reproduce them, each run three
  times and scored deterministically. 32 are held out and were run only for the baseline and
  the final configs."
- "Same agent, only the tables change: staging views get 51%, the modelled marts 92%. The
  data modelling is worth about 40 points."
- "Adding column descriptions fixed the main failure, session length measured as a clock span
  instead of watch time: 99.5%."
- "With retrieval and refusal rules, Haiku matches Sonnet, 100% on the held-out test, at
  about a ninth of the cost per question."
- "And the loop keeps going: the bot's `/week` once labelled every weekday one off. I tapped
  rated it bad from my phone, that became a candidate eval question, and it's now q097."

**Close:** "So: a real warehouse, explicit definitions, an agent that shows its work, and
evals that say how far to trust it."

## If something goes wrong

- **The bot doesn't reply within 20 seconds.** Keep talking through the step, then switch to
  the terminal and ask the same question on synthetic data:
  ```bash
  uv run scrollback ask "Which creator do I watch most?" --today 2026-11-20 --config config.sample.yaml
  uv run scrollback ask "What share of my watch time was after midnight in October?" \
    --today 2026-11-20 --show-sql --config config.sample.yaml
  ```
- **The answer is wrong.** Say so, tap 👎, and use it: "This is exactly how q097 got into the
  eval set." It is a better demo moment than a perfect run.
- **409 Conflict in the bot log.** Another process is polling the token; stop it (`kill` the
  PID the error names) and restart `serve bot`.

## Privacy during the demo

- Steps 3 and 4 show aggregates from the real export on screen, by the owner's choice. Don't
  show the raw real export, `data/raw/`, traces, or the bot's log.
- Don't record or publish the live bot part. The README GIF is recorded on synthetic data only.
