# Final runs on the held-out test split

Two runs, once each, as DESIGN §7.5 sets out: the best Haiku config (E6b, owner decision
2026-11-22) and Sonnet's final config (E1). 32 test questions × 3 repeats each, at commit
`b257ad8` (the E6b dev run's code; only label and report commits since). **No
tuning after seeing these results** (owner instruction): no prompt, retrieval, scorer or
question change follows from them.

- `20261121T195214Z_e06b_cannot_answer_haiku`: E6b on `claude-haiku-5-5`.
- `20261121T195447Z_e01_descriptions`: E1 on `claude-sonnet-5-5`.

## Results (test; reported overall only, DESIGN §7.2)

| | Sonnet E0 (baseline) | **Sonnet E1** | **Haiku E6b** |
|---|---|---|---|
| **Test pass rate** | 96.9% (96.9–96.9) | **99.0%** (96.9–100) | **100%** (100–100) |
| Passing runs per repeat (of 32) | 31, 31, 31 | 31, 32, 32 | 32, 32, 32 |
| Failing runs | q062 ×3 | q094 r1 | none |
| Dev pass rate (for reference) | 91.7% | 99.5% | 99.5% |
| Cost per question (test run) | $0.0097¹ | $0.0099 | $0.0011 |
| Tokens per question | 8,663¹ | 9,563 | 15,196 |
| Latency | 6.0 s¹ | 6.5 s | 6.2 s |
| Total cost | — | $0.95 (estimate $1.23) | $0.11 (estimate $0.13) |

¹ The E0 baseline ran dev and test together; efficiency figures are over all 96 questions.

- **Both configs hold up on held-out questions.** Test is at or above dev for both, so the
  dev iterations didn't overfit in a way the test split can see.
- **Haiku E6b and Sonnet E1 are level.** One failing run on 96 is 1 point, so 100% vs 99.0% is
  within repeat variance. Haiku costs about a ninth as much per question ($0.0011 vs $0.0099).
- **q062 passes 3/3 on both**, the question E0 failed every time (session length as a span
  instead of estimated watch time). It is the test split's member of the session-length
  cluster that table and column descriptions fixed on dev.
- **Sonnet done-when** (DESIGN §7.5): the final test pass rate (99.0%) is at least E0's 96.9%.
  Met, along with the dev criteria met at E1.

## Failure (label confirmed by the owner, 2026-11-22)

| Run | Q | Rep | Label | What happened |
|---|---|---|---|---|
| Sonnet E1 | q094 | 1 | `missing_caveat` | "Exactly how many minutes … in binge sessions in October 2026?" is a trap. The answer, "291.3 minutes (about 4.9 hours) across 3 binge sessions", is right, but says it is an estimate only in its assumptions ("Watch time is estimated"), not in the answer text. The caveat check reads the answer. Sonnet E1 has no caveat rule; that rule is E6's, which only the Haiku ladder ran. |
