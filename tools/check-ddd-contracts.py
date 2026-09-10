#!/usr/bin/env python3
"""Local DDD contract/transition evidence, never service or external qualification.

Runs strict schema examples and negative authority fields, descriptor/definition
compatibility, registration fault projections, lifecycle and accounting models,
and compiled Protobuf compatibility for the next P0-A validation method.
"""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import itertools
import re
import json
from pathlib import Path
import subprocess
import tempfile
from collections import Counter
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
COUNTS = Counter()


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def read(path):
    return json.loads((ROOT / path).read_text(), object_pairs_hook=reject_duplicates)


SCHEMAS = {}
for path in (ROOT / 'contracts').rglob('*.schema.json'):
    value = read(path)
    SCHEMAS[value['$id']] = value
REGISTRY = Registry().with_resources((key, Resource.from_contents(value)) for key, value in SCHEMAS.items())


def valid(schema, value):
    return Draft202012Validator({'$ref': schema}, registry=REGISTRY).is_valid(value)


def check(condition, label, category='assertions'):
    if not condition:
        raise AssertionError(label)
    COUNTS[category] += 1


def validate_references(value):
    if isinstance(value, dict):
        if '$ref' in value and value['$ref'].startswith('urn:'):
            REGISTRY.resolver().lookup(value['$ref'])
            COUNTS['resolved_refs'] += 1
        for item in value.values():
            validate_references(item)
    elif isinstance(value, list):
        for item in value:
            validate_references(item)


def schema_checks():
    for schema in SCHEMAS.values():
        validate_references(schema)
    fixtures = read('contracts/definitions/p0-contract-v1.fixtures.json')['examples']
    fixtures += [{'id': name, 'schema': 'urn:anvilkit:poll-policy:v1', 'value': read(f'contracts/definitions/{name}-poll-v1.json')} for name in ['review', 'publication', 'activation']]
    fixtures.append({'id': 'safety', 'schema': 'urn:anvilkit:runner-safety-bindings:v1', 'value': read('contracts/actions/runner-safety-bindings.example.json')})
    for example in fixtures:
        uri, value = example['schema'], example['value']
        check(valid(uri, value), example['id'], 'schema_positive')
        check(not valid(uri, {**value, 'dispatchAllowed': True}), example['id']+' unknown authority', 'schema_negative')
        for key in value:
            malformed = {**value, key: None}
            check(not valid(uri, malformed), example['id']+' null '+key, 'schema_negative')
    by_id = {x['id']: x for x in fixtures}
    # These rejections substantiate identity preservation and credential boundaries.
    bad = copy.deepcopy(by_id['source']['value']); bad['files'][0]['path'] = '../escape'
    check(not valid(by_id['source']['schema'], bad), 'source path escape', 'schema_negative')
    bad = copy.deepcopy(by_id['profile']['value']); bad['containerCapabilities'].append('SYS_ADMIN')
    check(not valid(by_id['profile']['schema'], bad), 'fourth capability', 'schema_negative')
    bad = copy.deepcopy(by_id['validation-response']['value']); bad['definitionDigest'] = 'sha256:'+'a'*64
    check(not valid(by_id['validation-response']['schema'], bad), 'failed validation cannot mint digest', 'schema_negative')
    bad = copy.deepcopy(by_id['binding']['value']); del bad['imageDigest']
    check(not valid(by_id['binding']['schema'], bad), 'job binding requires image', 'schema_negative')
    for example in fixtures:
        if example['id'].startswith('event-'):
            bad = copy.deepcopy(example['value']); bad['payload']['dispatchAllowed'] = True
            check(not valid(example['schema'],bad), 'event payload authority injection', 'schema_negative')
    for event, state, receipt in [('review.observed','reviewState','approvalRef'),('publication.observed','npmState','npmReceiptRef'),('activation.observed','activationState','activationReceiptRef')]:
        example=by_id['event-'+event];bad=copy.deepcopy(example['value'])
        bad['payload'][state]={'reviewState':'approved','npmState':'published','activationState':'active'}[state]
        check(not valid(example['schema'],bad),'success event needs receipt','schema_negative')
    bad=copy.deepcopy(by_id['registration']['value']);bad['state']='registered'
    check(not valid(by_id['registration']['schema'],bad),'registered requires durable settlement intent','schema_negative')
    bad=copy.deepcopy(by_id['meter']['value']);bad['categories'][0]['unitDenominator']='0'
    check(not valid(by_id['meter']['schema'],bad),'zero pricing denominator','schema_negative')
    try:
        json.loads('{"valid":true,"valid":false}', object_pairs_hook=reject_duplicates)
    except ValueError:
        COUNTS['schema_negative'] += 1
    else:
        raise AssertionError('duplicate authority accepted')


