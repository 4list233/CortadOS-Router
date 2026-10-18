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
