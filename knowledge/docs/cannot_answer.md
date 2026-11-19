# What this data cannot answer

The warehouse is built from a TikTok data export: a log of when videos were started, plus
likes, favourites, searches and shares. Some questions sound reasonable but this data cannot
answer them. Say what is missing instead of guessing.

## Not in the data at all

- **True watch durations.** There is no duration field. Watch time is an estimate from gaps
  between views (see watch time estimation); "exactly how long" questions can only be answered
  with the estimate and its caveats.
- **What a video was about.** No titles, captions, hashtags, sounds or categories. The export
  has a link and a numeric video ID only.
- **Creators.** No author or account names for viewed videos, so "which creator do I watch
  most?" cannot be answered. (An optional, off-by-default enrichment step could add titles and
  authors to `dim_video`; until it has run, creator questions are unanswerable.)
- **Ads.** Ad impressions are not in the watch history.
- **Whether a video was finished, skipped or looped.** Only starts are recorded.
- **App opens, screen time outside video viewing, notifications, DMs or live streams.** These
  are not modelled. A session is inferred from gaps between views, not from app open/close
  events.
- **Other people's behaviour, or how a video performed.** The export is one account's
  activity.
- **Device, location or network.**

## Only partly answerable

- **Anything before the export's history window or after the latest export.** Days outside
  the loaded range are unknown, not zero. Rewatch detection only sees views inside the window.
- **Exact local times**, if the export's timezone assumption ({source_tz}) turns out to be
  wrong; dates are shown in {local_tz}.
- **Likes of videos watched outside the history**: likes are counted, but may not match a
  view.
- **Search intent or results**: only the search text and time are known.

## How to answer

- State what is missing in one sentence and, if possible, offer the nearest answerable
  question (for example, number of views instead of minutes per video).
- Never invent creators, titles, durations or categories.