def descriptor_checks():
    descriptors = read('contracts/actions/registry.json')['descriptors']
    catalog = {x['id']: x for x in descriptors}
    enums = read('contracts/values/agent-enums-v1.schema.json')['$defs']
    check(set(catalog) == set(enums['actionId']['enum']) and len(descriptors) == 13, 'complete registry', 'descriptor_checks')
    for d in descriptors:
        check(valid('urn:anvilkit:action-descriptor:v2', d), d['id'], 'descriptor_checks')
        check(set(d['controlOutcomes']) <= set(d['outcomes']), 'control subset', 'descriptor_checks')
        for port in d['inputs'].values():
            REGISTRY.resolver().lookup('urn:anvilkit:reference-ports:v1#/$defs/'+port['schemaRef'])
        for port in d['outputs'].values():
            check(set(port['availableOn']) <= set(d['outcomes']), 'output availability', 'descriptor_checks')
        if d['id'] in ['component.validate','component.publication-status']:
            check(d == read('contracts/actions/'+d['id'].replace('.','-')+'.example.json'), 'preserved descriptor', 'descriptor_checks')
    for family in ['generation','release']:
        definition = read(f'contracts/definitions/component-{family}.example.json')
        for step in definition['steps']:
            d = catalog[step['action']['id']]
            expected = set(d['outcomes']) - set(d['controlOutcomes'])
            if step['kind'] == 'wait':
                expected.remove(step['wait']['pendingOutcome'])
                check(d['effects'] == ['read'], 'wait is read-only', 'descriptor_checks')
            check(set(step['on']) == expected, 'exact ordinary edges '+step['id'], 'descriptor_checks')
            check(set(step['inputs']) == set(d['inputs']), 'required input ports', 'descriptor_checks')
            check(set(step['outputs']) <= set(d['outputs']), 'output ports', 'descriptor_checks')
    for p in read('contracts/jobs/job-kind-profiles-v1.json')['profiles']:
        d = catalog[p['actionId']]
        check(set(p['inputKinds']) == {x['kind'] for x in d['inputs'].values()}, 'job inputs agree', 'profile_checks')
        for port,outcomes in p['requiredOutputsByOutcome'].items():
            check(outcomes == d['outputs'][port]['availableOn'], 'job output availability', 'profile_checks')


def apply_patch(value, path, replacement):
    parts = path.split('/'); current = value
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current,list) else current[part]
    key = int(parts[-1]) if isinstance(current,list) else parts[-1]
    if replacement is None:
        del current[key]
    else:
        current[key] = replacement


def registration_decision(record, facts):
    """Reference acceptance projection over MOCK verified adapter observations."""
    effects=record['effects'];binding=record['binding']
    if [e['kind'] for e in effects]!=['source-snapshot','candidate-designation','registry-entry'] or len({e['effectId'] for e in effects})!=3 or len({e['commandId'] for e in effects})!=3:
        return 'conflicted'
    if binding['sourceRef']['contentDigest']!=binding['sourceDigest'] or binding['validationRef']['contentDigest']!=binding['validationDigest']:
        return 'conflicted'
    if facts['validationSourceDigest'] != record['binding']['sourceDigest']:
        return 'conflicted'
    if facts['currentCandidateSnapshotId'] != record['effects'][0].get('snapshotId'):
        return 'conflicted'
    if not facts['acceptanceDeadlineValid'] or not facts['fundingConfirmed']:
        return 'blocked'
    unknown = ['snapshot_unknown','designation_unknown','registry_unknown']
    completed = ['prepared','source_saved','candidate_designated']
    for i,effect in enumerate(record['effects']):
        if effect['state'] == 'not-dispatched':
            return completed[i] if facts['leaseValid'] else 'blocked'
        if effect['state'] != 'applied':
            return unknown[i]
        if not effect.get('provenanceRef') or set(effect['binding']) != set(record['binding']):
            return unknown[i]
        if effect['binding'] != record['binding'] or effect['snapshotId'] != record['effects'][0]['snapshotId']:
            return 'conflicted'
        expected=record['binding']['baseRevision'] if i==0 else effects[i-1]['resultRevision']
        if effect.get('expectedRevision')!=expected:
            return 'conflicted'
    if not valid('urn:anvilkit:candidate-registration:v1',record):
        return 'registry_unknown'
    return 'registered'


