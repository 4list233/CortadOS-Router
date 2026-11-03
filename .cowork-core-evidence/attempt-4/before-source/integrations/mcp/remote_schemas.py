# PURPOSE: Own closed remote records, validation, projection and safe audit.
# CONTRACT: RT-COWORK-MCP-008 v4 sections 1–4.
# DEPENDS: Existing JSON/schema and task-evidence owners.
# CEILING: No inference, policy discovery, filesystem or network authority.
# Static schema declarations; runtime never reads a contract or exam fixture.
import json
import re
import sys
from collections.abc import Mapping
from types import MappingProxyType
from urllib.parse import urlsplit
from jsonschema import Draft202012Validator
from cortados_router.task_evidence import canonical

def _shape(specification):
    """Construct a closed schema from owned field declarations, never documents."""
    properties = {}
    required = []
    for declaration in specification.split(';'):
        name, encoded = declaration.split('=', 1)
        optional = name.endswith('?')
        name = name.rstrip('?')
        nullable = encoded.endswith('?')
        encoded = encoded.rstrip('?')
        field = (json.loads(encoded) if encoded.startswith(('{', '['))
                 else {'$ref': '#/$defs/' + encoded})
        properties[name] = {'anyOf': [field, {'type': 'null'}]} if nullable else field
        if not optional:
            required.append(name)
    return {'type': 'object', 'properties': properties,
            'required': required, 'additionalProperties': False}


