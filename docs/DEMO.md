# Scrollback: demo script

The demo has two parts. **Part 1** shows what Scrollback does, live on the phone, before any
explanation. **Part 2** opens it up: the data problem, the warehouse, the agent and its guard.
Results get one sentence at the end, not a section.

Target length: about 4 minutes 30 seconds. For a 3-minute slot, use the cuts listed at the
end. The lines under **Say** are the exact words; the lines under **Do** are the actions.

Part 1 uses the live Telegram bot on the owner's real export (aggregate answers only). Part 2
uses the synthetic sample, the README and the dbt docs site.

## Before the demo (15 minutes ahead)

1. **Build the synthetic warehouse and the docs site** (free, no API calls):
   ```bash
   uv run scrollback synth --config config.sample.yaml
   uv run scrollback ingest data/sample/user_data_tiktok.json --config config.sample.yaml
   uv run scrollback docs --config config.sample.yaml -q
   ```
   Open `dbt/target/docs/static_index.html` in a browser tab. Click `fct_views` in the left
   panel, then the lineage button (bottom right) so the graph shows raw → staging →
   intermediate → marts.
2. **Start the bot** on the real configuration, in its own terminal, and leave it running:
   ```bash
   uv run scrollback serve bot
   ```
   Check that the start-up line shows Haiku E6b, `aggregate_only` and the current commit. If it
   says another bot holds the lock, stop that one first: only one process can poll the token.
3. **Warm it up.** Send `/today` once, so the knowledge index and API connection are warm.
   Then clear the chat history so the demo starts on an empty chat.