def registration_checks():
    fixture=read('contracts/actions/candidate-registration-v1.fixtures.json')
    for case in fixture['cases']:
        record=copy.deepcopy(fixture['baseline']); facts=copy.deepcopy(fixture['facts'])
        source_before=copy.deepcopy(record['binding'])
        for key,value in case['patch'].items():
            apply_patch(record if key.startswith('effects/') else facts,key,value)
        actual=registration_decision(record,facts)
        check(actual==case['expected'],case['id']+': '+actual,'registration_cases')
        check(record['binding']==source_before,'recovery retains source/report')
    # Lost responses followed by a verifiable original-identity query resolve once.
    for index in range(3):
        record=copy.deepcopy(fixture['baseline']); proof=copy.deepcopy(record['effects'][index])
        record['effects'][index]['state']='unknown'
        for successor in range(index+1,3):
            record['effects'][successor]['state']='not-dispatched'
        check(registration_decision(record,fixture['facts'])!='registered','unknown predecessor blocks')
        record['effects'][index]=proof
        for successor in range(index+1,3):
            check(registration_decision(record,fixture['facts']) in ['source_saved','candidate_designated'],'successor requires verified predecessor')
            record['effects'][successor]=copy.deepcopy(fixture['baseline']['effects'][successor])
        accepted={}
        for _ in range(2):
            if registration_decision(record,fixture['facts'])=='registered':
                accepted.setdefault(record['binding']['operationId'],'one-settlement-intent')
        check(len(accepted)==1,'duplicate receipt/acceptance creates one settlement','registration_cases')


def resume_allowed(s):
    if not all(s[k] for k in ['operatorAuthorized','scopeAuthorized','subjectUnchanged','senderResolved']) or s['effectsUnknown']:
        return False
    now=s['now']
    if s['kind']=='generation':
        if not s['sourceUnchanged'] or not s['budgetEligible']: return False
        if s.get('firstPermitAt') is None: return s['queueExpiresAt']>now
        if s['stage']=='bootstrap-before-lease': return s['activeDeadline']>now
        return s['activeDeadline']>now and s['leaseValid'] and s['fundingConfirmed']
    if s['kind']=='preview': return s['previewCurrent'] and s['previewDeadline']>now
    if s['stage']=='awaiting_review': return s['reviewDeadline']>now
    return s['approvalCurrent']


def lifecycle_checks():
    fixture=read('contracts/definitions/lifecycle-v1.fixtures.json')
    for case in fixture['resume']:
        before=copy.deepcopy(case['input'])
        check(resume_allowed(case['input'])==case['expected'],case['id'],'resume_cases')
        check(before==case['input'],'resume cannot reset identities or clocks')
    # Reducer deliberately gives acknowledgement no closure/failure semantics.
    def observe(state,event,identity='cmd-1'):
        if event=='acknowledge':
            state['commands'].setdefault(identity,'acknowledged')
        elif event=='receipt':
            state['effect']='applied';state['receipts'].add('original-receipt')
        elif event=='crash':
            state['restarts']+=1
        elif event=='close' and state['effect']=='unknown':
            return False
        return True
    for kind in fixture['effectClasses']:
        for interface in fixture['publicationInterfaces'] if kind=='publication' else ['generic']:
            state={'public':'blocked','effect':'unknown','owner':'workflow-original','operationId':'op-original','effectId':'effect-original','commands':{},'receipts':set(),'restarts':0,'cost':80}
            for event in ['acknowledge','acknowledge','crash','close']:
                observe(state,event)
            check(state['effect']=='unknown' and state['public']=='blocked' and state['owner']=='workflow-original' and len(state['commands'])==1,kind+interface,'disposition_cases')
            observe(state,'receipt');observe(state,'receipt')
            check(state['effect']=='applied' and len(state['receipts'])==1 and state['cost']==80 and state['owner']=='workflow-original','late receipt preserved','disposition_cases')
    # Evidence-bearing dispositions have identical gates at either publication interface.
    def disposition(state, command, evidence=None):
        key=command['commandId'];body=(command['action'],evidence)
        if key in state['decisions']:
            return state['decisions'][key][1] if state['decisions'][key][0]==body else 'IDEMPOTENCY_CONFLICT'
        if command['expectedRevision']!=state['revision']: return 'REVISION_CONFLICT'
        action=command['action']
        if action=='confirm-applied' and evidence!='original-applied-receipt': return 'EVIDENCE_REQUIRED'
        if action=='confirm-absent' and evidence!='original-absent-and-send-terminated': return 'EVIDENCE_REQUIRED'
        result='acknowledged' if action=='abandon-unresolved' else action
        state['decisions'][key]=(body,result);state['revision']+=1
        if action=='confirm-applied':state['effect']='applied'
        if action=='confirm-absent':state['effect']='absent-confirmed'
        return result
    for kind in fixture['effectClasses']:
        for action in fixture['dispositions']:
            state={'revision':1,'effect':'unknown','decisions':{},'owner':'workflow-original','dispatchAllowed':False}
            command={'commandId':'disposition-1','expectedRevision':1,'action':action}
            evidence={'confirm-applied':'original-applied-receipt','confirm-absent':'original-absent-and-send-terminated'}.get(action)
            before=copy.deepcopy(state)
            if evidence:
                check(disposition(state,command,None)=='EVIDENCE_REQUIRED' and state==before,'ack is not evidence','disposition_cases')
            # Aborted precommit transaction changes nothing; after commit exact replay dedups.
            speculative=copy.deepcopy(state);disposition(speculative,command,evidence)
            check(state==before,'crash before commit retains original state','disposition_cases')
            result=disposition(state,command,evidence);committed=copy.deepcopy(state)
            check(disposition(state,command,evidence)==result and state==committed,'crash after commit duplicate','disposition_cases')
            check(disposition(state,{**command,'action':'different'},evidence)=='IDEMPOTENCY_CONFLICT','changed command conflict','disposition_cases')
            check(disposition(state,{**command,'commandId':'late'},evidence)=='REVISION_CONFLICT','late stale disposition','disposition_cases')
            check(not state['dispatchAllowed'] and state['owner']=='workflow-original','no disposition mints dispatch or owner','disposition_cases')
    for interface in fixture['publicationInterfaces']:
        outputs={'npm':'applied','browser':'unknown'}
        allowed_resolutions=[] if 'unknown' in outputs.values() else ['terminate-failed']
        check(allowed_resolutions==[],interface+' partial unknown cannot terminate','disposition_cases')