WIRE = {
'ID': {'type': 'string', 'pattern': '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'},
'Hash': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
'Name': {'type': 'string', 'minLength': 1, 'maxLength': 255},
'Media': {'type': 'string', 'maxLength': 127, 'pattern': '^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$'},
'Selection': {'oneOf': [{'type': 'object', 'properties': {'mode': {'const': 'explicit'}, 'model': {'type': 'string', 'minLength': 1}}, 'required': ['mode', 'model'], 'additionalProperties': False}, {'type': 'object', 'properties': {'mode': {'const': 's1'}, 'routing_brief': {'type': 'string'}}, 'required': ['mode'], 'additionalProperties': False}]},
'Constraints': _shape('allowed_models?={"type":"array","items":{"type":"string","minLength":1},"maxItems":100,"uniqueItems":true};min_band?={"enum":["R0","R1","R2","R3","R4"]}'),
'Permissions': _shape('network?={"const":false};mcp_servers?={"type":"array","maxItems":0}'),
'Reference': _shape('source={"type":"string","minLength":1};text={"type":"string","minLength":1}'),
'Evidence': _shape('schema={"const":"cortados.mcp-task-evidence.v1"};task_group_id=ID;components?={"type":"array","items":{"type":"string","minLength":1},"maxItems":100,"uniqueItems":true};dependencies?={"type":"array","items":{"type":"string","minLength":1},"maxItems":100,"uniqueItems":true};constraints?={"type":"array","items":{"type":"string","minLength":1},"maxItems":100,"uniqueItems":true};uncertainties?={"type":"array","items":{"type":"string","minLength":1},"maxItems":100,"uniqueItems":true};verification?={"type":"array","items":{"type":"string","minLength":1},"maxItems":100,"uniqueItems":true};references?={"type":"array","items":{"$ref":"#/$defs/Reference"},"maxItems":100}'),
'Create': _shape('request_id=ID;task_group_id=ID;title?={"type":"string","maxLength":200}'),
'TaskKey': _shape('task_id=ID'),
'Delete': _shape('task_id=ID;request_id=ID'),
'Context': _shape('task={"type":"string","minLength":1};context?={"type":"string"};selection=Selection;task_evidence?=Evidence'),
'Route': _shape('task={"type":"string","minLength":1};context?={"type":"string"};selection=Selection;task_evidence=Evidence;task_id=ID;constraints?=Constraints;permissions?=Permissions;account_ref?={"const":"native-default"};input_artifact_ids={"type":"array","items":{"$ref":"#/$defs/ID"},"maxItems":100,"uniqueItems":true}'),
'Delegate': _shape('task={"type":"string","minLength":1};context?={"type":"string"};selection=Selection;task_evidence=Evidence;task_id=ID;constraints?=Constraints;permissions?=Permissions;account_ref?={"const":"native-default"};request_id=ID;timeout_s={"type":"integer","minimum":1};input_artifact_ids={"type":"array","items":{"$ref":"#/$defs/ID"},"maxItems":100,"uniqueItems":true}'),
'JobKey': _shape('task_id=ID;job_id=ID'),
'Check': _shape('kind={"enum":["command","artifact","review"]};reference={"type":"string","minLength":1};claim={"enum":["passed","failed","unknown"]}'),
'Feedback': _shape('task_id=ID;job_id=ID;feedback_id=ID;verdict={"enum":["accepted","rejected","inconclusive"]};summary={"type":"string","minLength":1};checks={"type":"array","items":{"$ref":"#/$defs/Check"},"maxItems":100}'),
'Upload': _shape('task_id=ID;request_id=ID;artifact_id=ID?;filename=Name;bytes={"type":"integer","minimum":0};sha256=Hash;media_type=Media'),
'Download': _shape('task_id=ID;request_id=ID;artifact_id=ID'),
'Error': _shape('code={"enum":["invalid_request","idempotency_conflict","model_unavailable","account_unavailable","subscription_required","quota_unknown","quota_spent","scorer_unavailable","scoring_context_too_large","worker_context_too_large","needs_clarification","permission_refused","connector_unavailable","workspace_busy","capacity_exhausted","job_not_found","storage_error","native_version_mismatch","native_protocol_error","native_failed","output_limit","timeout","cancel_requested","broker_interrupted","containment_failed","job_not_terminal","task_not_found","artifact_not_found","task_deleted","task_quarantined","upload_incomplete","transfer_busy","ticket_invalid","ticket_expired","ticket_consumed","length_mismatch","hash_mismatch","artifact_corrupt","policy_unavailable","publication_failed"]};message={"type":"string","minLength":1}'),
'Failure': _shape('ok={"const":false};error=Error'),
'Limits': _shape('file_bytes={"type":"integer","minimum":1};task_bytes={"type":"integer","minimum":1};installation_bytes={"type":"integer","minimum":1};artifacts_per_task={"type":"integer","minimum":1,"maximum":100};ticket_ttl_s={"type":"integer","minimum":1};control_bytes={"type":"integer","minimum":1};retention={"const":"explicit_delete"}'),
'Artifact': _shape('artifact_id=ID;filename=Name;media_type=Media;bytes={"type":"integer","minimum":0};sha256=Hash;origin={"enum":["input","output"]};job_id=ID?;state={"enum":["reserved","uploading","sealed","quarantined"]};partial={"type":"boolean"};created_at_ms={"type":"integer","minimum":0}'),
'Task': _shape('task_id=ID;task_group_id=ID;title={"type":"string"};state={"enum":["creating","ready","busy","quarantined","deleting","deleted"]};created_at_ms={"type":"integer","minimum":0};expires_at_ms={"type":"null"};active_job_id=ID?;artifacts={"type":"array","items":{"$ref":"#/$defs/Artifact"},"maxItems":100};limits=Limits;cleanup_error=Error?'),
'TaskSuccess': _shape('ok={"const":true};task=Task'),
'Policy': _shape('id={"const":"cowork-task-v1"};sha256=Hash;native_version={"const":"0.163.0"}'),
'Job': _shape('task_id=ID;job_id=ID;route_id=ID;state={"enum":["queued","running","cancelling","succeeded","failed","cancelled","timed_out","interrupted"]};created_at_ms={"type":"integer","minimum":0};started_at_ms={"type":"integer","minimum":0}?;finished_at_ms={"type":"integer","minimum":0}?;elapsed_ms={"type":"integer","minimum":0};progress={"type":"string"};reason={"enum":["invalid_request","idempotency_conflict","model_unavailable","account_unavailable","subscription_required","quota_unknown","quota_spent","scorer_unavailable","scoring_context_too_large","worker_context_too_large","needs_clarification","permission_refused","connector_unavailable","workspace_busy","capacity_exhausted","job_not_found","storage_error","native_version_mismatch","native_protocol_error","native_failed","output_limit","timeout","cancel_requested","broker_interrupted","containment_failed","job_not_terminal","task_not_found","artifact_not_found","task_deleted","task_quarantined","upload_incomplete","transfer_busy","ticket_invalid","ticket_expired","ticket_consumed","length_mismatch","hash_mismatch","artifact_corrupt","policy_unavailable","publication_failed"]}?;execution_policy=Policy;publication={"enum":["pending","complete","failed","blocked"]};publication_error=Error?'),
'JobSuccess': _shape('ok={"const":true};job=Job'),
'Worker': _shape('provider={"const":"codex-cli"};requested_model={"type":"string","minLength":1};reported_model={"type":"string"}?;cli_version={"type":"string","minLength":1};account_ref={"const":"native-default"}'),
'Usage': _shape('input_tokens={"type":"integer","minimum":0}?;cached_input_tokens={"type":"integer","minimum":0}?;cache_write_input_tokens={"type":"integer","minimum":0}?;output_tokens={"type":"integer","minimum":0}?;reasoning_output_tokens={"type":"integer","minimum":0}?'),
'Warning': _shape('code={"type":"string","minLength":1};message={"type":"string"}'),
'Result': _shape('summary={"type":"string"}?;worker=Worker;usage=Usage;artifacts={"type":"array","items":{"$ref":"#/$defs/Artifact"},"maxItems":100};changed_paths={"const":null};verification={"const":"planner_review_required"};warnings={"type":"array","items":{"$ref":"#/$defs/Warning"},"maxItems":100};error=Error?'),
'ResultSuccess': _shape('ok={"const":true};job=Job;result=Result?'),
'FeedbackSuccess': _shape('ok={"const":true};feedback={"type":"object","properties":{"job_id":{"$ref":"#/$defs/ID"},"feedback_id":{"$ref":"#/$defs/ID"},"seq":{"type":"integer","minimum":1},"created_at_ms":{"type":"integer","minimum":0},"verdict":{"enum":["accepted","rejected","inconclusive"]},"verification_status":{"const":"planner_asserted"}},"required":["job_id","feedback_id","seq","created_at_ms","verdict","verification_status"],"additionalProperties":false}'),
'ContextSuccess': _shape('ok={"const":true};context_requirements={"type":"object","properties":{"schema":{"const":"cortados.mcp-context-requirements.v1"},"status":{"enum":["over_budget","needs_context","ready"]},"missing_fields":{"type":"array","items":{"type":"string","minLength":1},"maxItems":100},"questions":{"type":"array","items":{"type":"object","properties":{"field":{"type":"string","minLength":1},"prompt":{"type":"string","minLength":1}},"required":["field","prompt"],"additionalProperties":false},"maxItems":100},"scorer_chars":{"type":"integer","minimum":0},"worker_bytes":{"type":"integer","minimum":0},"scorer_limit_chars":{"type":"integer","minimum":0},"worker_limit_bytes":{"type":"integer","minimum":0},"scorer_digest":{"anyOf":[{"$ref":"#/$defs/Hash"},{"type":"null"}]}},"required":["schema","status","missing_fields","questions","scorer_chars","worker_bytes","scorer_limit_chars","worker_limit_bytes","scorer_digest"],"additionalProperties":false}'),
'RouteSuccess': _shape('ok={"const":true};route={"type":"object","properties":{"route_id":{"$ref":"#/$defs/ID"},"selection_mode":{"enum":["explicit","s1"]},"chosen":{"type":"object","properties":{"lane":{"const":"codex"},"provider":{"const":"codex-cli"},"model":{"type":"string","minLength":1},"account_ref":{"const":"native-default"}},"required":["lane","provider","model","account_ref"],"additionalProperties":false},"context":{"type":"object","properties":{"worker_bytes":{"type":"integer","minimum":0},"scorer_chars":{"type":"integer","minimum":0},"scorer_digest":{"anyOf":[{"$ref":"#/$defs/Hash"},{"type":"null"}]}},"required":["worker_bytes","scorer_chars","scorer_digest"],"additionalProperties":false},"quota":{"type":"object","properties":{"status":{"const":"fresh"},"source":{"const":"codex-app-server"},"sampled_at_ms":{"type":"integer","minimum":0}},"required":["status","source","sampled_at_ms"],"additionalProperties":false}},"required":["route_id","selection_mode","chosen","context","quota"],"additionalProperties":false}'),
'Ticket': _shape('ticket_id=ID;artifact_id=ID;method={"enum":["PUT","GET"]};url={"type":"string","format":"uri"};authorization={"type":"string","minLength":1};expires_at_ms={"type":"integer","minimum":0};bytes={"type":"integer","minimum":0};sha256=Hash;media_type=Media'),
'TicketSuccess': _shape('ok={"const":true};ticket=Ticket'),
'Receipt': _shape('ok={"const":true};task_id=ID;artifact=Artifact'),
}

