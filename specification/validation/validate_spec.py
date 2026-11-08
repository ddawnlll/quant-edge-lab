#!/usr/bin/env python3
"""Offline architecture-package validator. This is not a simulator or a runtime conformance test."""
import argparse, copy, hashlib, json, re, sys, unicodedata
from pathlib import Path
from decimal import Decimal
from fractions import Fraction
from datetime import datetime, timezone
import jsonschema
from jsonschema import Draft202012Validator, validators

def integer_domain(validator, rule, value, schema):
 if not isinstance(value,str):return
 try:
  n=int(value)
  if not int(rule['minimum'])<=n<=int(rule['maximum']) or value=='-0':
   yield jsonschema.ValidationError('Exact integer outside declared domain or negative zero')
 except (ValueError,TypeError):yield jsonschema.ValidationError('Invalid exact integer')
def decimal_domain(validator, rule, value, schema):
 if not isinstance(value,str):return
 try:
  n=Decimal(value)
  if not n.is_finite() or (n==0 and value.startswith('-')):
   yield jsonschema.ValidationError('Nonfinite decimal or negative zero')
 except Exception:yield jsonschema.ValidationError('Invalid exact decimal')
def decimal_bounds(validator, rule, value, schema):
 if not isinstance(value,str):return
 try:
  if not Decimal(rule['minimum'])<=Decimal(value)<=Decimal(rule['maximum']):
   yield jsonschema.ValidationError('Decimal outside declared bounds')
 except Exception:yield jsonschema.ValidationError('Invalid bounded decimal')
QelValidator=validators.extend(Draft202012Validator,{
 'x-qel-integer':integer_domain,'x-qel-decimal':decimal_domain,'x-qel-decimal-bounds':decimal_bounds})

CATALOGS=['human_sections','sources','decisions','components','principals','contracts','requirements','operators','state_machines','gates','capabilities','resource_profiles','tables','tests','benchmarks','invariants','tasks','milestones','open_risks','baseline_sections','original_requirements','model_bindings','runtime_semantic_rules','error_codes','build_benchmarks']
def fail(msg):raise ValueError(msg)
def require(ok,msg):
 if not ok:fail(msg)
def pairs(items):
 d={}
 for k,v in items:
  require(k not in d,'Duplicate JSON key: '+k);d[k]=v
 return d
def load(p):return json.loads(p.read_text(encoding='utf-8'),object_pairs_hook=pairs,parse_constant=lambda s:fail('Nonfinite JSON '+s))
def check_schema(x,s,name):
 errors=sorted(QelValidator(s).iter_errors(x),key=lambda e:str(list(e.path)))
 require(not errors,f'{name}: '+('; '.join(str(list(e.path))+': '+e.message[:260] for e in errors[:3])))
def dag(records,name):
 ids={r['id'] for r in records};edges={r['id']:r.get('depends_on',[]) for r in records};done=set();active=set()
 def visit(x):
  require(x in ids,f'{name} unresolved dependency {x}')
  require(x not in active,f'{name} cycle at {x}')
  if x in done:return
  active.add(x)
  for q in edges[x]:visit(q)
  active.remove(x);done.add(x)
 for x in ids:visit(x)
def type_vectors(runtime):
 vectors=[
  ('GitOid','a'*40,True),('GitOid','a'*64,True),('GitOid','a'*32,False),('Hash','a'*40,False),
  ('Ns',str(2**64-1),True),('Ns',str(2**64),False),('Ns',1,False),
  ('I64',str(-(2**63)),True),('I64',str(2**63),False),('I64','-0',False),
  ('MoneyCoeff',str(2**100),True),('MoneyCoeff',str(2**127),False),
  ('Decimal','0.25',True),('Decimal','1.00',False),('Decimal','-0',False),('Decimal','1e3',False)
 ]
 for name,value,want in vectors:
  actual=not list(QelValidator({**runtime,'$ref':'#/$defs/'+name}).iter_errors(value))
  require(actual==want,'Runtime type vector failed '+name+' '+str(value))
 return len(vectors)