4. **Screens.**
   - Phone: Telegram open in the bot chat, mirrored to the screen (QuickTime "New Movie
     Recording" with the iPhone as camera works on a Mac), or the Telegram desktop app.
   - Terminal: large font, in the repository folder, history cleared. This is a second
     terminal, not the one running the bot.
   - Browser tab 1: the README on GitHub, scrolled to "Estimating watch time without
     durations".
   - Browser tab 2: the README "Architecture" diagram.
   - Browser tab 3: the dbt docs lineage graph from step 1.
5. **Notifications off** on the laptop and phone.

Expected API spend for the whole demo: under $0.01.

---

# Part 1: what it does

## 0:00 – 0:20 · Opening

**Do:** Phone on screen, empty bot chat.

**Say:**
> "I wanted to know how much TikTok I actually watch. TikTok doesn't tell you, and its data
> export doesn't either. So I built Scrollback: a data agent over my own watch history. I ask
> it a question in plain English, and it answers from a warehouse built from my export. Let me
> show you first, and then I'll show how it's built."

## 0:20 – 0:45 · A one-tap question

**Do:** Send `/today`.

**Say, while it runs:**
> "The slash-today command asks about the most recent day in my data. The export always ends a
> few days before I download it, so 'today' means the last day it covers."

**Do:** When the answer arrives, point at each part as you name it.

**Say:**
> "It gives the number first. Under it, the exact date it used, and a note that watch time is
> an estimate. I'll come back to why it's an estimate, because that's the core of the
> project."

## 0:45 – 1:35 · A free-text question

**Do:** Type and send:

> What share of my watch time is after midnight, and how does last month compare with the month before?

**Say, while it runs (about 5 to 10 seconds):**
> "This one isn't a canned command. The agent has to work out which metric 'after midnight'
> means, which dates 'last month' covers, write the SQL, and run it."

**Do:** When the answer arrives, point at the assumptions line.

**Say:**
> "Here it says which dates it took for 'last month' and 'the month before'. Here it names the
> metric it used: late-night share of estimated watch time."

**Do:** Tap **Show SQL**.

**Say:**
> "And this is the SQL that actually ran. Not SQL the model says it ran: the query that
> produced the number. So every answer can be checked."

**Do:** Point at the footer.

**Say:**
> "The footer has a trace ID, the model, Claude Haiku, and the cost: about a tenth of a cent."

## 1:35 – 2:00 · A question it can't answer

**Do:** Type and send:

> Which creator do I watch most?

**Do:** When the answer arrives, point at the first sentence.

**Say:**
> "The export has video links but no creator names, so it says that in the first sentence. It
> doesn't guess, and it offers what it can answer instead."

**Do:** Point at the 👍 and 👎 buttons.

**Say:**
> "Every answer has a thumbs up and down. A thumbs down asks what was wrong, and that becomes a
> candidate test question. That's what it does. Now, how it works."

---

# Part 2: how it's built

## 2:00 – 2:45 · The data problem: no durations

**Do:** In the terminal, run:

```bash
jq '."Your Activity"."Watch History".VideoList[:3]' data/sample/user_data_tiktok.json
```

**Say:**
> "This is the shape of TikTok's export. This file is synthetic, with the same shape as the
> real one. Each view is a timestamp and a link. There is no duration anywhere."

**Do:** Point at the three times (newest first): 11:46:17, 11:46:01, 11:45:26.

**Say:**
> "So all I know is when each video started. Sixteen seconds between the top two, thirty-five
> between the next two. The gaps are the only signal."

**Do:** Switch to browser tab 1, the timeline diagram.

**Say:**
> "So watch time is estimated. Each view gets the gap to the next view, capped at two minutes,
> because a long gap usually means I put the phone down. A gap over thirty minutes ends a
> session. The last view in a session has no next view, so it gets the median of the others.
> I checked the cap against my phone's Screen Time: the estimate lands a bit under it, which
> is right, because Screen Time also counts comments and browsing."

## 2:45 – 3:15 · The warehouse

**Do:** Switch to browser tab 3, the dbt lineage graph.

**Say:**
> "That definition is built into the warehouse, not left to the model. The export is loaded
> into DuckDB and modelled with dbt in layers. Raw keeps the records as exported. Staging
> types them and converts to local time. Intermediate computes the gaps and the sessions. The
> marts are the clean tables: views, sessions, a daily summary, a date table. Every layer has
> data tests, and every column in the marts has a description. The agent can only query the
> marts."

## 3:15 – 4:05 · The agent and its guard

**Do:** Switch to browser tab 2, the architecture diagram.

**Say:**
> "The agent is a plain tool-calling loop on Claude, no framework. It has four tools. It
> searches the metric definitions, which are embedded and indexed locally. It describes a
> table. It runs SQL. And it finishes with a final answer that has to cite which query
> results it used. That's why the SQL I showed is guaranteed to be the SQL behind the
> number."

**Do:** In the terminal, run:

```bash
uv run python -c '
from scrollback.agent.sql_guard import guard_sql, SqlGuardError
for q in ["SELECT * FROM fct_views", "DROP TABLE fct_views",
          "SELECT local_date, SUM(est_watch_sec)/60 AS minutes FROM fct_views GROUP BY 1"]:
    try:
        print("allowed :", guard_sql(q, result_mode="aggregate_only", max_rows=200).sql)
    except SqlGuardError as e:
        print("rejected:", e)
'
```

**Say:**
> "Every query goes through this guard before it runs. It parses the SQL. Anything that
> writes is rejected. Raw rows are rejected too: in this mode, only aggregates leave my
> machine, so individual viewing records never reach the model. An aggregate query passes,
> and it gets a row limit added."

## 4:05 – 4:30 · How I know it's right, and close

**Say:**
> "To check all of this, there's a set of 97 test questions with hand-written answers on
> synthetic data, scored automatically. The biggest lesson from it: the same agent on the raw
> tables gets about half right; on the modelled marts, over ninety percent. The data modelling
> mattered more than the prompt."

> "So that's Scrollback: a real warehouse with explicit definitions, an agent that shows its
> work and refuses what it can't know, and tests that say how far to trust it."

---

## Cuts for a 3-minute slot

- Drop `/today` (0:20 – 0:45). Start with the free-text question.
- Drop the dbt lineage graph (2:45 – 3:15). Say its first two sentences over the architecture
  diagram instead.
- Drop the 👍/👎 line at 1:35 – 2:00 and go straight to "That's what it does".

## If something goes wrong

- **The bot doesn't reply within 20 seconds.** Keep talking through the step, then switch to
  the terminal and ask the same question there. It runs the same config on the same real data:
  ```bash
  uv run scrollback ask "What share of my watch time is after midnight, and how does last month compare with the month before?" --show-sql
  uv run scrollback ask "Which creator do I watch most?"
  ```
  This only helps when Telegram is the problem: if the Anthropic API is down, the terminal
  fails too.
- **The answer is wrong.** Say so, tap 👎, and use it: "This is exactly how test questions
  get added. The bot once labelled every weekday one day off; a thumbs down turned that into
  question 97." It is a better demo moment than a perfect run.
- **409 Conflict in the bot log.** Another process is polling the token; stop it (`kill` the
  PID the error names) and restart `serve bot`.
- **The docs page is blank.** Re-run `uv run scrollback docs --config config.sample.yaml` and
  reload the tab.

## Privacy during the demo

- Part 1 shows aggregates from the real export on screen, by the owner's choice. Don't show
  the raw real export, `data/raw/`, traces, or the bot's log.
- Part 2 uses only the synthetic sample. The dbt docs site is built from the synthetic
  warehouse (`--config config.sample.yaml`), never from `real.duckdb`.
