"""Synthetic actual-core measurements; correctness pass is NOT automatic efficiency approval."""
import asyncio
from collections import Counter
import gc
import hashlib
import json
import inspect
from pathlib import Path
import resource
import sqlite3
import statistics
import sys
import time
import tracemalloc
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
from acceptance.cowork_mcp.support import CoreCase,mod,owner,uid
OUT=Path(sys.argv[1]);report={'scope':'Actual local core with prior protected fake native; no real providers',
    'baseline':'baseline-ordinary.json: direct existing owners, not an equivalent prior remote service',
    'memory_acceptance':'Independent B review required: file-size-independent growth with chunk×active streams. No fixed RSS or latency SLO.',
    'selector_budget':'One indexed SELECT per unique selector; one indexed prefix artifact listing<=100 rows; bounded temporary sort allowed.',
    'cases':[],'correctness_pass':False,'efficiency_review':'pending'}

async def transfer_case(size,chunk,streams):
    case=CoreCase();await case.asyncSetUp();f=case.fixture
    # Synthetic MiB sizes exercise scaling. 120s prevents benchmarking overhead expiring leases;
    # it is not a deployed timeout choice. Each case owns fresh private state.
    f.core['limits'].update(file_bytes=16*1048576,task_bytes=64*1048576,installation_bytes=256*1048576,
        chunk_bytes=chunk,control_bytes=max(65536,chunk),uploads_per_task=2,downloads_per_task=2,
        ticket_ttl_s=120,transfer_idle_ms=120000,transfer_total_ms=120000,ipc_idle_ms=120000)
    sql=Counter();original=sqlite3.connect
    def connect(*a,**kw):
        db=original(*a,**kw);db.set_trace_callback(lambda q:sql.update([q.split()[0].upper()]));return db
    try:
        with patch.object(sqlite3,'connect',new=connect):await case.start()
        tasks=[await case.task() for _ in range(streams)]
        block=b'x'*chunk;digest=hashlib.sha256()
        for offset in range(0,size,chunk):digest.update(block[:min(chunk,size-offset)])
        expected=digest.hexdigest();gc.collect();tracemalloc.start();tracemalloc.reset_peak();start=time.perf_counter()
        async def upload(task):
            client=await case.connect();ticket=case.ok(await case.call('file_upload_ticket',{'task_id':task['task_id'],
                'request_id':uid(),'artifact_id':None,'filename':'opaque.bin','bytes':size,'sha256':expected,
                'media_type':'application/octet-stream'},client))['ticket']
            lease=case.ok((await case.stream('open_put',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:],
                'content_length':size},client=client))['result'])['lease_id']
            for offset in range(0,size,chunk):
                case.ok((await case.stream('put_chunk',{'lease_id':lease,'offset':offset},block[:min(chunk,size-offset)],client=client))['result'])
            artifact=case.ok((await case.stream('finish_put',{'lease_id':lease},client=client))['result'])['artifact']
            ticket=case.ok(await case.call('file_download_ticket',{'task_id':task['task_id'],'request_id':uid(),
                'artifact_id':artifact['artifact_id']},client))['ticket']
            lease=case.ok((await case.stream('open_get',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:]},client=client))['result'])['lease_id']
            actual=hashlib.sha256();offset=0
            while True:
                frame=await case.stream('read_chunk',{'lease_id':lease,'offset':offset,'max_bytes':chunk},client=client)
                value=case.ok(frame['result']);actual.update(frame['body']);offset=value['offset']
                if value['eof']:break
            case.assertEqual(offset,size);case.assertEqual(actual.hexdigest(),expected)
            case.ok((await case.stream('close',{'lease_id':lease,'reason':'complete'},client=client))['result']);await client.close()
        await asyncio.gather(*(upload(t) for t in tasks))
        current,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        timing=[]
        for _ in range(50):
            now=time.perf_counter_ns();case.ok(await case.call('task_get',{'task_id':tasks[0]['task_id']}));timing.append((time.perf_counter_ns()-now)/1e6)
        plans={};connection=original('file:'+str(f.path/'state/jobs.db')+'?mode=ro',uri=True)
        selectors={'task':'SELECT * FROM remote_tasks WHERE owner_id=? AND task_id=?',
            'replay':'SELECT * FROM remote_operations WHERE owner_id=? AND operation=? AND request_id=?',
            'artifact':'SELECT * FROM remote_artifacts WHERE owner_id=? AND task_id=? AND artifact_id=?',
            'listing':'SELECT * FROM remote_artifacts WHERE owner_id=? AND task_id=? ORDER BY created_at_ms,artifact_id'}
        args={'task':(owner(f.principal),tasks[0]['task_id']),'replay':(owner(f.principal),'task_create',uid()),
            'artifact':(owner(f.principal),tasks[0]['task_id'],uid()),'listing':(owner(f.principal),tasks[0]['task_id'])}
        for name,query in selectors.items():
            plan=connection.execute('EXPLAIN QUERY PLAN '+query,args[name]).fetchall();trace=[]
            connection.set_trace_callback(trace.append);rows=connection.execute(query,args[name]).fetchall();connection.set_trace_callback(None)
            assert len(trace)==1 and not any('SCAN ' in p[3] for p in plan),(name,plan,trace)
            if name=='listing':assert len(rows)<=100
            plans[name]={'plan':plan,'selector_statements':len(trace),'rows':len(rows)}
        connection.close()
        return {'payload_bytes_per_stream':size,'chunk_bytes':chunk,'active_streams':streams,'wall_s':time.perf_counter()-start,
            'tracemalloc_current_bytes':current,'tracemalloc_peak_bytes':peak,'process_highwater_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'memory_scope':'Both Python IPC endpoints+core in one process; no full-file client payload. RSS includes prior cases/imports, not per-operation allocation.',
            'task_get_raw_ms':timing,'task_get_median_ms':statistics.median(timing),'all_sql_statement_counts':dict(sql),'selector_plans':plans}
    finally:
        if tracemalloc.is_tracing():tracemalloc.stop()
        await case.stack.aclose()
        while case._cleanups:
            fn,args,kwargs=case._cleanups.pop();value=fn(*args,**kwargs)
            if inspect.isawaitable(value):await value