def validate(root,a=None,fixtures=None,examples=None,check_human=True):
 a=copy.deepcopy(a) if a is not None else load(root/'spec/architecture.json')
 fixtures=fixtures or load(root/'fixtures/conformance.json')
 schemas={p.name:load(p) for p in sorted((root/'schemas').glob('*.json'))}
 for name,s in schemas.items():Draft202012Validator.check_schema(s)
 check_schema(a,schemas['architecture.schema.json'],'architecture')
 check_schema(fixtures,schemas['fixtures.schema.json'],'fixtures')
 runtime=schemas['runtime.schema.json'];defs=runtime['$defs']
 ids={};typed={k:{x['id'] for x in a[k]} for k in CATALOGS}
 for k in CATALOGS:
  for x in a[k]:
   require(x['id'] not in ids,'Duplicate ID '+x['id']);ids[x['id']]=x
 for f in fixtures['fixtures']:
  require(f['id'] not in ids,'Fixture ID collision');ids[f['id']]=f
 roles={x['id'] for x in a['agent_protocol']['roles']}
 tests=typed['tests']|typed['benchmarks']|typed['gates']|{x['id'] for x in fixtures['fixtures']}
 def refs(values,allowed,where):
  for v in values:require(v in allowed,f'{where}: unresolved {v}')
 def walk(x,path=''):
  if isinstance(x,dict):
   for k,v in x.items():
    if k=='baseline_refs':refs(v,typed['baseline_sections'],path)
    if k=='human_ref':refs([v],typed['human_sections'],path)
    if k=='human_refs':refs(v,typed['human_sections'],path)
    walk(v,path+'/'+k)
  elif isinstance(x,list):
   for i,v in enumerate(x):walk(v,path+f'/{i}')
  elif isinstance(x,str):require(unicodedata.normalize('NFC',x)==x,'Non-NFC text at '+path)
 walk(a);walk(fixtures)
 for c in a['components']:
  refs(c['depends_on'],typed['components'],c['id']);refs(c['provides_contracts']+c['consumes_contracts'],typed['contracts'],c['id']);refs(c['capability_ids'],typed['capabilities'],c['id']);refs([c['principal']],typed['principals'],c['id'])
  refs(c['outputs'],set(defs),c['id'])
  refs(c['failure_codes'],typed['error_codes'],c['id'])
 for payload in a['artifact_payloads']:refs(payload['required_schema_defs'],set(defs),'artifact payload')
 for payload in a['ledger_payload_registry']:
  require(payload['schema_ref'].split('/')[-1] in defs,'Ledger payload schema unresolved')
 require(set(runtime['$defs']['LedgerEventType']['enum'])=={x['event_type'] for x in a['ledger_payload_registry']},'Ledger discriminant registry mismatch')
 for rule in a['runtime_semantic_rules']:
  refs(rule['schema_defs'],set(defs),rule['id']);refs(rule['test_ids'],tests,rule['id'])
 for c in a['contracts']:
  refs([c['owner']],typed['components'],c['id']);refs(c['roles'],typed['principals'],c['id'])
  require(c['version']==a['meta']['version'],c['id']+' contract version mismatch')
  for k in ['input_schema','output_schema','error_schema']:
   file,frag=c[k].split('#');require(file=='runtime.schema.json' and frag.startswith('/$defs/'),'Invalid schema reference');require(frag.split('/')[-1] in defs,c['id']+' missing schema fragment')
  if 'completion_contract' in c:refs([c['completion_contract']],typed['contracts'],c['id'])
 for key,fields in [('requirements',['verification_ids']),('invariants',['verification_ids']),('capabilities',['fixture_test_ids']),('decisions',['closure_test_ids']),('benchmarks',['correctness_test_ids']),('open_risks',['closure_test_ids']),('milestones',['exit_test_ids'])]:
  for x in a[key]:
   for fld in fields:refs(x[fld],tests,x['id'])
 for r in a['requirements']:refs(r['component_ids'],typed['components'],r['id'])
 for x in a['model_bindings']:
  require(x['schema_ref'].split('/')[-1] in defs,'Unresolved model schema');refs([x['owner']],typed['components'],x['id'])
 require({x['schema_ref'].split('/')[-1] for x in a['model_bindings']}==set(defs),'Model trace coverage missing')
 for t in a['tasks']:
  refs([t['component_id']],typed['components'],t['id']);refs([t['milestone_id']],typed['milestones'],t['id']);refs(t['depends_on'],typed['tasks'],t['id']);refs(t['input_contract_ids'],typed['contracts'],t['id']);refs(t['acceptance_test_ids'],tests,t['id']);refs(t['required_schema_defs'],set(defs),t['id'])
  require(bool(t['deliverables']) and bool(t['negative_cases']),t['id']+' incomplete task')
 for m in a['milestones']:
  refs(m['task_ids'],typed['tasks'],m['id'])
  require(set(m['task_ids'])=={x['id'] for x in a['tasks'] if x['milestone_id']==m['id']},'Milestone task ownership mismatch')
  for q in m['conditional_dependencies']:
   refs([q['if_required_capability']],typed['capabilities'],m['id']);refs([q['requires_milestone']],typed['milestones'],m['id'])
 conditional_edges=copy.deepcopy(a['milestones'])
 for m in conditional_edges:m['depends_on']+=list({q['requires_milestone'] for q in m['conditional_dependencies']})
 dag(conditional_edges,'conditional milestones')
 for x in a['open_risks']:
  refs([x['owner']],typed['components'],x['id']);refs(x['blocking_refs'],set(ids),x['id'])
 for r in a['agent_protocol']['roles']:
  refs([r['principal']],typed['principals'],r['id']);refs(r['allowed_operations'],typed['contracts'],r['id'])
 for key in ['components','tasks','milestones','gates']:dag(a[key],key)
 for sm in a['state_machines']:
  ss=set(sm['states']);require(len(ss)==len(sm['states']),'Duplicate state')
  refs([sm['initial']]+sm['terminal_states'],ss,sm['id'])
  seen=set()
  for t in sm['transitions']:
   refs([t['from'],t['to']],ss,sm['id']);require(t['from'] not in sm['terminal_states'],'Terminal state has outgoing transition')
   k=(t['from'],t['event']);require(k not in seen,'Nondeterministic transition '+str(k));seen.add(k)
  reachable={sm['initial']}
  for _ in ss:
   reachable|={t['to'] for t in sm['transitions'] if t['from'] in reachable}
  require(reachable==ss,sm['id']+' unreachable states '+str(ss-reachable))
 guard_paths={x['path'] for x in a['state_guard_bindings']}
 for sm in a['state_machines']:
  for tr in sm['transitions']:
   for guard in tr['conditions']:require(guard['field'] in guard_paths,'Undefined state guard '+guard['field'])
 for t in a['tables']:
  cols={x['name'] for x in t['columns']};refs(t['primary_key'],cols,t['id'])
  for keys in t['unique_keys']+t['indexes']:refs(keys,cols,t['id'])
 for p in a['resource_profiles']:
  require(p['host_reserve_mib']+p['lab_parent_max_mib']==p['host_nominal_mib'],p['id']+' host budget sum')
  require(p['supervisor_max_mib']+p['collector_max_mib']+p['heavy_pool_max_mib']+p['slack_mib']==p['lab_parent_max_mib'],p['id']+' parent budget sum')
  require(p['worker_max_mib']*p['max_heavy_workers']<=p['heavy_pool_max_mib'],p['id']+' worker budget')
  require(0<p['lab_parent_high_mib']<p['lab_parent_max_mib'],p['id']+' high/max invalid')
 require(sum(a['operations']['storage']['allocations_gib'].values())==a['operations']['storage']['example_lab_gib'],'Disk allocation sum')
 for s in a['baseline_sections']:
  require(bool(s['requirement_ids']),s['id']+' no inherited requirement coverage');refs(s['requirement_ids'],typed['requirements'],s['id']);refs(s['component_ids'],typed['components'],s['id']);refs(s['human_refs'],typed['human_sections'],s['id'])
 require(typed['baseline_sections']=={f'V10-{i:02d}' for i in range(1,28)}|{'V10-A','V10-B','V10-C'},'Baseline domain coverage')
 for r in a['original_requirements']:refs(r['requirement_ids'],typed['requirements'],r['id'])
 for c in a['components']:
  require(any(t['component_id']==c['id'] for t in a['tasks']),c['id']+' missing task')
  require(any(c['id'] in r['component_ids'] for r in a['requirements']),c['id']+' missing requirement')
  for out in c['provides_contracts']:require(ids[out]['owner']==c['id'],c['id']+' contract owner mismatch')
 symbols={x['name']:x['type'] for x in a['invariant_symbols']}
 def expr(e):
  if 'var' in e:require(e['var'] in symbols,'Undefined predicate symbol');return symbols[e['var']]
  if 'literal' in e:return 'BOOL' if isinstance(e['literal'],bool) else 'INTEGER' if isinstance(e['literal'],int) else 'UNKNOWN'
  op=e['op'];args=e['args'];types=[expr(y) for y in args]
  require(len(args)==2 if op in ['eq','ne','lt','le','gt','ge','sub','div'] else len(args)==1 if op=='not' else len(args)>=2,'Expression arity')
  if op in ['and','or','not']:require(all(t=='BOOL' for t in types),'Boolean expression types')
  return 'BOOL' if op in ['eq','ne','lt','le','gt','ge','and','or','not'] else types[0]
 for x in a['invariants']:require(expr(x['expression'])=='BOOL','Invariant nonboolean')
 baseline=root/a['meta']['baseline_file'];require(hashlib.sha256(baseline.read_bytes()).hexdigest()==a['meta']['baseline_sha256'],'Original baseline modified')
 require(set(a['fixture_suite']['fixture_ids'])=={x['id'] for x in fixtures['fixtures']},'Fixture catalog mismatch')
 for f in fixtures['fixtures']:
  refs(f['baseline_refs'],typed['baseline_sections'],f['id']);refs([f['human_ref']],typed['human_sections'],f['id'])
 # Resolve every local runtime $ref, without network fetching.
 def schema_refs(x):
  if isinstance(x,dict):
   if '$ref' in x:
    rr=x['$ref'];require(rr.startswith('#/$defs/'),'Unexpected remote runtime schema ref');require(rr.split('/')[-1] in defs,'Unresolved runtime schema ref')
   for y in x.values():schema_refs(y)
  elif isinstance(x,list):
   for y in x:schema_refs(y)
 schema_refs(runtime)
 exs=examples if examples is not None else {e['path']:load(root/e['path']) for e in load(root/'examples/index.json')['examples']}
 operators={x['id']:x for x in a['operators']}
 for e in load(root/'examples/index.json')['examples']:
  value=exs[e['path']];name=e['schema_ref'].split('/')[-1];check_schema(value,{**runtime,'$ref':'#/$defs/'+name},e['path'])
  if name=='RuntimeLock' and value['status']=='VERIFIED':require(bool(value['native_spike_report_hash']) and bool(value['nautilus_git_commit']),'Unproved verified runtime lock')
  if name=='StrategySpec':
   refs(value['required_capabilities'],typed['capabilities'],'example policy')
   ns=value['feature_dag'];nsids={n['node_id'] for n in ns};require(len(nsids)==len(ns),'Duplicate DAG node')
   order={};stateful=0
   for n in ns:
    require(n['operator_id'] in operators,'Unknown operator');op=operators[n['operator_id']]
    require(len(n['inputs'])==len(op['input_types']),'Operator arity')
    for k,expected_type in zip(n['inputs'],op['input_types']):require(k in order and order[k]==expected_type,'Feature input type/order')
    require(n['output_type']==op['output_type'],'Output type mismatch');check_schema(n['params'],op['params_schema'],'operator params '+n['node_id']);stateful+=int(op['stateful']);order[n['node_id']]=n['output_type']
   require(stateful<=value['max_stateful_operators']<=32,'Stateful node limit')
   for k in ['long_condition_node','short_condition_node']:require(order.get(value['entry_policy'][k])=='BOOL','Entry node not boolean')
   for k in ['long_exit_node','short_exit_node']:require(order.get(value['exit_policy'][k])=='BOOL','Exit node not boolean')
   require(order.get(value['exit_policy']['stop_sigma_node'])=='F64','Stop sigma type')
   require(value['max_holding_ns']==value['exit_policy']['max_holding_ns'],'Policy holding limit mismatch')
   if e['runnable']:require('0'*64 not in json.dumps(value),'Unbound hash in runnable example')
 if check_human:
  human=(root/a['meta']['human_document']).read_text()
  for s in a['human_sections']:require(f'<a id="{s["anchor"]}"></a>' in human,'Missing human anchor '+s['id'])
  for key in CATALOGS:
   if key=='human_sections':continue
   for x in a[key]:
    require(x['id'] in human,'Missing human object trace '+x['id'])
    if 'human_object_anchor' in x:require(f'<a id="{x["human_object_anchor"]}"></a>' in human,'Missing object anchor '+x['id'])
  for f in fixtures['fixtures']:require(f['id'] in human,'Missing fixture human trace')
 # Arithmetic checks validate goldens only, not simulator execution.
 require(Decimal('1000')+Decimal('10')-Decimal('0.31')-Decimal('0.5')+Decimal('5')==Decimal('1014.19'),'F23 golden arithmetic')
 require(Decimal('5.1')-Decimal('100')*Decimal('0.002')==Decimal('4.9'),'F12 golden arithmetic')
 alpha=Fraction(1,20)/2**9;m=8;ceil=lambda x:(x.numerator+x.denominator-1)//x.denominator
 require(max(10000,ceil(20*m/alpha))-1==1638399,'F28 MC budget golden')
 require(48+96==a['example_experiment']['warmup_bars'],'Example warmup golden')
 require(a['example_experiment']['grid']['base_variants']+a['example_experiment']['grid']['stability_variants']==16,'Variant budget consistency')
 for scenario in next(f for f in fixtures['fixtures'] if f['id']=='F28')['scenarios'][:2]:
  spec=scenario['steps'][0]['arguments'];ps=[Fraction(x) for x in spec['p_upper']];alpha=Fraction(spec['alpha']);alive=True;actual=[]
  for i,p in enumerate(ps):
   alive=alive and p<=alpha/(len(ps)-i);actual.append(alive)
  require(actual==scenario['expected'][0]['expected'],'Holm golden inconsistent')
 hashscenario=next(f for f in fixtures['fixtures'] if f['id']=='F21')['scenarios'][0]
 vector=hashscenario['steps'][0]['arguments']['value'];canonical=json.dumps(vector,separators=(',',':'),sort_keys=True,ensure_ascii=False)
 require(hashlib.sha256(canonical.encode()).hexdigest()==hashscenario['expected'][1]['expected'],'JCS ASCII golden hash mismatch')
 tv=type_vectors(runtime)
 return {'schema_documents':len(schemas),'runtime_models':len(defs),'catalog_objects':len(ids),'components':len(a['components']),'requirements':len(a['requirements']),'contracts':len(a['contracts']),'tasks':len(a['tasks']),'fixtures_defined':len(fixtures['fixtures']),'baseline_domains':len(a['baseline_sections']),'invariants':len(a['invariants']),'golden_arithmetic_checks':7,'ascii_canonical_hash_vectors':1,'runtime_type_vectors':tv,'runtime_conformance_executed':False,'benchmarks_executed':False}

