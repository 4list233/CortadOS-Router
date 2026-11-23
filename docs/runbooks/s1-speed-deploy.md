# Runbook: deploy the S1 speed changes to the Studio (branch `s1-speed`, #132)

What changes on the scorer (`com.cortados.s1-scorer`, `~/cortados/.venv-mlx/bin/python ~/cortados-router/s1/serve.py`):
the system-prompt prefix cache (`student.PrefixCache`), full-queue-only admission with the deadline check at dequeue,
the service-time estimate that ages out (`CORTADOS_S1_SERVICE_MAX_AGE_S`, 300 s) and resets on a model load, and one
metadata-only request-log line per request. Router hosts get the no-retry-on-timeout/busy rule (`cortados_router/core.py`).

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
- `mismatched_requests` 0: the cached path gives the same answers as the uncached path on the same model
  (`max_prob_diff` is reported; a few thousandths are bf16 noise);
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
