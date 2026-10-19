# PURPOSE: Pin closed config/wire/private boundaries independently of implementation.
# CONTRACT: RT-COWORK-MCP-008 AC03,05,09. DEPENDS: frozen examples and actual owners.
# CEILING: Synthetic state only; no provider or native execution.
import copy
import json
import tempfile
import unittest
from unittest.mock import patch
import jsonschema
from .support import FIXTURE,Fixture,canon,expand,mod,plain,uid

class SchemaExam(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='cowork-schema-');self.addCleanup(self.temp.cleanup)
        self.f=Fixture(self.temp.name)
    def config(self):return mod('remote_config').load_core_config(canon(self.f.core),self.f.base)
    def test_every_config_key_required_and_unknown_refuses(self):
        for key in self.f.core:
            with self.subTest(key=key),self.assertRaises(mod('remote_schemas').RemoteError):
                value=copy.deepcopy(self.f.core);del value[key]
                mod('remote_config').load_core_config(canon(value),self.f.base)
        with self.assertRaises(mod('remote_schemas').RemoteError):
            mod('remote_config').load_core_config(canon(dict(self.f.core,unexpected=True)),self.f.base)
    def test_integer_limits_reject_boolean_zero_missing_and_unknown(self):
        for key in self.f.core['limits']:
            for bad in (True,0,-1,'1'):
                with self.subTest(key=key,bad=bad),self.assertRaises(mod('remote_schemas').RemoteError):
                    value=copy.deepcopy(self.f.core);value['limits'][key]=bad
                    mod('remote_config').load_core_config(canon(value),self.f.base)
    def test_cross_field_dimensions_and_retention_refuse(self):
        for changes in ({'file_bytes':self.f.core['limits']['task_bytes']+1},{'chunk_bytes':65537},{'control_bytes':511},
                        {'control_bytes':2**32},{'artifacts_per_task':101},{'uploads_per_task':5},{'ipc_idle_ms':20001}):
            with self.subTest(changes=changes),self.assertRaises(mod('remote_schemas').RemoteError):
                value=copy.deepcopy(self.f.core);value['limits'].update(changes)
                mod('remote_config').load_core_config(canon(value),self.f.base)
        value=copy.deepcopy(self.f.core);value['retention']['automatic_cleanup']=True
        with self.assertRaises(mod('remote_schemas').RemoteError):mod('remote_config').load_core_config(canon(value),self.f.base)
    def test_duplicate_nonfinite_surrogate_json_rejected(self):
        for raw in (b'{"schema":"x","schema":"y"}',b'{"x":NaN}',b'{"x":"\\ud800"}'):
            with self.subTest(raw=raw),self.assertRaises(mod('remote_schemas').RemoteError):mod('remote_config').load_core_config(raw,self.f.base)
    def test_principal_is_synthetic_and_installation_matches(self):
        for key,value in (('issuer','https://attacker.invalid'),('subject','real-user'),('installation_id',uid()),('client_id','')):
            data=copy.deepcopy(self.f.core);data['principal'][key]=value
            with self.subTest(key=key),self.assertRaises(mod('remote_schemas').RemoteError):mod('remote_config').load_core_config(canon(data),self.f.base)
    def test_root_geometry_and_base_state_identity_refuse(self):
        for roots in ({**self.f.core['roots'],'blobs_dir':self.f.core['roots']['tasks_dir']},
                      {**self.f.core['roots'],'state_dir':self.f.core['roots']['staging_dir']}):
            with self.subTest(roots=roots),self.assertRaises(mod('remote_schemas').RemoteError):
                mod('remote_config').load_core_config(canon(dict(self.f.core,roots=roots)),self.f.base)
    def test_remote_errors_are_closed_and_do_not_leak_worker_messages(self):
        from integrations.mcp.schemas import WorkerError
        api=mod('remote_schemas')
        error=api.from_worker_error(WorkerError('storage_error','SENTINEL/secret/path'))
        self.assertEqual(error.payload(),{'ok':False,'error':{'code':'storage_error','message':'Remote operation refused: storage_error'}})
        self.assertNotIsInstance(error,WorkerError)
        with self.assertRaises((ValueError,api.RemoteError)):api.RemoteError('not-a-code','SENTINEL')
    def test_wire_success_examples_and_errors_match_frozen_schema(self):
        # Independent oracle pins all output shapes; candidate projections are exercised separately.
        schema=FIXTURE['wire_schema']
        for name,value in FIXTURE['wire_examples'].items():
            with self.subTest(name=name):
                jsonschema.validate(value,{'$ref':'#/$defs/'+name,'$defs':schema['$defs']})
                bad=copy.deepcopy(value);bad['owner_id']='SENTINEL'
                with self.assertRaises(jsonschema.ValidationError):jsonschema.validate(bad,{'$ref':'#/$defs/'+name,'$defs':schema['$defs']})
    def test_private_record_constructors_freeze_and_reject_unknown_fields(self):
        api=mod('remote_schemas')
        for name,pair in FIXTURE['private_examples'].items():
            with self.subTest(name=name):
                value=copy.deepcopy(pair['valid'])
                if name=='CoreConfig':value['ticket_key']=bytes.fromhex(value['ticket_key'])
                if name=='ReadResult':value['body']=bytes.fromhex(value['body'])
                record=getattr(api,name)(**value)
                first=next(iter(value))
                with self.assertRaises((AttributeError,TypeError)):setattr(record,first,None)
                with self.assertRaises(api.RemoteError):getattr(api,name)(**dict(value,unexpected=True))
    def test_projected_variants_and_unknown_cross_fields(self):
        api=mod('remote_schemas')
        for name,pair in expand(FIXTURE['protocol_examples'])['ProjectionInput'].items():
            with self.subTest(name=name):
                result=plain(api.project_remote_result(pair['valid']))
                self.assertIs(type(result['ok']),bool)
                with self.assertRaises(api.RemoteError):api.project_remote_result(pair['invalid'])
    def test_request_unknown_identity_paths_and_filename_boundaries(self):
        api=mod('remote_schemas');config=self.config()
        request={'task_id':uid(),'request_id':uid(),'artifact_id':None,'filename':'résumé "Q1".bin','bytes':0,'sha256':'a'*64,'media_type':'application/octet-stream'}
        self.assertEqual(plain(api.validate_remote('file_upload_ticket',request,config))['filename'],request['filename'])
        for name in ('','.', '..','a/b','a\\b','a\x00b','\x85','é'*128):
            with self.subTest(name=name),self.assertRaises(api.RemoteError):api.validate_remote('file_upload_ticket',dict(request,filename=name),config)
        for key in ('owner_id','cwd','path','policy','execution_kind'):
            with self.subTest(key=key),self.assertRaises(api.RemoteError):api.validate_remote('file_upload_ticket',dict(request,**{key:'SENTINEL'}),config)

    def test_nested_inputs_are_copied_and_immutable_without_implementation_requirement(self):
        api=mod('remote_schemas');value=copy.deepcopy(FIXTURE['private_examples']['NativeManifest']['valid'])
        record=api.NativeManifest(**value)
        expected=tuple(value['disabled_features']);value['disabled_features'].append('injected')
        self.assertEqual(tuple(record.disabled_features),expected)
        with self.assertRaises((AttributeError,TypeError)):record.disabled_features[0]='injected'
        nested=api.NativeManifest(**{**value,'disabled_features':expected,'template':record.template})
        self.assertEqual(nested.template.id,record.template.id)

    def test_every_new_owner_module_has_a_real_importable_boundary(self):
        for name in ('remote_config','remote_schemas','remote_native','remote_runtime','remote_dispatch','remote_ipc',
                     'remote_http','task_store','task_workspace','artifact_store','transfer_tickets','file_transfer','remote_capacity'):
            with self.subTest(owner=name):self.assertIsNotNone(mod(name))