def cost_checks():
    # Serializations of concurrent database operations, NOT a database concurrency proof.
    for policy in ['proposed-synchronous-freeze','alternative-grandfather']:
        for order in itertools.permutations(['correction','claimB','intake','topup','returnB','rollover']):
            allocation={'A':60,'B':40};cost={'A':0,'B':0};exposure={'A':60,'B':0};fenced=False;correction_ids=set();claims=[];period='original'
            for event in order:
                committed=lambda:sum(max(allocation[k],cost[k]+exposure[k]) for k in allocation)
                if event=='correction':
                    for _ in range(2):
                        if 'correction-1' not in correction_ids:
                            cost['A']=80;exposure['A']=0;correction_ids.add('correction-1')
                    if policy=='proposed-synchronous-freeze' and committed()>100:fenced=True
                elif event=='claimB':
                    if not fenced and allocation['B']-cost['B']-exposure['B']>=40:
                        exposure['B']=40;claims.append(event)
                elif event in ['intake','topup']:
                    if committed()+1<=100:
                        allocation['C' if event=='intake' else 'B']=allocation.get('C' if event=='intake' else 'B',0)+1
                        cost.setdefault('C',0);exposure.setdefault('C',0)
                elif event=='returnB': allocation['B']=cost['B']+exposure['B']
                elif event=='rollover':period='next'
                check(sum(cost.values())>=cost['A'],'liability rollup')
            check(cost['A']==80 and len(correction_ids)==1,'one truthful correction','cost_schedules')
            # Previously issued B request may settle even when the correction fenced B.
            if claims:
                cost['B']=40;exposure['B']=0
            # Original period attribution is retained even after rollover.
            actor={'actorA':cost['A'],'actorB':cost['B']};tenant={'tenantA':actor['actorA'],'tenantB':actor['actorB']}
            check(sum(actor.values())==sum(tenant.values())==sum(cost.values()),'all hierarchy levels truthful')
            if policy=='proposed-synchronous-freeze' and order.index('correction')<order.index('claimB') and order.index('correction')<order.index('returnB'):
                check(not claims,'no post-correction grant')
            check(period=='next','rollover observed without moving cost')


