#!/usr/bin/env python3
"""Install required auxiliary routing into an explicit Hermes checkout (no restart)."""
import argparse
import ast
from pathlib import Path
import re

IMPORT = '''from agent.auxiliary_model_routing import (
    auxiliary_scope, prepare_required_request, required_send, required_asend,
)
'''
PREPARE = '''    required = prepare_required_request(
        globals(), task, provider=provider, model=model, main_runtime=main_runtime,
        messages=messages, temperature=temperature, max_tokens=max_tokens, tools=tools,
        timeout=timeout, extra_body=extra_body, reasoning_config=reasoning_config,
        extra_headers=extra_headers, route_info=route_info, async_mode=async_mode)
    if required is not None:
        return required
'''


def replace_once(source, old, new):
    if new in source:
        return source
    if source.count(old) != 1:
        raise RuntimeError("Inspect changed Hermes auxiliary call site before patching")
    return source.replace(old, new, 1)


def transform_host(source):
    import_anchor = "from types import SimpleNamespace\n"
    source = replace_once(source, import_anchor, import_anchor + IMPORT)
    source = replace_once(source,
        '    ("effective_extra_body", Dict[str, Any]), ("base_info", str)])\n',
        '    ("effective_extra_body", Dict[str, Any]), ("base_info", str), ("required_route", Optional[dict])])\n'
        '_PreparedAuxRequest.__new__.__defaults__ = (None,)\n')
    source = replace_once(source, '@_relay_auxiliary_call\ndef call_llm(', '@auxiliary_scope\n@_relay_auxiliary_call\ndef call_llm(')
    source = replace_once(source, '@_relay_auxiliary_call_async\nasync def async_call_llm(',
                          '@auxiliary_scope\n@_relay_auxiliary_call_async\nasync def async_call_llm(')
    source = replace_once(source,
        '    """Build the recovery-ladder generator for a failed primary request."""\n',
        '    """Build the recovery-ladder generator for a failed primary request."""\n'
        '    if req.required_route is not None:\n        raise first_err\n')
    for tag in ('""', '" (async)"'):
        old = f'            if not _should_retry_same_provider(task, transient_err, {tag}):\n'
        new = f'            if req.required_route is not None or not _should_retry_same_provider(task, transient_err, {tag}):\n'
        source = replace_once(source, old, new)
    # Limit physical-send edits to audited helper functions. Native SDK adapters
    # keep their own wire conversion; model catalog and availability probes stay
    # outside this logical auxiliary request boundary.
    sends = {"_relay_sync_stream": 2, "_create_with_progress_once": 3,
             "_acreate_with_stream": 1, "_acreate_with_progress": 3, "_call_llm_impl": 1}
    lines = source.splitlines(True)
    functions = [n for n in ast.parse(source).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for node in reversed(functions):
        chunk = "".join(lines[node.lineno-1:node.end_lineno])
        if node.name == "_prepare_aux_request":
            anchor = '    resolved_provider, resolved_model, resolved_base_url, resolved_api_key, resolved_api_mode = _resolve_task_provider_model(\n'
            chunk = replace_once(chunk, anchor, PREPARE + anchor)
        elif node.name in sends:
            method = "required_asend" if isinstance(node, ast.AsyncFunctionDef) else "required_send"
            count = len(re.findall(r"client\.chat\.completions\.create\(\*\*(\w+)\)", chunk))
            if count == 0 and chunk.count(method + "(client,") == sends[node.name]:
                continue
            if count != sends[node.name]:
                raise RuntimeError(f"Inspect changed Hermes auxiliary send site: {node.name}")
            chunk = re.sub(r"client\.chat\.completions\.create\(\*\*(\w+)\)", method + r"(client, \1)", chunk)
        else:
            continue
        lines[node.lineno-1:node.end_lineno] = [chunk]
    if not (set(sends) | {"_prepare_aux_request"}).issubset({n.name for n in functions}):
        raise RuntimeError("Missing required Hermes auxiliary send function")
    return "".join(lines)


def transform_hooks(source):
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.AnnAssign)
                and isinstance(n.target, ast.Name) and n.target.id == "VALID_HOOKS")
    if not isinstance(node.value, ast.Set):
        raise RuntimeError("Inspect changed Hermes hook registry")
    if "route_agent_model" in ast.literal_eval(node.value):
        return source
    lines = source.splitlines(True)
    lines.insert(node.value.lineno, '    "route_agent_model",  # required model selection\n')
    return "".join(lines)


def apply(root, *, check=False):
    root = Path(root).resolve()
    changes = {}
    for rel, transform in [("agent/auxiliary_client.py", transform_host), ("hermes_cli/plugins.py", transform_hooks)]:
        path = root / rel
        original = path.read_text()
        changed = transform(original)
        if changed != original:
            changes[path] = changed
    path = root / "agent/auxiliary_model_routing.py"
    module = Path(__file__).with_name("auxiliary-routing") / path.name
    text = module.read_text()
    if not path.exists() or path.read_text() != text:
        changes[path] = text
    for path, text in changes.items():
        compile(text, str(path), "exec")
    if not check:
        for path, text in changes.items():
            path.write_text(text)
    return [str(path.relative_to(root)) for path in changes]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changes = apply(args.hermes_root, check=args.check)
    print(("Would update: " if args.check else "Updated: ") + ", ".join(changes) if changes else "Already patched")
