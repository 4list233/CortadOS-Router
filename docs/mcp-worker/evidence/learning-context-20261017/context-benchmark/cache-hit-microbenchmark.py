"""CPU-only exploratory exact-score memoization cost; not production cache or end-to-end routing."""
import copy,hashlib,json,statistics,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
manifest=(ROOT/'manifest.json').read_bytes();revision=hashlib.sha256(manifest).hexdigest()
fixtures={r['id']:r for r in json.loads((ROOT/'synthetic-cases.json').read_text())}
paired=json.loads((ROOT/'result.json').read_text())
# Iterations:1000 gives a stable local lookup distribution with tinyCPUcost;
# fewer make percentiles noisy, more does not establish workload-level hit rates.
REPETITIONS=1000
results=[]
for pair in paired['pairs']:
    fixture=fixtures[pair['id']];value=pair['strategies']['baseline']['result']
    key=(revision,'interactive',hashlib.sha256(fixture['request'].encode()).hexdigest());cache={key:copy.deepcopy(value)}
    samples=[]
    for _ in range(REPETITIONS):
        start=time.perf_counter_ns()
        current=(revision,'interactive',hashlib.sha256(fixture['request'].encode()).hexdigest())
        returned=copy.deepcopy(cache[current]);elapsed=time.perf_counter_ns()-start
        assert returned==value
        samples.append(elapsed/1000000)
    ordered=sorted(samples)
    results.append({'id':pair['id'],'request_chars':len(fixture['request']),'cache_hit_samples_ms':samples,'median_ms':statistics.median(samples),'p95_ms':ordered[int(.95*len(ordered))-1],'max_ms':max(samples),'exact_returned_score_equal':True,'baseline_contended_inference_ms':pair['strategies']['baseline']['wall_ms']})
report={'prototype_only':True,'gpu_model_loaded':False,'production_cache_changed':False,'does_not_measure_quota_admission_or_route_resolution':True,'cache_hit_rate_not_estimated':True,'repeat_count':REPETITIONS,'results':results,'required_policy':'Cache only exact score inputs + model/prompt/tokenizer revision. Fresh quota/admission/ranking still runs on every request. Shared existing execution_routing cache owner must be extended/extracted if implemented.','source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
(ROOT/'cache-hit-microbenchmark.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'results':[{'id':r['id'],'median_ms':r['median_ms'],'p95_ms':r['p95_ms']} for r in results]}))