def sql_checks():
    # Structural, not a statement count: a bare count breaks on any edit and says nothing about the
    # contract. R02 (2026-09-10) added the ownership statements this now asserts directly.
    from pglast import parse_sql
    text=(ROOT/'contracts/definitions/activation-v1.sql').read_text()
    parse_sql(text)
    check(True,'activation DDL parses','sql_parse_checks')
    relations=set(re.findall(r'CREATE TABLE\s+definition_contract\.([a-z_]+)',text))
    check(relations=={'immutable_records','activations','activation_pointers'},'activation DDL relations','sql_parse_checks')
    guarded=set(re.findall(r'ON definition_contract\.([a-z_]+) FOR EACH ROW\nEXECUTE FUNCTION definition_contract\.reject_mutation',text))
    check(guarded=={'immutable_records','activations'},'the two immutable relations are guarded','sql_parse_checks')
    check('activation_pointers' not in guarded,'the activation pointer stays mutable for its CAS','sql_parse_checks')
    check('CREATE SCHEMA definition_contract AUTHORIZATION anvilkit_control_migrator' in text
          and text.count('SET ROLE anvilkit_control_migrator;')==1 and text.count('RESET ROLE;')==1,
          'activation objects are owned by the migrator','sql_parse_checks')
    roles=(ROOT/'contracts/sql/roles-v1.sql').read_text()
    for role in ('anvilkit_control_migrator','anvilkit_control_rw','anvilkit_api_ro'):
        check(f'CREATE ROLE {role} NOLOGIN' in roles,f'{role} is created in migration step 1','sql_parse_checks')
    check('NOBYPASSRLS' in roles and roles.count('NOBYPASSRLS')==2,'both service roles cannot bypass RLS','sql_parse_checks')


def bundle_checks():
    bundle=read('contracts/definitions/p0-local-bundle-v1.json')
    check(bundle['status']=='draft' and bundle['ownerReview']=='pending' and bundle['enabledEffectPermissions']==[], 'local bundle grants no authority', 'bundle_checks')
    listed=set()
    for record in bundle['artifacts']:
        path=ROOT/record['path']
        check(record['path'] not in listed and hashlib.sha256(path.read_bytes()).hexdigest()==record['sha256'], record['path']+' bundle digest', 'bundle_checks')
        listed.add(record['path'])
    actual={str(p.relative_to(ROOT)) for p in (ROOT/'contracts').rglob('*') if p.is_file() and p.name!='p0-local-bundle-v1.json'}
    check(listed==actual,'retained local bundle closure','bundle_checks')


def protobuf_checks():
    with tempfile.TemporaryDirectory(prefix='ddd-proto-') as directory:
        subprocess.run(['protoc','-I',str(ROOT/'contracts/proto'),'--python_out='+directory,str(ROOT/'contracts/proto/definition-validation-v1.proto')],check=True)
        spec=importlib.util.spec_from_file_location('definition_validation_v1_pb2',Path(directory)/'definition_validation_v1_pb2.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        fixtures={x['id']:x['value'] for x in read('contracts/definitions/p0-contract-v1.fixtures.json')['examples']}
        request=fixtures['validation-request'];wire=module.ValidateDefinitionRequest(definition_json=json.dumps(request['definition']).encode(),descriptor_digest=request['descriptorDigest'],runtime_profile_ref=request['runtimeProfileRef'],policy_refs=request['policyRefs'])
        decoded=module.ValidateDefinitionRequest.FromString(wire.SerializeToString())
        restored={'definition':json.loads(decoded.definition_json),'descriptorDigest':decoded.descriptor_digest,'runtimeProfileRef':decoded.runtime_profile_ref,'policyRefs':list(decoded.policy_refs)}
        check(restored==request,'explicit public/private mapping','protobuf_roundtrips')
        response=fixtures['validation-response'];wire=module.ValidateDefinitionResponse(valid=False,descriptor_digest=response['descriptorDigest'],runtime_profile_ref=response['runtimeProfileRef'])
        for issue in response['issues']:wire.issues.add(code=issue['code'],path=issue['path'],message=issue['message'])
        decoded=module.ValidateDefinitionResponse.FromString(wire.SerializeToString())
        check(decoded.HasField('valid') and not decoded.valid and not decoded.HasField('definition_digest'),'presence retained','protobuf_roundtrips')
        check(not decoded.issues[0].HasField('step_id') and decoded.issues[0].HasField('path'),'optional versus empty','protobuf_roundtrips')


if __name__=='__main__':
    for run in [schema_checks,descriptor_checks,registration_checks,lifecycle_checks,cost_checks,protobuf_checks,sql_checks,bundle_checks]:
        run()
    print(json.dumps(dict(COUNTS),sort_keys=True))
    print('PASS: local schemas, compatibility and synthetic transition models; no runtime qualification')
