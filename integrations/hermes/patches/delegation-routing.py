#!/usr/bin/env python3
"""Install native delegation preconstruction routing into an explicit Hermes checkout.

Use --check to validate anchors and Python syntax without writing. No restart or
configuration changes are performed. Reapply after updating Hermes.
"""
import argparse
import ast
from pathlib import Path


EARLY = '''    try:
        creds = _resolve_delegation_credentials(routing_cfg, parent_agent)
    except ValueError as exc:
        # Explicit-pin preflight failures (e.g. pinned delegation.command missing from PATH) refuse the
        # spawn loudly (#80450).
        return tool_error(str(exc))
'''
PREFLIGHT = '''    from tools.delegate_model_routing import prepare_children
    try:
        creds, child_routes = prepare_children(
            task_list, cfg=cfg, credentials_cfg=credentials_cfg, parent_agent=parent_agent,
            constraints=routing_constraints, resolve_credentials=_resolve_delegation_credentials,
        )
    except Exception as exc:
        return tool_error(f"Delegation model routing unavailable: {exc}")

'''
OVERRIDES = '''    overrides = {
        "override_provider": creds["provider"], "override_base_url": creds["base_url"],
        "override_api_key": creds["api_key"], "override_api_mode": creds["api_mode"],
        "override_request_overrides": creds.get("request_overrides"),
        "override_acp_command": creds.get("command"),
        "override_acp_args": creds.get("args"),
        "routing_cfg": routing_cfg,
    }
'''
PER_CHILD = '''        plan = child_routes[i] if child_routes is not None else None
        selected_creds = plan["creds"] if plan is not None else creds
        selected_cfg = plan["routing_cfg"] if plan is not None else routing_cfg
''' + "".join("    " + line for line in OVERRIDES.replace('creds[', 'selected_creds[').replace('creds.get(', 'selected_creds.get(').replace('"routing_cfg": routing_cfg', '"routing_cfg": selected_cfg').splitlines(True))


def substitute(source, before, after, label):
    if after and after in source:
        return source
    if source.count(before) != 1:
        raise RuntimeError(f"Inspect changed Hermes call site before patching {label}")
    return source.replace(before, after, 1)


def transform_delegate(source):
    # One versioned marker covers the deletion anchor on subsequent installs.
    marker = "    # cortados-native-delegation-routing-v1\n"
    edits = [
        (EARLY, ""),
        ("    credentials_cfg: Optional[Dict[str, Any]] = None,\n",
         "    credentials_cfg: Optional[Dict[str, Any]] = None,\n    routing_constraints: Optional[Dict[str, Any]] = None,\n"),
        ("    overall_start = time.monotonic()\n", marker + PREFLIGHT + "    overall_start = time.monotonic()\n"),
        ("    live_deleg_id: Optional[str], live_writers: list, task_images: Optional[List[Optional[List[str]]]] = None,\n",
         "    live_deleg_id: Optional[str], live_writers: list, task_images: Optional[List[Optional[List[str]]]] = None,\n    child_routes=None,\n"),
        (OVERRIDES, "    from tools.delegate_model_routing import attach_route\n"),
        ("    for i, t in enumerate(task_list):\n", "    for i, t in enumerate(task_list):\n" + PER_CHILD),
        ('                model=creds["model"], max_iterations=max_iterations, task_count=len(task_list),\n',
         '                model=selected_creds["model"], max_iterations=max_iterations, task_count=len(task_list),\n'),
        ('        _writer = live_writers[i] if i < len(live_writers) else None\n',
         '        _writer = live_writers[i] if i < len(live_writers) else None\n        attach_route(child, plan, _writer)\n'),
        ('        routing_cfg=routing_cfg, live_deleg_id=live_deleg_id, live_writers=live_writers, task_images=task_images,\n',
         '        routing_cfg=routing_cfg, live_deleg_id=live_deleg_id, live_writers=live_writers, task_images=task_images,\n        child_routes=child_routes,\n'),
    ]
    if marker in source:
        if EARLY in source or any(after and after not in source for _, after in edits):
            raise RuntimeError("Inspect changed patched Hermes tools/delegate_tool.py")
        return source
    for before, after in edits:
        source = substitute(source, before, after, "tools/delegate_tool.py")
    return source


def transform_hooks(source):
    # Cooperate with cron and generic-request installers, in either order.
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.AnnAssign)
                and isinstance(n.target, ast.Name) and n.target.id == "VALID_HOOKS")
    if not isinstance(node.value, ast.Set):
        raise RuntimeError("Inspect changed Hermes VALID_HOOKS before patching")
    if "route_agent_model" in ast.literal_eval(node.value):
        return source
    lines = source.splitlines(True)
    lines.insert(node.value.lineno, '    "route_agent_model",  # native delegation and generic request routing\n')
    return "".join(lines)


def transform_review(source):
    before = "    raw = delegate_task(goal=goal, context=context, background=True, parent_agent=parent_agent, credentials_cfg=credentials_cfg)\n"
    after = '''    # Trusted review ownership is explicit and never inferred from task text.
    routing_constraints = {"kind": "review", "owner": "auxiliary.review",
                           "provider": (credentials_cfg or {}).get("provider") or "",
                           "model": (credentials_cfg or {}).get("model") or ""}
    raw = delegate_task(goal=goal, context=context, background=True, parent_agent=parent_agent,
                        credentials_cfg=credentials_cfg, routing_constraints=routing_constraints)
'''
    return substitute(source, before, after, "agent/review_engine.py")


def apply(root, *, check=False):
    root = Path(root).resolve()
    changes = {}
    for rel, transform in [("tools/delegate_tool.py", transform_delegate),
                           ("hermes_cli/plugins.py", transform_hooks),
                           ("agent/review_engine.py", transform_review)]:
        target = root / rel
        before = target.read_text()
        after = transform(before)
        if after != before:
            changes[target] = after
    target = root / "tools/delegate_model_routing.py"
    module = Path(__file__).with_name("delegation-routing") / target.name
    after = module.read_text()
    if not target.exists() or target.read_text() != after:
        changes[target] = after
    # Validate every target before the first write: a changed host aborts cleanly.
    for target, source in changes.items():
        compile(source, str(target), "exec")
    if not check:
        for target, source in changes.items():
            target.write_text(source)
    return [str(path.relative_to(root)) for path in changes]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changed = apply(args.hermes_root, check=args.check)
    print(("Would update: " if args.check else "Updated: ") + ", ".join(changed) if changed else "Already patched")