class BuildClosedShapeRegressionExam(unittest.TestCase):
    def test_native_private_record_array_constraints_match_frozen_schema(self):
        api=mod('remote_schemas')
        changes=[('NativeTemplate','mcp_servers',['injected']),
                 ('NativeManifest','captures',[{'name':str(i),'sha256':'a'*64,'kind':'synthetic'} for i in range(101)]),
                 ('NativeManifest','disabled_features',['x']*101),('NativeManifest','environment_names',['X']*101)]
        for name,field,value in changes:
            with self.subTest(name=name,field=field):
                fields=copy.deepcopy(FIXTURE['private_examples'][name]['valid']);fields[field]=value
                with self.assertRaises(api.RemoteError):getattr(api,name)(**fields)

from .support import CoreCase
class Revision2IpcDeadlineExam(CoreCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fixture.core['limits'].update(ipc_idle_ms=100,transfer_idle_ms=100,transfer_total_ms=500)
        await self.start()

    async def endpoint(self):
        import socket
        from .support import raw_send,raw_read
        left,right=socket.socketpair();left.setblocking(False);right.setblocking(False)
        left.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,4096)
        self.addCleanup(left.close);self.addCleanup(right.close)
        session=await self.runtime.attach(left,self.runtime.config.principal)
        await raw_send(right,dict(v=1,seq=0,op='bind',body_bytes=0,args={'principal':self.fixture.principal,'config_digest':self.fixture.digest()}))
        self.assertTrue((await raw_read(right))[0]['result']['ok'])
        return left,right,session

    async def test_actual_socket_primary_and_fallback_backpressure(self):
        import asyncio,struct
        for oversized in (False,True):
            with self.subTest(oversized=oversized):
                left,right,session=await self.endpoint();calls=0
                async def result(*args):
                    nonlocal calls
                    calls+=1;return {'ok':True,'padding':'x'*(70000 if oversized else 512)}
                frames=[]
                for seq in range(1,101):
                    b=canon(dict(v=1,seq=seq,op='tool',body_bytes=0,args={'tool':'task_get','arguments':{}}))
                    frames.append(struct.pack('!I',len(b))+b)
                with patch.object(self.runtime.core,'dispatch_remote',result):
                    try:
                        try:await asyncio.wait_for(asyncio.get_running_loop().sock_sendall(right,b''.join(frames)),1)
                        except (BrokenPipeError,ConnectionResetError):pass
                        await asyncio.wait({session.task},timeout=.35)
                        self.assertGreater(calls,1);self.assertTrue(session.task.done(),'Fallback socket write exceeded100ms original IPC budget')
                        self.assertEqual(left.fileno(),-1)
                    finally:
                        session.task.cancel();await asyncio.gather(session.task,return_exceptions=True)

    async def test_encoding_and_fallback_consume_original_deadline(self):
        import asyncio,time
        from .support import raw_send,raw_read
        for oversized in (False,True):
            with self.subTest(oversized=oversized):
                left,right,session=await self.endpoint();api=mod('remote_schemas');original=api.json_bytes
                async def result(*args):return {'ok':True,'padding':'x'*(70000 if oversized else 1)}
                def slow_encode(value):
                    data=original(value)
                    if isinstance(value,dict) and 'padding' in value.get('result',{}):time.sleep(.15)
                    return data
                with patch.object(self.runtime.core,'dispatch_remote',result),patch.object(api,'json_bytes',slow_encode):
                    await raw_send(right,dict(v=1,seq=1,op='tool',body_bytes=0,args={'tool':'task_get','arguments':{}}))
                    with self.assertRaises(EOFError):
                        async with asyncio.timeout(.5):await raw_read(right)
                self.assertTrue(session.task.done());self.assertEqual(left.fileno(),-1)

    async def test_partial_ipc_frame_cannot_append_fallback(self):
        import asyncio
        from .support import raw_send
        left,right,session=await self.endpoint();loop=asyncio.get_running_loop();original=loop.sock_sendall;writes=[]
        async def result(*args):return {'ok':True}
        async def partial(sock,data):
            if sock is left:
                writes.append(bytes(data))
                if len(writes)==1:
                    await original(sock,data[:8]);raise mod('remote_schemas').RemoteError('output_limit')
            return await original(sock,data)
        with patch.object(self.runtime.core,'dispatch_remote',result),patch.object(loop,'sock_sendall',partial):
            await raw_send(right,dict(v=1,seq=1,op='tool',body_bytes=0,args={'tool':'task_get','arguments':{}}))
            await asyncio.wait({session.task},timeout=.3)
            self.assertEqual(len(writes),1,'Indeterminate partial frame cannot be followed by another frame')
            self.assertTrue(session.task.done());self.assertEqual(left.fileno(),-1)

    async def test_applicable_lease_deadline_bounds_response_fallback(self):
        import asyncio,time
        from .support import raw_send,raw_read
        # Lease100ms is deliberately shorter than IPC500ms;150ms encoding is
        # beyond the lease but inside IPC. A timely normal open is the control.
        await self.stack.aclose()
        self.fixture.core['limits']['ipc_idle_ms']=500
        await self.start()
        control_task=await self.task();control_ticket=await self.ticket(control_task['task_id'],b'x')
        control=self.ok((await self.stream('open_put',{'ticket_id':control_ticket['ticket_id'],
            'capability':control_ticket['authorization'][7:],'content_length':1}))['result'])
        self.ok((await self.stream('close',{'lease_id':control['lease_id'],'reason':'abort'}))['result'])
        task=await self.task();ticket=await self.ticket(task['task_id'],b'x')
        left,right,session=await self.endpoint();ipc=mod('remote_ipc');api=mod('remote_schemas');original=ipc._stream;encode=api.json_bytes
        async def oversized(*args):
            result,body=await original(*args)
            if args[2]=='open_put':return dict(result,padding='x'*70000),body
            return result,body
        def late(value):
            data=encode(value)
            if isinstance(value,dict) and 'padding' in value.get('result',{}):time.sleep(.15)
            return data
        with patch.object(ipc,'_stream',oversized),patch.object(api,'json_bytes',late):
            await raw_send(right,dict(v=1,seq=1,op='open_put',body_bytes=0,args={'ticket_id':ticket['ticket_id'],
                'capability':ticket['authorization'][7:],'content_length':1}))
            with self.assertRaises(EOFError):
                async with asyncio.timeout(.5):await raw_read(right)
        self.assertFalse(self.runtime.leases);self.assertFalse(self.runtime.descriptors)