PRIVATE = dict(WIRE)
PRIVATE.update({
'N': {'type': 'integer', 'minimum': 0, 'maximum': 9007199254740991},
'P': {'type': 'integer', 'minimum': 1, 'maximum': 9007199254740991},
'Text': {'type': 'string'},
'Nonblank': {'type': 'string', 'minLength': 1},
'Owner': {'type': 'string', 'minLength': 1},
'HTTPSOrigin': {'type': 'string', 'const': 'https://cowork.invalid'},
'Principal': _shape('issuer=Nonblank;subject=Nonblank;installation_id=ID;client_id=Nonblank;enabled={"type":"boolean"}'),
'Roots': _shape('state_dir=Nonblank;tasks_dir=Nonblank;blobs_dir=Nonblank;staging_dir=Nonblank'),
'CoreLimits': _shape('file_bytes=P;task_bytes=P;installation_bytes=P;artifacts_per_task=P;ticket_ttl_s=P;control_bytes=P;chunk_bytes=P;uploads_per_task=P;downloads_per_task=P;streams_per_installation=P;transfer_idle_ms=P;transfer_total_ms=P;ipc_idle_ms=P;workspace_poll_ms=P;disk_headroom_bytes=P'),
'Retention': _shape('payloads={"const":"explicit_delete"};history={"const":"preserve"};tombstones={"const":"preserve"};backups={"const":"external_not_managed"};automatic_cleanup={"const":false}'),
'CoreConfig': _shape('schema={"const":"cortados.cowork-core.v1"};mode={"const":"local_test"};installation_id=ID;principal=Principal;roots=Roots;limits=CoreLimits;retention=Retention;execution_policy=Policy;transfer_origin=HTTPSOrigin;ticket_key_version=Nonblank;ticket_key={"type":"string","pattern":"^[0-9a-f]*$","description":"Diagnostic hexadecimal bytes, never a transfer wire field","minLength":64,"maxLength":64}'),
'FrontendConfig': _shape('config_digest=Hash;limits=CoreLimits;transfer_origin=HTTPSOrigin'),
'PolicySnapshot': _shape('schema={"const":"cortados.cowork-snapshot.v1"};installation_id=ID;principal=Principal;roots=Roots;limits=CoreLimits;retention=Retention;execution_policy=Policy;transfer_origin=HTTPSOrigin;ticket_key_version=Nonblank;ticket_key_sha256=Hash;config_digest=Hash'),
'CaptureRef': _shape('name=Nonblank;sha256=Hash;kind={"enum":["synthetic","captured"]}'),
'NativeTemplate': _shape('id={"const":"cowork-task-v1"};default_permissions={"const":"cowork-task"};extends={"const":":workspace"};filesystem={"type":"object","properties":{"root":{"const":"deny"},"minimal":{"const":"read"},"tmpdir":{"const":"deny"},"slash_tmp":{"const":"deny"}},"required":["root","minimal","tmpdir","slash_tmp"],"additionalProperties":false};network={"const":false};strict_config={"const":true};ignore_user_config={"const":true};ignore_rules={"const":true};ephemeral={"const":true};skip_git_repo_check={"const":true};mcp_servers={"type":"array","maxItems":0}'),
'NativeManifest': _shape('schema={"const":"cortados.cowork-native.v1"};native_version={"const":"0.163.0"};binary_sha256=Hash;template=NativeTemplate;disabled_features={"type":"array","items":{"$ref":"#/$defs/Nonblank"},"maxItems":100};environment_names={"type":"array","items":{"$ref":"#/$defs/Nonblank"},"maxItems":100};environment_policy={"const":"ordinary-native-environment-v1"};captures={"type":"array","items":{"$ref":"#/$defs/CaptureRef"},"maxItems":100}'),
'WorkspaceIdentity': _shape('device=N;inode=P;relative_name=Nonblank'),
'BlobIdentity': _shape('device=N;inode=P;relative_name=Nonblank;bytes=N;sha256=Hash'),
'InputEntry': _shape('artifact=Artifact;blob=BlobIdentity'),
'OutputEntry': _shape('artifact=Artifact;blob=BlobIdentity;phase={"enum":["staged","renamed","sealed"]}'),
'Handoff': _shape('text=Text;worker_bytes=N;sha256=Hash'),
'AcceptanceSeed': _shape('owner_id=Owner;task_id=ID;task_group_id=ID;request_id=ID;argument_hash=Hash;expected_task_version=N;input_manifest={"type":"array","items":{"$ref":"#/$defs/InputEntry"},"maxItems":100};execution_policy=Policy;native_manifest=NativeManifest;handoff=Handoff;policy_snapshot=PolicySnapshot;preparation_id=ID'),
'RemoteBinding': _shape('job_id=ID;execution_kind={"const":"cowork_remote_v1"};seed=AcceptanceSeed;workspace_identity=WorkspaceIdentity?'),
'WorkerBinding': _shape('job_id=ID;execution_kind={"const":"cowork_remote_v1"};owner_id=Owner;task_id=ID;task_group_id=ID;input_manifest={"type":"array","items":{"$ref":"#/$defs/InputEntry"},"maxItems":100};execution_policy=Policy;native_manifest=NativeManifest;workspace_identity=WorkspaceIdentity;config_digest=Hash;limits=CoreLimits;handoff=Handoff'),
'TaskRow': _shape('owner_id=Owner;task_id=ID;group_id=ID;title=Text;state={"enum":["creating","ready","busy","quarantined","deleting","deleted"]};policy=PolicySnapshot;created_at_ms=N;updated_at_ms=N;version=N;active_job_id=ID?;root_identity=WorkspaceIdentity?;cleanup_error=Error?'),
'ObjectIDs': _shape('task_id=ID;artifact_ids={"type":"array","items":{"$ref":"#/$defs/ID"},"maxItems":100};job_id=ID?;ticket_id=ID?'),
'OperationRow': _shape('owner_id=Owner;operation={"enum":["task_create","task_delete","delegate","file_upload_ticket","file_download_ticket"]};request_id=ID;argument_hash=Hash;phase={"enum":["intent","committed","failed"]};object_ids=ObjectIDs;error=Error?;created_at_ms=N'),
'ArtifactRow': _shape('owner_id=Owner;task_id=ID;artifact_id=ID;filename=Name;media_type=Media;declared_bytes=N;declared_hash=Hash;observed_bytes=N?;observed_hash=Hash?;origin={"enum":["input","output"]};job_id=ID?;state={"enum":["reserved","uploading","sealed","quarantined"]};blob=BlobIdentity?;partial={"type":"boolean"};created_at_ms=N'),
'TicketRow': _shape('ticket_id=ID;owner_id=Owner;task_id=ID;artifact_id=ID;operation={"enum":["file_upload_ticket","file_download_ticket"]};request_id=ID;capability_hash=Hash;key_version=Nonblank;direction={"enum":["put","get"]};bytes=N;sha256=Hash;expires_at_ms=N;state={"enum":["issued","claimed","consumed","revoked"]};lease_id=ID?;connection_id=ID?'),
'PublicationRow': _shape('job_id=ID;owner_id=Owner;task_id=ID;manifest={"type":"array","items":{"$ref":"#/$defs/OutputEntry"},"maxItems":100};state={"enum":["pending","complete","failed","blocked"]};error=Error?'),
'ReservationRow': _shape('reservation_id=ID;owner_id=Owner;task_id=ID;operation={"enum":["task_create","task_delete","delegate","file_upload_ticket","file_download_ticket"]};request_id=ID;kind={"enum":["upload","workspace_copy","output_snapshot"]};bytes=N;materialized_bytes=N;state={"enum":["held","materialized","released"]}'),
'Lease': _shape('lease_id=ID;connection_id=ID;owner_id=Owner;task_id=ID;artifact_id=ID;ticket_id=ID;direction={"enum":["put","get"]};offset=N;deadline_ms=N;idle_deadline_ms=N;state={"enum":["open","closing","closed"]}'),
'ReadResult': _shape('body={"type":"string","pattern":"^[0-9a-f]*$","description":"Diagnostic hexadecimal bytes, never a transfer wire field"};offset=N;eof={"type":"boolean"}'),
'DeleteProgress': _shape('state={"enum":["deleting","deleted"]};cleanup_error=Error?'),
'RecoveryResult': _shape('state={"enum":["ready","quarantined","deleting","deleted"]};error=Error?'),
'TaskUsage': _shape('owner_id=Owner;task_id=ID;measured_bytes=N;promised_bytes=N;artifact_slots=N'),
'RemoteEvent': _shape('schema={"const":"cortados.cowork-event.v1"};event_id=ID;at_ms=N;event={"enum":["refusal","transition"]};entity={"enum":["runtime","task","artifact","ticket","publication","job","lease"]};object_id=ID?;request_id=ID?;from_state=Text?;state=Text?;reason={"enum":["invalid_request","idempotency_conflict","model_unavailable","account_unavailable","subscription_required","quota_unknown","quota_spent","scorer_unavailable","scoring_context_too_large","worker_context_too_large","needs_clarification","permission_refused","connector_unavailable","workspace_busy","capacity_exhausted","job_not_found","storage_error","native_version_mismatch","native_protocol_error","native_failed","output_limit","timeout","cancel_requested","broker_interrupted","containment_failed","job_not_terminal","task_not_found","artifact_not_found","task_deleted","task_quarantined","upload_incomplete","transfer_busy","ticket_invalid","ticket_expired","ticket_consumed","length_mismatch","hash_mismatch","artifact_corrupt","policy_unavailable","publication_failed"]}?'),
'CapacityUsage': _shape('tasks={"type":"array","items":{"$ref":"#/$defs/TaskUsage"},"maxItems":100};installation_measured_bytes=N;installation_promised_bytes=N;free_bytes=N;sampled_at_ms=N'),
'PreparationRow': _shape('preparation_id=ID;owner_id=Owner;task_id=ID;request_id=ID;argument_hash=Hash;expected_task_version=N;relative_name={"type":"string","minLength":1};workspace_identity=WorkspaceIdentity?;phase={"enum":["intent","prepared","adopted","retired","quarantined"]};job_id=ID?'),
})

