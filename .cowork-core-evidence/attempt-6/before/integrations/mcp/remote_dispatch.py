# PURPOSE: Compose owned remote operations with existing routing/job/quota owners.
# CONTRACT: RT-COWORK-MCP-008 v5 R014–027,036,041–042.
# DEPENDS: CoreRuntime, task_store, existing route_task and JobStore.
# CEILING: No alternative routing algorithm, paid retry, listener or caller-selected cwd.
import asyncio
import hashlib
import os

from . import remote_schemas as s
from . import task_store as store
from . import task_workspace as workspace
from . import transfer_tickets as tickets
from .schemas import WorkerError, canonical, validate


class CoreBroker:
    def __init__(self, runtime):
        self.runtime = runtime

    def task_result(self, task):
        try:
            return self._task_result(task)
        except Exception:
            self.runtime.fatal()
            raise

    def _task_result(self, task):
        artifacts = store.rows(self.runtime.broker.store.db, 'remote_artifacts', 'owner_id=? AND task_id=?', (task.owner_id, task.task_id))
        visible = []
        for row in artifacts:
            if row.origin == 'input':
                visible.append(row)
            else:
                try:
                    tickets.require_publication(self.runtime.broker.store.db, row)
                except s.RemoteError as exc:
                    if exc.code == 'publication_failed':
                        continue
                    raise
                visible.append(row)
        artifacts = visible
        return s.project_remote_result(dict(kind='task', task=task, artifacts=artifacts))

    def job_result(self, owner_id, task_id, job_id, *, include_result=False):
        runtime = self.runtime
        try:
            bindings = store.rows(runtime.broker.store.db, 'remote_job_bindings', 'owner_id=? AND task_id=? AND job_id=?', (owner_id, task_id, job_id))
        except Exception:
            runtime.fatal()
            raise
        if not bindings:
            raise s.RemoteError('job_not_found')
        try:
            return self._job_result(owner_id, task_id, job_id, bindings[0], include_result=include_result)
        except Exception:
            runtime.fatal()
            raise

    def _job_result(self, owner_id, task_id, job_id, binding, *, include_result):
        runtime = self.runtime
        record = runtime.broker.store.get(job_id)
        runtime.binding(record)
        if record.get('execution_kind') != 'cowork_remote_v1':
            raise s.RemoteError('policy_unavailable')
        publication = store.rows(runtime.broker.store.db, 'remote_publications', 'job_id=? AND owner_id=? AND task_id=?', (job_id, owner_id, task_id))[0]
        value = dict(kind='result' if include_result else 'job', job=record, binding=binding, publication=publication)
        if include_result:
            value['artifacts'] = []
            if publication.state == 'complete':
                for entry in publication.manifest:
                    row = tickets.artifact(runtime.broker.store.db, owner_id, task_id, entry.artifact.artifact_id)
                    tickets.require_publication(runtime.broker.store.db, row)
                    value['artifacts'].append(row)
        return s.project_remote_result(value)

    async def dispatch_remote(self, principal, tool, args):
        runtime = self.runtime
        try:
            if principal != runtime.config.principal or not runtime.enabled:
                raise s.RemoteError('permission_refused')
            runtime.authorize(principal, tool)
            args = s.validate_remote(tool, args, runtime.config)
            async with runtime.broker.admission_lock:
                lock = runtime.task_locks.setdefault(args.get('task_id', ''), asyncio.Lock())
                async with lock:
                    runtime.authorize(principal, tool)
                    return await self._dispatch(principal, tool, args)
        except WorkerError as exc:
            error = s.from_worker_error(exc)
        except s.RemoteError as exc:
            error = exc
        except Exception:
            runtime.fatal()
            error = s.RemoteError('storage_error')
        try:
            # Owners latch integrity loss; a public code alone cannot distinguish
            # a definitive acceptance rollback from an uncertain committed effect.
            if runtime.broker.store.admission_closed:
                runtime.admission_failed = True
                runtime.fatal()
                return s.RemoteError('storage_error').payload()
            with runtime.broker.store.transaction() as tx:
                store.event(tx, 'runtime', None, reason=error.code)
        except Exception:
            runtime.admission_failed = True
            error = s.RemoteError('storage_error')
            runtime.fatal()
        return error.payload()

    async def _dispatch(self, principal, tool, args):
        runtime = self.runtime
        broker = runtime.broker
        db = broker.store.db
        owner_id = s.owner(principal)
        argument_hash = hashlib.sha256(canonical(args)).hexdigest()
        if tool in ('task_create', 'task_delete', 'delegate', 'file_upload_ticket', 'file_download_ticket'):
            prior = store.operation(db, owner_id, tool, args['request_id'], argument_hash)
            if prior:
                ids = prior.object_ids
                if tool == 'delegate':
                    return self.job_result(owner_id, ids.task_id, ids.job_id)
                if tool.startswith('file_'):
                    ticket = store.rows(db, 'remote_tickets', 'ticket_id=? AND owner_id=?', (ids.ticket_id, owner_id))[0]
                    if ticket.state == 'revoked':
                        raise s.RemoteError('ticket_invalid')
                    return self.ticket_result(ticket)
                return self.task_result(runtime.task(ids.task_id))
        if tool == 'context_requirements':
            from cortados_router.task_evidence import context_requirements
            from .remote_native import build_handoff
            value = context_requirements(validate(tool, args, runtime.base), runtime.base)
            packet = build_handoff(args, (), 9007199254740991)
            value['context_requirements']['worker_bytes'] = packet.worker_bytes
            if packet.worker_bytes > runtime.base['limits']['max_handoff_bytes']:
                value['context_requirements']['status'] = 'over_budget'
            return value
        if tool == 'task_create':
            with broker.store.transaction() as tx:
                task = store.create_intent(tx, principal, args, runtime.snapshot)
            try:
                identity = workspace.create_task(task)
                with broker.store.transaction() as tx:
                    task = store.transition(tx, owner_id, task.task_id, task.version, 'ready')
                    task = store.save(tx, 'remote_tasks', store.changed(task, root_identity=s.plain(identity)))
            except (OSError, s.RemoteError):
                with broker.store.transaction() as tx:
                    task = store.transition(tx, owner_id, task.task_id, task.version, 'quarantined')
                    task = store.save(tx, 'remote_tasks', store.changed(task, cleanup_error=s.RemoteError('storage_error').payload()['error']))
            return self.task_result(task)
        if tool.startswith('job_'):
            # Unknown/foreign task and job pairs have the same public job refusal.
            self.job_result(owner_id, args['task_id'], args['job_id'])
            if tool == 'job_cancel':
                await broker.stop(args['job_id'], 'cancel_requested')
            if tool == 'job_feedback':
                return broker.store.feedback({key: value for key, value in args.items() if key != 'task_id'})
            return self.job_result(owner_id, args['task_id'], args['job_id'], include_result=tool == 'job_result')
        task = store.get_owned(db, owner_id, args['task_id'])
        if tool == 'task_get':
            return self.task_result(task)
        if task.state == 'quarantined':
            raise s.RemoteError('task_quarantined')
        if task.state == 'deleted':
            raise s.RemoteError('task_deleted')
        if tool == 'task_delete':
            if (task.active_job_id or task.state == 'busy'
                    or any(lease.task_id == task.task_id and lease.direction == 'put' for lease in runtime.leases.values())):
                raise s.RemoteError('workspace_busy')
            from . import file_transfer
            with broker.store.transaction() as tx:
                tickets.revoke(tx, owner_id, task.task_id)
            for lease in tuple(runtime.leases.values()):
                if lease.task_id == task.task_id:
                    file_transfer.close(lease, 'abort')
            with broker.store.transaction() as tx:
                task = store.transition(tx, owner_id, task.task_id, task.version, 'deleting')
                store.record_operation(tx, owner_id, tool, args['request_id'], argument_hash, task.task_id)
            progress = workspace.remove(task)
            with broker.store.transaction() as tx:
                if progress.state == 'deleted':
                    task = store.transition(tx, owner_id, task.task_id, task.version, 'deleted')
                task = store.save(tx, 'remote_tasks', store.changed(task, cleanup_error=s.plain(progress.cleanup_error)))
            return self.task_result(task)
        if tool.startswith('file_'):
            if tool == 'file_upload_ticket':
                runtime.capacity.check(owner_id, task.task_id, args['bytes'] if args['artifact_id'] is None else 0)
            with broker.store.transaction() as tx:
                ticket = tickets.issue(tx, principal, args, 'put' if tool == 'file_upload_ticket' else 'get')
            return self.ticket_result(ticket)
        if task.state != 'ready':
            raise s.RemoteError('workspace_busy')
        if store.rows(db, 'remote_artifacts', "owner_id=? AND task_id=? AND state IN ('reserved','uploading')", (owner_id, task.task_id)):
            raise s.RemoteError('workspace_busy')
        if args['task_evidence']['task_group_id'] != task.group_id:
            raise s.RemoteError('invalid_request')
        from cortados_router.task_evidence import missing_fields
        if missing_fields(args):
            raise s.RemoteError('needs_clarification')
        if any(lease.task_id == task.task_id for lease in runtime.leases.values()):
            raise s.RemoteError('workspace_busy')
        entries = []
        for artifact_id in args['input_artifact_ids']:
            row = tickets.artifact(db, owner_id, task.task_id, artifact_id)
            if row.state != 'sealed':
                raise s.RemoteError('upload_incomplete')
            entries.append(s.InputEntry(artifact=s.artifact_projection(row), blob=row.blob))
        from .remote_native import build_handoff, load_manifest
        from cortados_router.route_task import route_task
        load_manifest(runtime.manifest_raw, runtime.base, runtime.config.execution_policy)
        local = {key: value for key, value in args.items() if key not in ('task_id', 'input_artifact_ids')}
        packet = build_handoff(local, tuple(entries), runtime.base['limits']['max_handoff_bytes'])
        if tool == 'route':
            route = await asyncio.to_thread(route_task, local, runtime.base, broker.metadata, handoff_packet=packet.text)
            runtime.authorize(principal, tool)
            load_manifest(runtime.manifest_raw, runtime.base, runtime.config.execution_policy)
            return s.project_remote_result(dict(kind='route', route=route))
        with broker.store.transaction() as tx:
            intent = store.preparation_intent(tx, task, args['request_id'], argument_hash)
        try:
            identity = workspace.prepare_empty(task, intent)
            with broker.store.transaction() as tx:
                intent = store.preparation_transition(tx, intent.preparation_id, 'intent', 'prepared', workspace_identity=identity)
            local['cwd'] = os.path.join(task.policy.roots.tasks_dir, intent.relative_name)
            local = validate('delegate', local, runtime.base)
            route = await asyncio.to_thread(route_task, local, runtime.base, broker.metadata, handoff_packet=packet.text)
            runtime.capacity.check(owner_id, task.task_id, sum(entry.artifact.bytes for entry in entries))
            load_manifest(runtime.manifest_raw, runtime.base, runtime.config.execution_policy)
            seed = s.AcceptanceSeed(preparation_id=intent.preparation_id, owner_id=owner_id, task_id=task.task_id,
                task_group_id=task.group_id, request_id=args['request_id'], argument_hash=argument_hash,
                expected_task_version=task.version, input_manifest=entries, execution_policy=runtime.config.execution_policy,
                native_manifest=runtime.manifest, handoff=packet, policy_snapshot=runtime.snapshot)
            runtime.authorize(principal, tool)
            record, created = broker.store.accept(local, route, cli_version=broker.metadata.version, acceptance_seed=seed)
        except Exception:
            if broker.store.admission_closed:
                runtime.admission_failed = True
                raise
            bindings = store.rows(db, 'remote_job_bindings', 'owner_id=? AND task_id=?', (owner_id, task.task_id))
            if not any(binding.seed.preparation_id == intent.preparation_id for binding in bindings):
                try:
                    workspace.remove_preparation(task, intent)
                    phase = 'retired'
                except (OSError, s.RemoteError):
                    phase = 'quarantined'
                with broker.store.transaction() as tx:
                    store.preparation_transition(tx, intent.preparation_id, intent.phase, phase)
                    if phase == 'quarantined':
                        store.transition(tx, owner_id, task.task_id, task.version, 'quarantined')
            raise
        if created:
            worker = asyncio.create_task(broker.run_worker(record))
            broker.tasks.add(worker)
            worker.add_done_callback(broker.tasks.discard)
        return self.job_result(owner_id, task.task_id, record['job_id'])

    def ticket_result(self, ticket):
        row = tickets.artifact(self.runtime.broker.store.db, ticket.owner_id, ticket.task_id, ticket.artifact_id)
        if ticket.direction == 'get':
            tickets.require_publication(self.runtime.broker.store.db, row)
        return s.project_remote_result(dict(kind='ticket', ticket=ticket, authorization='Bearer ' + tickets.capability(ticket),
            transfer_origin=self.runtime.config.transfer_origin, media_type=row.media_type))