from .support import CoreCase, FatalCapture, monitor_fixture


class CanonicalReadRevisionExam(CoreCase):
    """Real SQLite reader, independent frozen shapes, deliberate private corruption."""
    records = (
        ('remote_tasks', 'TaskRow'), ('remote_operations', 'OperationRow'),
        ('remote_preparations', 'PreparationRow'), ('remote_job_bindings', 'RemoteBinding'),
        ('remote_artifacts', 'ArtifactRow'), ('remote_tickets', 'TicketRow'),
        ('remote_publications', 'PublicationRow'), ('remote_reservations', 'ReservationRow'))

    async def read_case(self, table, name, field=None, corrupt=None, semantic=False):
        await monitor_fixture(self)
        db = self.runtime.broker.store.db
        value = copy.deepcopy(FIXTURE['private_examples'][name]['valid'])
        jsonschema.validate(value, {'$ref': '#/$defs/' + name,
                                   '$defs': FIXTURE['private_schema']['$defs']})
        columns = [row[1] for row in db.execute('PRAGMA table_info(' + table + ')')]
        encoded = {}
        for column in columns:
            key = column[:-5] if column.endswith('_json') else column
            if key in value:
                encoded[column] = json.dumps(value[key]) if column.endswith('_json') else value[key]
        if table == 'remote_job_bindings':
            encoded.update(owner_id=value['seed']['owner_id'], task_id=value['seed']['task_id'], active=1)
        if table == 'remote_operations':
            encoded['task_id'] = value['object_ids']['task_id']
        if field is not None:
            encoded[field] = corrupt
        # Only the corruption fixture bypasses SQL constraints; the real canonical
        # reader must still validate data loaded from damaged/foreign storage.
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('PRAGMA ignore_check_constraints=ON')
        keys = tuple(encoded)
        db.execute('INSERT INTO ' + table + '(' + ','.join(keys) + ') VALUES(' +
                   ','.join('?' for _ in keys) + ')', tuple(encoded.values()))
        signal = FatalCapture(self)
        try:
            if field is None:
                result = mod('task_store').rows(db, table)
                self.assertEqual(len(result), 1)
                self.assertFalse(self.runtime.fatal_failed)
            else:
                with self.assertRaises(mod('remote_schemas').RemoteError) as raised:
                    mod('task_store').rows(db, table)
                self.assertEqual(raised.exception.code, 'policy_unavailable' if semantic else 'storage_error')
                self.assertTrue(self.runtime.fatal_failed)
                self.assertTrue(self.runtime.broker.store.admission_closed)
                signal.assert_one(self)
        finally:
            db.execute('DELETE FROM ' + table)
            db.execute('PRAGMA ignore_check_constraints=OFF')
            db.execute('PRAGMA foreign_keys=ON')
            await self.stack.aclose()

    async def test_every_canonical_row_valid_control(self):
        for table, name in self.records:
            with self.subTest(record=name):
                await self.read_case(table, name)

    async def test_missing_policy_member_before_owner_access(self):
        await self.read_case('remote_tasks', 'TaskRow', 'policy_json', '{}')

    async def test_missing_seed_member_before_redundant_owner_access(self):
        await self.read_case('remote_job_bindings', 'RemoteBinding', 'seed_json', '{}')

    async def test_missing_operation_ids_before_redundant_task_access(self):
        await self.read_case('remote_operations', 'OperationRow', 'object_ids_json', '{}')

    async def test_missing_workspace_identity_before_relative_access(self):
        await self.read_case('remote_preparations', 'PreparationRow', 'workspace_identity_json', '{}')

    async def test_nested_array_and_object_shapes_are_closed(self):
        for table, name, column, corrupt in (
                ('remote_artifacts', 'ArtifactRow', 'blob_json', '[]'),
                ('remote_publications', 'PublicationRow', 'manifest_json', '{}'),
                ('remote_tasks', 'TaskRow', 'policy_json', '{"unknown":true}')):
            with self.subTest(record=name):
                await self.read_case(table, name, column, corrupt)

    async def test_scalar_ticket_and_reservation_fields_validate(self):
        for table, name, column in (
                ('remote_tickets', 'TicketRow', 'direction'),
                ('remote_reservations', 'ReservationRow', 'state')):
            with self.subTest(record=name):
                await self.read_case(table, name, column, 'SYNTHETIC-invalid')

    async def test_nested_duplicate_and_nonfinite_json_refuse(self):
        for raw in ('{"device":1,"device":2}', '{"device":NaN}'):
            with self.subTest(raw=raw):
                await self.read_case('remote_artifacts', 'ArtifactRow', 'blob_json', raw)

    async def test_valid_shape_redundant_identity_mismatch_keeps_policy_unavailable(self):
        await self.read_case('remote_operations', 'OperationRow', 'task_id', uid(), semantic=True)

    async def test_sql_boolean_domain_is_validated_before_conversion(self):
        for table, name, column in (
                ('remote_artifacts', 'ArtifactRow', 'partial'),
                ('remote_job_bindings', 'RemoteBinding', 'active')):
            with self.subTest(record=name, column=column):
                await self.read_case(table, name, column, 2)