ERROR_CODES = frozenset(WIRE['Error']['properties']['code']['enum'])


class RemoteError(Exception):
    def __init__(self, code, message=None):
        if code not in ERROR_CODES:
            raise ValueError('Unknown remote error code')
        self.code = code
        self.message = message or 'Remote operation refused: ' + code
        super().__init__(self.message)

    def payload(self):
        return {'ok': False, 'error': {'code': self.code, 'message': self.message}}


def from_worker_error(error):
    return RemoteError(error.code if error.code in ERROR_CODES else 'storage_error')


def plain(value):
    if isinstance(value, Record):
        return {key: plain(item) for key, item in value._values.items()}
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


def json_bytes(value):
    return canonical(plain(value)).encode('utf-8')


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RemoteError('invalid_request')
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        json_bytes(value)
        return value
    except (ValueError, UnicodeError, TypeError, AttributeError) as exc:
        raise RemoteError('invalid_request') from exc


def filename(value):
    if (not isinstance(value, str) or not value.strip() or value in ('.', '..')
            or len(value.encode('utf-8')) > 255
            or any(c in '/\\' or ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
        raise RemoteError('invalid_request')
    return value


def _semantic(value):
    if isinstance(value, float):
        raise RemoteError('invalid_request')
    if isinstance(value, str):
        value.encode('utf-8')
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == 'filename':
                filename(item)
            if key == 'owner_id':
                decoded = strict_json(item.encode('utf-8'))
                if (not isinstance(decoded, list) or len(decoded) != 3 or canonical(decoded) != item
                        or any(not isinstance(part, str) or not part.strip() for part in decoded)):
                    raise RemoteError('invalid_request')
                _validate('ID', decoded[2], WIRE)
            _semantic(item)
    elif isinstance(value, list):
        for item in value:
            _semantic(item)


def _validate(name, value, definitions):
    try:
        _semantic(value)
        validator = Draft202012Validator({'$ref': '#/$defs/' + name, '$defs': definitions})
        if not validator.is_valid(value):
            raise RemoteError('invalid_request')
    except (UnicodeError, ValueError, TypeError) as exc:
        raise RemoteError('invalid_request') from exc


def _freeze(value, field):
    if '$ref' in field:
        name = field['$ref'].rsplit('/', 1)[-1]
        if name in RECORD_NAMES:
            return globals()[name](**value)
        field = PRIVATE[name]
    if 'anyOf' in field:
        if value is None:
            return None
        return _freeze(value, field['anyOf'][0])
    if isinstance(value, dict):
        properties = field.get('properties', {})
        return MappingProxyType({key: _freeze(item, properties.get(key, {})) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item, field.get('items', {})) for item in value)
    return value


class Record:
    __slots__ = ('_values',)

    def __init__(self, **fields):
        name = type(self).__name__
        value = plain(fields)
        diagnostic = dict(value)
        byte_field = 'ticket_key' if name == 'CoreConfig' else 'body' if name == 'ReadResult' else None
        if byte_field:
            if not isinstance(value.get(byte_field), bytes):
                raise RemoteError('invalid_request')
            diagnostic[byte_field] = value[byte_field].hex()
        _validate(name, diagnostic, PRIVATE)
        if name == 'PreparationRow':
            if ((value['phase'] in ('prepared', 'adopted') and value['workspace_identity'] is None)
                    or ((value['job_id'] is not None) != (value['phase'] == 'adopted'))):
                raise RemoteError('invalid_request')
        if name == 'RemoteEvent':
            states = {'runtime': ('starting', 'ready', 'disabled', 'stopping', 'failed', 'closed'),
                      'task': tuple(WIRE['Task']['properties']['state']['enum']),
                      'artifact': tuple(WIRE['Artifact']['properties']['state']['enum']),
                      'publication': ('pending', 'complete', 'failed', 'blocked'),
                      'job': tuple(WIRE['Job']['properties']['state']['enum']),
                      'ticket': ('issued', 'claimed', 'consumed', 'revoked'), 'lease': ('open', 'closing', 'closed')}
            if value['event'] == 'refusal':
                if value['from_state'] is not None or value['state'] is not None:
                    raise RemoteError('invalid_request')
            elif (value['state'] not in states[value['entity']] or value['from_state'] == value['state']
                    or (value['from_state'] is not None and value['from_state'] not in states[value['entity']])):
                raise RemoteError('invalid_request')
        properties = PRIVATE[name]['properties']
        frozen = {key: _freeze(item, properties[key]) for key, item in value.items()}
        object.__setattr__(self, '_values', MappingProxyType(frozen))

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        raise AttributeError('Remote records are immutable')

    def __eq__(self, other):
        return type(self) is type(other) and self._values == other._values


RECORD_NAMES = tuple(name for name, schema in PRIVATE.items()
                     if schema.get('type') == 'object' and name not in WIRE) + ('Policy', 'Artifact', 'Error')
for _name in RECORD_NAMES:
    globals()[_name] = type(_name, (Record,), {'__slots__': ()})


def owner(principal):
    return canonical([principal.issuer, principal.subject, principal.installation_id])


TOOLS = dict(task_create='Create', task_get='TaskKey', task_delete='Delete',
             context_requirements='Context', route='Route', delegate='Delegate',
             job_status='JobKey', job_cancel='JobKey', job_result='JobKey', job_feedback='Feedback',
             file_upload_ticket='Upload', file_download_ticket='Download')


def remote_tool_schema(tool):
    if tool not in TOOLS:
        raise RemoteError('invalid_request')
    return {'$ref': '#/$defs/' + TOOLS[tool], '$defs': plain(WIRE)}


def validate_remote(tool, args, config):
    if tool not in TOOLS:
        raise RemoteError('invalid_request')
    value = plain(args)
    _validate(TOOLS[tool], value, WIRE)
    if tool == 'task_create':
        value.setdefault('title', '')
    if tool in ('context_requirements', 'route', 'delegate'):
        value.setdefault('context', '')
    if tool in ('route', 'delegate'):
        value.setdefault('constraints', {})
        value.setdefault('permissions', {'network': False, 'mcp_servers': []})
        value['permissions'].setdefault('network', False)
        value['permissions'].setdefault('mcp_servers', [])
        value.setdefault('account_ref', 'native-default')
        if value['selection']['mode'] != 's1' and 'min_band' in value['constraints']:
            raise RemoteError('invalid_request')
    return value


def emit_remote_event(tx, event):
    event = event if isinstance(event, RemoteEvent) else RemoteEvent(**event)
    if tx is None:
        sys.stderr.write(json_bytes(event).decode() + '\n')
        sys.stderr.flush()
    else:
        tx.execute('INSERT INTO remote_audit(event_id,event_json) VALUES (?,?)',
                   (event.event_id, json_bytes(event).decode()))


def artifact_projection(row):
    value = plain(row)
    keys = ('artifact_id', 'filename', 'media_type', 'origin', 'job_id', 'state', 'partial', 'created_at_ms')
    return {**{key: value[key] for key in keys}, 'bytes': value['declared_bytes'], 'sha256': value['declared_hash']}


def project_remote_result(record):
    value = plain(record)
    if not isinstance(value, dict):
        raise RemoteError('invalid_request')
    kind = value.get('kind')
    variants = {'task': ('task', 'artifacts'), 'job': ('job', 'binding', 'publication'),
                'result': ('job', 'binding', 'publication', 'artifacts'),
                'ticket': ('ticket', 'authorization', 'transfer_origin', 'media_type'),
                'receipt': ('task_id', 'artifact'), 'route': ('route',), 'context': ('value',),
                'feedback': ('value',), 'failure': ('error',)}
    if kind not in variants or set(value) != {'kind', *variants[kind]}:
        raise RemoteError('invalid_request')
    if kind == 'failure':
        Error(**value['error'])
        return {'ok': False, 'error': value['error']}
    if kind in ('context', 'feedback'):
        result = value['value']
    elif kind == 'route':
        route = value['route']
        result = {'ok': True, 'route': {key: route[key] for key in ('route_id', 'selection_mode', 'chosen', 'context', 'quota')}}
    elif kind == 'receipt':
        result = {'ok': True, 'task_id': value['task_id'], 'artifact': artifact_projection(value['artifact'])}
    elif kind == 'task':
        task = TaskRow(**value['task'])
        result = {'ok': True, 'task': {key: getattr(task, key) for key in
                  ('task_id', 'title', 'state', 'created_at_ms', 'active_job_id')}}
        result['task'].update(task_group_id=task.group_id, expires_at_ms=None,
            cleanup_error=plain(task.cleanup_error),
            artifacts=[] if task.state == 'deleted' else [artifact_projection(a) for a in sorted(value['artifacts'], key=lambda a: (a['created_at_ms'], a['artifact_id']))],
            limits={**{key: getattr(task.policy.limits, key) for key in WIRE['Limits']['required'] if key != 'retention'}, 'retention': 'explicit_delete'})
    elif kind == 'ticket':
        ticket = TicketRow(**value['ticket'])
        result = {'ok': True, 'ticket': {key: getattr(ticket, key) for key in ('ticket_id', 'artifact_id', 'expires_at_ms', 'bytes', 'sha256')}}
        result['ticket'].update(method='PUT' if ticket.direction == 'put' else 'GET',
            url=value['transfer_origin'] + '/transfers/' + ticket.ticket_id,
            authorization=value['authorization'], media_type=value['media_type'])
    else:
        from .job_store import JobStore, TERMINAL
        binding = RemoteBinding(**value['binding'])
        publication = PublicationRow(**value['publication'])
        job = value['job']
        projected = JobStore.snapshot(job)
        projected.pop('cwd')
        projected.update(task_id=binding.seed.task_id, execution_policy=plain(binding.seed.execution_policy),
                         publication=publication.state, publication_error=plain(publication.error))
        result = {'ok': True, 'job': projected}
        if kind == 'result':
            native = job['result'] if job['state'] in TERMINAL else None
            result['result'] = None if native is None else {key: native[key] for key in
                ('summary', 'worker', 'usage', 'warnings', 'verification')}
            if native is not None:
                error = native.get('error')
                result['result'].update(changed_paths=None,
                    error=RemoteError(error['code']).payload()['error'] if error else None,
                    artifacts=[plain(entry.artifact) for entry in publication.manifest] if publication.state == 'complete' else [])
    schema = {'task': 'TaskSuccess', 'job': 'JobSuccess', 'result': 'ResultSuccess', 'ticket': 'TicketSuccess',
              'receipt': 'Receipt', 'route': 'RouteSuccess', 'context': 'ContextSuccess', 'feedback': 'FeedbackSuccess'}[kind]
    _validate(schema, result, WIRE)
    return result
