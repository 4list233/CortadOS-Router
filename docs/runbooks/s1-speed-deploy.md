# Runbook: deploy the S1 speed changes to the Studio (branch `s1-speed`, #132)

What changes on the scorer (`com.cortados.s1-scorer`, `~/cortados/.venv-mlx/bin/python ~/cortados-router/s1/serve.py`):
the system-prompt prefix cache (`student.PrefixCache`), full-queue-only admission with the deadline check at dequeue,
the service-time estimate that ages out (`CORTADOS_S1_SERVICE_MAX_AGE_S`, 300 s) and resets on a model load, and one
metadata-only request-log line per request. Router hosts get the no-retry-on-timeout/busy rule (`cortados_router/core.py`).

**The prefix cache is OFF by default** (`CORTADOS_S1_PREFIX_CACHE` unset; `on` enables it, owner 2026-11-23). With it off
the scorer scores every request in one full pass, as before s1-speed, and every other change above deploys as is:
steps 1, 3–5; step 2 is needed only before turning the cache on. The 2026-11-22 check found 28 of 200 requests
answering differently cached vs uncached; the cause is the split prefill, not the cache (see step 2), and the owner
decides on enabling it after review. To enable: add `CORTADOS_S1_PREFIX_CACHE=on` to the scorer plist's
EnvironmentVariables, then restart (step 3).

Unlike a promotion, `serve.py` / `student.py` changes need a scorer restart (the process keeps the old code). Run each
step on the Studio as the owner; nothing here is run by an agent without the owner's go.

## 1. Update the checkout (scorer still running the old code)

```sh
cd ~/cortados-router && git fetch origin && git status -sb      # clean; note the current HEAD for rollback
git checkout main && git pull --ff-only                         # after s1-speed is merged
```

## 2. Prefix-cache check, before the restart (opt-in; not part of the test suite)

Runs in its own process on the promoted model; the running scorer is not touched. It needs the memory of a second
loaded model (~5–6 GB for the current 8B base) for a few minutes, so run it when the nightly is not training
(`launchctl print gui/501/com.cortados.s1-continuous | grep state`, or run `s1/nightly.sh --stop` first).

```sh
cd ~/cortados-router
~/cortados/.venv-mlx/bin/python -B s1/check_prefix_cache.py --n 200
# optional: --requests <private.jsonl> (rows with "request") instead of the synthetic set; --max-growth-mb 256
```

One JSON line. Go on only with exit 0, which means all of:
- `rss_growth_mb` ≤ 256 over the 200 cached scores (the earlier prefix cache grew ~50 MB per call, ~10 GB here);
  `rss_every_50_mb` should be flat, not a staircase;
- `mismatched_requests` 0: the cached path gives exactly the answers of the uncached path with the prefill split at
  the prefix (`max_prob_diff` 0), i.e. the stored state and its per-request copy change nothing;
- `full_pass_*` (reported, not gated): the cached path against one full prefill pass, the path the scorer serves with
  the cache off. These differ: on the 4-bit/bf16 model, splitting the prefill at ANY point (256, 284, 512, 568, 569),
  with no cache involved, moves the first answer's logits by one bf16 step (0.125), and the difference grows over the
  twelve answers. That is the 2026-11-22 failure (28/200 requests, `max_prob_diff` 0.535). Measured 2026-11-23 on the
  same 200 synthetic requests, with no cache: split at 256 vs full 29 requests, split at 300 vs full 32, split at 256
  vs split at 300 31; cached vs split at the prefix 0 (`max_prob_diff` 0.0). The flipped answers are mostly close
  calls (e.g. urgency 0.4994 / 0.4994 full vs 0.3772 / 0.6219 split; median top-two margin 0.16) but not all (up to
  0.67). So about 15 % of requests change an answer under any bf16 rounding change: a property of the scorer, which
  the cache neither causes nor worsens and which the owner weighs before enabling it;
- `prefix_reused_tokens` > 0 (the cache was actually used).

Exit 1: do not deploy; paste the JSON into #132. Exit 2: it could not run (no MLX, `current.json` unreadable).

## 3. Restart the scorer

```sh
launchctl kickstart -k gui/501/com.cortados.s1-scorer
sleep 20; curl -s http://$(tailscale ip -4 | head -1):8431/health      # {"ok": true, ...} after the warm-up job
```

## 4. Watch the request log for the first minutes

`s1-requests.log` sits beside the scorer's stdout log (or at `CORTADOS_S1_REQUEST_LOG`). Each line:

```
s1-request ts= outcome= code= queue_wait_ms= service_ms= depth= bytes= tokens= prefix_tokens= expected_ms= prefix_slots= prefix_mb= peak_rss_mb=
```

- `prefix_slots` ≤ 2 and `prefix_mb` constant after the first requests;
- `peak_rss_mb` levels off within minutes. A climb of tens of MB per request is the old leak: roll back (step 5).
  The memory watchdog (`CORTADOS_S1_MAX_RSS_GB`, 6) restarts the process anyway, but that is a backstop, not a fix;
- `service_ms` well below the 2026-11-22 baseline (median 10.4 s); `outcome=busy` only under real backlog
  (`depth` near `CORTADOS_S1_MAX_PENDING`), `outcome=dropped` only for jobs whose caller had already gone or that had others waiting behind them.

```sh
tail -f <log dir>/s1-requests.log | awk '{print $3, $6, $7, $12, $13, $14}'
```

## 5. Rollback

```sh
cd ~/cortados-router && git checkout <HEAD noted in step 1> && launchctl kickstart -k gui/501/com.cortados.s1-scorer
```
