# Two-round verification on a pricing change

Compare the tagged 1.2.3 wheel at `1c9d08609fc474dc1ee41f915f2d58a8057cfc90` to
the candidate wheel. The driver records the wheel and harness digests. Each arm
starts with an isolated Git fixture and an already-implemented execution. The
first edit breaks the negative quantity clamp. The second edit varies by case.
The clean control starts clean. The final charge has no dedicated test, so only
independent inspection can discover its regression.

| Case | Expected second observation |
| --- | --- |
| replacement | Close the negative-quantity finding and discover incorrect total |
| persistent | Keep the same finding open across moved or reworded evidence |
| deleted | Keep the negative-quantity finding and assertion coverage open |
| repair | Close the negative-quantity finding with the current passing check |
| clean | No blocker or false block in either round |

Run the same driver using each wheel's Python with `-I`. Supply `--mode replay`
or `--mode live`, `--case`, `--wheel`, `--revision`, and a separate `--report`
path per arm. The runner uses the installed library's CLI entry points. The live
reviewer sees only the current fixture, its session and supported review context.
The labels and expected finding text stay in the driver outside the fixture.

Use one live pair per case for this initial qualification. Alternate the first
arm between cases. Retain actual diagnostic output, time, provider version and
available token counts. Score closure by the original finding identity and
discovery from the review before adding runtime test failures. A scripted reviewer
measures data handling. Only live reviews contribute to reviewer accuracy.

Keep follow-up advisory if any labeled finding is falsely closed, any independent
defect caught on baseline is missed, a clean case is blocked, required evidence
is missing, or the sample cannot establish a reliable blocking policy. Record
unclosed true fixes separately. Report total command time, pair count and observed
latency range. Dollar cost requires a measured billing source.

## Observed results

On Windows, Codex CLI 0.168.0 with `gpt-6.2-sol` reviewed five matched pairs.
Each observation contains two verify rounds and two real invocations of the fixture
command. Baseline round two exposed a review and verdict without an execution-scoped
closure decision. The paired candidate recorded the following outcomes.

| Case | Baseline verdict | Candidate verdict | Candidate finding outcome | Baseline / candidate seconds for both rounds |
| --- | --- | --- | --- | --- |
| [Replacement](verify-followup/candidate-live-replacement.json) | fail | fail | A closed, independent B found | 49.00 / 64.31 |
| [Persistent](verify-followup/candidate-live-persistent.json) | fail | fail | Same A remains open | 45.57 / 58.28 |
| [Deleted assertion](verify-followup/candidate-live-deleted.json) | fail | fail | A remains open, reviewer also reports the missing check | 55.06 / 58.05 |
| [Repair](verify-followup/candidate-live-repair.json) | pass | pass | A closed with a fresh receipt | 59.66 / 64.15 |
| [Clean](verify-followup/candidate-live-clean.json) | pass | pass | Zero open findings | 71.16 / 76.13 |

Across the four cases starting with A, two of two true repairs closed and zero
of two surviving defects closed. Both reviewers found B in the one labeled
independent-discovery case. The single clean control passed on both arms.
The deleted-assertion command exited zero in both arms. In scripted replay where
the reviewer also missed the defect, baseline returned pass and the candidate
retained the finding and reported incomplete coverage.
[Baseline replay](verify-followup/baseline-replay.json) · [Candidate replay](verify-followup/candidate-replay.json)

Median observed time across the five two-round runs was 55.06 seconds for baseline
(range 45.57–71.16) and 64.15 seconds for the paired candidate (58.05–76.13).
Provider-reported input/output tokens across those five runs were
1,515,116 / 12,693 and 1,525,108 / 14,595 respectively. Cached input is included.
The experiment did not measure billed cost, human interventions, native host transport
or performance on other models. Concurrent runs and cache state can affect timing.

The raw reports identify each installed wheel. The paired candidate wheel digest
begins `5239173e3f73`. After further checks for stale publication and finding routing,
the final candidate wheel (`11d7a2171b05`) repeated all five scripted cases and
the live replacement case. On that repeat it closed A and found B in 58.35 seconds.
The final live repeat is outside the five paired measurements.
[Final live repeat](verify-followup/final-live-replacement.json)

**Decision.** Keep finding follow-up advisory for release decisions. The fixture
supports continuity for the observed cases. One independent defect, one model and
one live pair per case give limited information about false blocks across real projects.
An obligation with a missing original assertion still produces the existing coverage gap
when its due stage is verify.

### Replay after publication and assertion corrections

A sixth control leaves pricing broken, replaces the assertion with a print, and has
the scripted reviewer explicitly endorse the replacement. The test command exits
zero in both arms. Tagged v1.2.3 reports pass. The corrected candidate reports
incomplete, preserves the finding ID, and keeps the quantity obligation open.
Its repair and clean controls both pass. Each report records the wheel digest and
the number of installed package files compared byte for byte before the run.
The live measurements above refer to the earlier wheels.
[Baseline negative control](verify-followup/correction-baseline.json) · [Six candidate replays](verify-followup/correction-replay.json)

