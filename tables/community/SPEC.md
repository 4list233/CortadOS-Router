# Community sentiment as a routing signal (Forest 2026-09-23)

Public benchmarks say how strong a model is; they do not say what it is *known for*. Practitioners do: "Fable is the one for
strategy memos", "Astra for 3D / CAD code", "Gemini Flash for long-document summaries". This pipeline turns that talk into a
small, bounded adjustment per (model family, use case), refreshed nightly, and into training material for the scorer.

## Collection (nightly cron `community-rating`, 06:30, subscription brain, never a local model)
The job web-searches the last 7 days for each family in `tables/inventory/` (Fable 5.1, GPT-6 Astra, Opus 5.5, Sonnet 5,
GPT-5.6 Sol/Terra, Gemini 3.8 Flash / 3.1 Pro, Grok 4.7, Kimi K3, GLM 5.2, Muse Spark, Composer 2.5, Qwen3.5 27B): forums,
HN, Reddit, X, vendor changelogs, practitioner blogs. For every concrete claim it records one line in
`tables/community/claims.jsonl`:

    {"ts", "family", "use_case": one of the 7 work kinds or "other:<what>", "direction": "+1|-1", "claim": "<one sentence>",
     "source_url", "source_kind": "forum|blog|vendor|benchmark-thread|social", "specificity": 1-3, "evidence": "anecdote|comparison|measured"}

Rules the job follows: one claim per source per (family, use case); vendor marketing counts as `vendor` and never alone;
"X is amazing" without a task is not a claim; it must quote or paraphrase the actual statement.

## Noise filter (`fold_sentiment.py`, runs inside `build_tiers.py`)
- a (family, use case) pair needs ≥ 3 independent sources (distinct domains) in the last 30 days to count at all;
- weight = specificity × {anecdote 1, comparison 2, measured 3} × source-kind {vendor 0.5, social 0.7, forum 1, blog 1, benchmark-thread 1.5};
- net = Σ(direction × weight) / Σ(weight), in [−1, 1]; adjustment = ±`max_points` (default 8) × net, clipped;
- applied to that family's use-case index only, never to the general ability, never above 100.
- everything is written to `tables/community-sentiment.json` with the supporting claims, so a bad adjustment can be traced and deleted.

## Training material
Claims with `evidence ∈ {comparison, measured}` and specificity 3 become teacher context: when the nightly labeller sees a
request whose work kind matches, the claim is appended to the teacher prompt as background ("practitioners report …"), so the
soft labels learn task→specialisation associations rather than only difficulty. Nothing is trained on until it has passed the
3-source filter.
