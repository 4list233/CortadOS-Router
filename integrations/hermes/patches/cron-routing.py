#!/usr/bin/env python3
"""Install the generic per-fire cron model-routing hook after Hermes updates."""
from pathlib import Path
import argparse

OLD = "        jc = _load_cron_job_config(job, job_id, job_name)\n        _cfg = jc.cfg\n"
NEW = '''        jc = _load_cron_job_config(job, job_id, job_name)
        from cron.scheduler_model_routing import route_job
        try:
            job, jc = route_job(job, jc, prompt=prompt, session_id=_cron_session_id)
        except Exception as exc:
            return _blocked_config_result(job_id, job_name, f"Cron model routing unavailable: {exc}")
        _cfg = jc.cfg
'''
HOOK = '    "pre_gateway_dispatch",\n'
HOOK_NEW = '    "pre_gateway_dispatch", "route_cron_model",\n'
HEADER_IMPORT = '    """Header of the persisted run document (title, ids, schedule, prompt)."""\n'
HEADER_IMPORT_NEW = HEADER_IMPORT + '    from cron.scheduler_model_routing import routing_header\n'
HEADER = '        f"# Cron Job: {title}\\n\\n"\n        f"**Job ID:** {job_id}\\n"\n'
HEADER_NEW = HEADER + '        f"{routing_header(job)}"\n'
RUN_SCOPE = '    scope = _CronRunScope(job, job_id, execution_id)\n'
RUN_SCOPE_NEW = '    _unrouted_job = job  # keep retry signaling on the caller\'s occurrence\n' + RUN_SCOPE
RETRY_FLAG = '                job["_model_unreachable"] = True\n'
RETRY_FLAG_NEW = '                _unrouted_job["_model_unreachable"] = True\n'
ATTACH = '        _audit = _FireAudit(job, job_id, model)\n'
ATTACH_NEW = '''        agent._cortados_route_constraints = dict(job.get('_routing_constraints') or {})
''' + ATTACH


def apply(root, *, check=False):
    changes = []
    for rel, before, after in [("cron/scheduler.py", OLD, NEW),
                               ("cron/scheduler.py", RUN_SCOPE, RUN_SCOPE_NEW),
                               ("cron/scheduler.py", RETRY_FLAG, RETRY_FLAG_NEW),
                               ("cron/scheduler.py", ATTACH, ATTACH_NEW),
                               ("cron/scheduler.py", HEADER_IMPORT, HEADER_IMPORT_NEW),
                               ("cron/scheduler.py", HEADER, HEADER_NEW),
                               ("hermes_cli/plugins.py", HOOK, HOOK_NEW)]:
        target = root / rel
        source = next((text for path, text in reversed(changes) if path == target), target.read_text())
        if after in source or (rel == 'hermes_cli/plugins.py' and '"route_cron_model"' in source):
            continue
        if source.count(before) != 1:
            raise RuntimeError(f"Inspect changed Hermes call site before patching {rel}")
        changes.append((target, source.replace(before, after)))
    module = Path(__file__).with_name("cron-routing") / "scheduler_model_routing.py"
    helper = root / "cron/scheduler_model_routing.py"
    if not helper.exists() or helper.read_text() != module.read_text():
        changes.append((helper, module.read_text()))
    for target, text in changes:
        compile(text, str(target), 'exec')
    if not check:
        for target, text in changes:
            target.write_text(text)
    return bool(changes)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path, default=Path.home()/".cortados/hermes/hermes-agent")
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    changed = apply(args.hermes_root, check=args.check)
    print(('would patch' if args.check else 'patched') if changed else 'already patched')