def negatives(root):
 a=load(root/'spec/architecture.json');fx=load(root/'fixtures/conformance.json');examples={e['path']:load(root/e['path']) for e in load(root/'examples/index.json')['examples']};results=[]
 cases=[]
 def case(name,fn):cases.append((name,fn))
 case('unknown_architecture_field',lambda b,f,e:b.update({'unexpected':True}))
 case('bad_version',lambda b,f,e:b['meta'].update({'version':'9.0.0'}))
 case('unresolved_component',lambda b,f,e:b['requirements'][0]['component_ids'].append('C-MISSING'))
 case('component_dependency_cycle',lambda b,f,e:b['components'][0]['depends_on'].append('C-LABD'))
 case('task_dependency_cycle',lambda b,f,e:b['tasks'][0]['depends_on'].append('TASK-32'))
 case('missing_schema_fragment',lambda b,f,e:b['contracts'][0].update({'input_schema':'runtime.schema.json#/$defs/Missing'}))
 case('invalid_state_transition',lambda b,f,e:b['state_machines'][0]['transitions'][0].update({'to':'UNKNOWN'}))
 case('missing_baseline_coverage',lambda b,f,e:b['baseline_sections'][0].update({'requirement_ids':[]}))
 case('missing_human_anchor',lambda b,f,e:b['human_sections'][0].update({'anchor':'missing-anchor'}))
 case('duplicate_catalog_id',lambda b,f,e:b['tasks'][1].update({'id':'TASK-01'}))
 case('bad_memory_budget',lambda b,f,e:b['resource_profiles'][0].update({'slack_mib':641}))
 case('operator_wrong_arity',lambda b,f,e:e['examples/strategy_mr.json']['feature_dag'][1]['inputs'].append('N-MID'))
 case('operator_unknown_parameter',lambda b,f,e:e['examples/strategy_mr.json']['feature_dag'][2]['params'].update({'centered':True}))
 case('wire_integer_wrong_type',lambda b,f,e:e['examples/strategy_mr.json'].update({'max_holding_ns':21600000000000}))
 case('fixture_unknown_operation',lambda b,f,e:f['fixtures'][0]['scenarios'][0]['steps'][0].update({'operation':'MAGIC'}))
 case('undefined_invariant_symbol',lambda b,f,e:b['invariants'][0].update({'expression':{'op':'eq','args':[{'var':'undefined.symbol'},{'literal':1}]}}))
 case('integer_string_overflow',lambda b,f,e:(e['examples/strategy_mr.json'].update({'max_holding_ns':str(2**64)}),e['examples/strategy_mr.json']['exit_policy'].update({'max_holding_ns':str(2**64)})))
 case('negative_zero_decimal',lambda b,f,e:e['examples/strategy_mr.json']['feature_dag'][8]['params'].update({'value':'-0'}))
 case('non_nfc_text',lambda b,f,e:b['human_sections'][0].update({'title':'e\u0301'}))
 case('quantile_probability_outside_domain',lambda b,f,e:e['examples/strategy_mr.json']['feature_dag'].append({'node_id':'N-INVALID-QUANTILE','operator_id':'OP-QUANTILE','inputs':['N-LOG'],'params':{'window':2,'probability':'1.1','approximate':False},'output_type':'F64','fit_scope':'NONE','null_policy':'PROPAGATE_ABSTAIN','clock':'BAR_CLOSE_DELIVERY'}))
 for name,mutate in cases:
  b=copy.deepcopy(a);f=copy.deepcopy(fx);e=copy.deepcopy(examples);mutate(b,f,e)
  try:validate(root,b,f,e);results.append({'case':name,'rejected':False})
  except (ValueError,jsonschema.ValidationError,jsonschema.SchemaError) as er:results.append({'case':name,'rejected':True,'reason':str(er)[:160]})
 require(all(x['rejected'] for x in results),'Validator accepted negative mutants: '+str([x['case'] for x in results if not x['rejected']]))
 return results