async def monitor_case():
    case=CoreCase();await case.asyncSetUp();f=case.fixture
    f.core['limits'].update(file_bytes=4096,workspace_poll_ms=100)
    f.control(output_hex={'overrun.bin':(b'x'*8192).hex()},hang=True)
    samples=[];stops=[];module=mod('remote_capacity');original=module.CapacityMonitor.measure
    def measure(monitor):
        start=time.monotonic();value=original(monitor)
        samples.append({'start_s':start,'end_s':time.monotonic(),'measured_bytes':value.installation_measured_bytes,
                        'promised_bytes':value.installation_promised_bytes})
        return value
    broker=mod('broker');stop=broker.Broker.stop
    async def stopping(instance,job_id,reason):
        start=time.monotonic();result=await stop(instance,job_id,reason)
        stops.append({'start_s':start,'finished_s':time.monotonic(),'reason':reason});return result
    try:
        with patch.object(module.CapacityMonitor,'measure',new=measure),patch.object(broker.Broker,'stop',new=stopping):
            await case.start();task=await case.task();job=case.ok(await case.call('delegate',case.args(task)))['job']
            result=await case.terminal(task['task_id'],job['job_id'])
            case.assertEqual(result['job']['state'],'cancelled');case.assertEqual(result['job']['reason'],'capacity_exhausted')
        return {'writer':'single synthetic8192-byte burst before fake native wait','file_limit_bytes':4096,
            'observed_file_overshoot_bytes':4096,'samples':samples,'stops':stops,
            'limitation':'Burst-overrun sample; no OS hard quota or arbitrary writer-rate containment guarantee.'}
    finally:
        await case.stack.aclose()
        while case._cleanups:
            fn,args,kwargs=case._cleanups.pop();value=fn(*args,**kwargs)
            if inspect.isawaitable(value):await value

async def main():
    try:
        for chunk in (4096,65536):
            for streams in (1,2):
                for size in (1048576,8*1048576,16*1048576):
                    report['cases'].append(await transfer_case(size,chunk,streams));OUT.write_text(json.dumps(report,indent=2)+'\n')
        report['monitor']=await monitor_case()
        report['correctness_pass']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)};raise
    finally:OUT.write_text(json.dumps(report,indent=2)+'\n')
asyncio.run(main())
