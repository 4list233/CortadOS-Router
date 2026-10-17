# S1 context and latency experiment — 2026-10-17

All six baseline cells at 2,048, 4,096 and 8,192 input tokens completed. The proposed faster vocabulary projection was rejected because it changed a scoring probability. No serving code, model promotion, nightly trainer, launchd job, or production limit was changed.

These are measurements during active nightly GPU training on a 32 GiB Apple M1 Max. They are neither normal idle latency nor a routing accuracy evaluation. The two inputs are synthetic: a bounded local documentation edit and an externally visible production migration. The exact instructions and final risk cues were retained at every size.

| First model input tokens | Local edit latency | Production migration latency | Highest MLX peak |
|---:|---:|---:|---:|
| 2,048 | 10.98 s | 11.21 s | 4.25 GB |
| 4,096 | 16.71 s | 29.17 s | 5.07 GB |
| 8,192 | 49.93 s | 30.82 s | 7.52 GB |

Token counts include the system prompt, chat template and first forced question prefix. The requests themselves were approximately 7,500, 18,000 and 39,000 characters. This experiment called Scorer directly to avoid the existing 6,000-character service boundary; it does not increase that deployed boundary. CPU tokenization alone took approximately 2, 4 and 9 ms.

The original scorer already uses one full prompt prefill followed by 11 short cached continuation passes. It does not reread the whole prompt 12 times. Each answer conditions later questions. A fresh KV cache was used for every request, exactly as the current scorer does; no persistent or deep-copied prefix cache was used.

The candidate moved the vocabulary projection after slicing the last hidden position. At 2k tokens it took 10.22 s versus 10.98 s for the local edit and 9.71 s versus 11.21 s for the migration. The migration parallelizability probability differed by 0.0798828784, exceeding the predeclared 0.0001 tolerance. One digit logit changed from 77.0 to 77.5. All observed discrete answers and post-policy bands happened to match, but that does not make the confidence change acceptable. The comparison stopped after two pairs, retaining the failed result; the remaining four optimization pairs were not run. The separate larger-context continuation used the unchanged baseline only.

The 4k/8k continuation completed in 129.67 seconds. The nightly trainer retained the same PID and process start identity and advanced from reported iteration 110 to 120. However, system swap use rose from 0 to 2,102.19 MB. The MLX memory setting is a guideline: MLX peak reached 7.52 GB while process RSS remained around 2.60 GB. The free-memory percentage and RSS guards therefore did not contain unified GPU memory pressure. These observations do not establish zero impact or zero slowdown. Further GPU experiments during the active nightly run are not recommended.

The current base configuration advertises 262,144 positions, while the promoted adapter training configuration uses a 1,536-token sequence limit. Neither the model maximum nor this small synthetic capacity test establishes reliable long-context routing. The observed 4k/8k contended latencies exceed the current 15-second router timeout; this is evidence about concurrent training conditions, not proof that idle 4k inference always exceeds that timeout.

A CPU-only exact-score cache prototype measured approximately 0.018 ms per repeated hit, including full-input hashing and copying the score. This excludes fresh subscription checks and route resolution, does not estimate real hit rate, and is not deployed. The existing execution_routing cache owner should be extended or extracted if this approach is adopted. Cache only scores for identical scorer inputs and model/prompt/tokenizer/calibration revisions; quota checks and scarcity ranking must still run for every route.

QA also received exact offline tokenizer captures at the 1,536/1,537-token training boundary using installed mlx_lm ChatDataset.process and TokenizerWrapper. The complete examples contain 69 supervised tokens, with prompt-mask offsets 1,467 and 1,468 respectively. ChatDataset uses the template tokens verbatim and does not add an extra BOS/EOS. These captures loaded neither MLX core nor model weights.

Every numeric result is backed by the raw JSON files listed in INDEX.json. Private adapter snapshots and installed-source copies are excluded from the public evidence list. No benchmark child remains running.