def main():
 p=argparse.ArgumentParser();p.add_argument('--root',default=str(Path(__file__).resolve().parents[1]));p.add_argument('--skip-negative',action='store_true');p.add_argument('--report',help='Write a fresh report outside the delivered package if desired');args=p.parse_args();root=Path(args.root).resolve()
 try:
  counts=validate(root);neg=[] if args.skip_negative else negatives(root)
  validated_paths=[root/'spec/architecture.json',root/'fixtures/conformance.json',root/'QUANT_EDGE_LAB_IMPLEMENTATION_MONOGRAPH_v1_1.md',root/'validation/validate_spec.py']+sorted((root/'schemas').glob('*.json'))+sorted((root/'examples').glob('*.json'))
  validated_files=[{'path':str(q.relative_to(root)),'sha256':hashlib.sha256(q.read_bytes()).hexdigest(),'size_bytes':q.stat().st_size} for q in validated_paths]
  report={'status':'PASS_SPECIFICATION_ONLY','generated_utc':datetime.now(timezone.utc).isoformat(),'validator_version':'1.1.0','python_version':sys.version.split()[0],'jsonschema_version':__import__('importlib.metadata',fromlist=['version']).version('jsonschema'),'checks':['DRAFT2020_12_META_SCHEMAS','ARCHITECTURE_STRUCTURE','RUNTIME_EXAMPLES','FIXTURE_GRAMMAR','ID_RESOLUTION','SCHEMA_FRAGMENTS','DEPENDENCY_DAGS','STATE_MACHINES','RESOURCE_SUMS','OPERATOR_TYPES','BASELINE_SHA256','BIDIRECTIONAL_HUMAN_TRACE','GOLDEN_ARITHMETIC'],'counts':counts,'negative_mutants':neg,'validated_files':validated_files,'claims_not_made':['NATIVE_API_COMPATIBILITY','RUNTIME_CONFORMANCE','STATISTICAL_FWER_CALIBRATION','PERFORMANCE','FORWARD_EDGE','PROFITABILITY']}
  report_path=Path(args.report) if args.report else root/'validation/validation_report.json'
  report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'status':report['status'],'counts':counts,'negative_mutants_rejected':len(neg)}))
 except Exception as er:
  print('SPECIFICATION_VALIDATION_FAILED: '+str(er),file=sys.stderr);sys.exit(1)
if __name__=='__main__':main()
