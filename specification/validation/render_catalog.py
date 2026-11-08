#!/usr/bin/env python3
"""Regenerate the human cross-reference appendix from canonical architecture data; no lab implementation."""
from pathlib import Path
import json
ROOT=Path(__file__).resolve().parents[1]
A=json.loads((ROOT/'spec/architecture.json').read_text())
F=json.loads((ROOT/'fixtures/conformance.json').read_text())['fixtures']
DOC=ROOT/A['meta']['human_document'];MARK='<!-- GENERATED_CATALOG_START -->'
head=DOC.read_text().split(MARK)[0]
out=[MARK,'\n### 20.1 Bileşenler ve kontratlar\n']
def esc(x):return str(x).replace('|','\\|').replace('\n',' ')
def row(*xs):return '| '+' | '.join(esc(x) for x in xs)+' |'
def table(headers,rows):
 out.append(row(*headers));out.append(row(*(['---']*len(headers))));out.extend(row(*r) for r in rows);out.append('')
def ident(x):return f'<a id="{x.get("human_object_anchor","obj-"+x["id"].lower())}"></a>`{x["id"]}`'
def link(h):return f'[{h}](#sec-{h.split("-")[-1]})'
def ids(x):return ', '.join('`'+v+'`' for v in x) if x else '—'
def bas(x):return ids(x.get('baseline_refs',[]))
table(['Kimlik','Bileşen / uygulama','Bağımlılıklar','Üretilen modeller','Açıklama'],[(ident(x),x['title']+' · '+x['implementation'],ids(x['depends_on']),ids(x['outputs']),link(x['human_ref'])) for x in A['components']])
table(['Kontrat','İşlem / taşıma','Girdi → çıktı modeli','Sahip','Sürüm'],[(ident(x),x['method']+' '+x['path']+' · '+x['transport'],x['input_schema'].split('/')[-1]+' → '+x['output_schema'].split('/')[-1],x['owner'],x['version']) for x in A['contracts']])
out.append('EXECUTE yanıtı admission receipt’idir; nihai payload `artifact_payloads` kaydına göre mühürlenir. `API-WORKER-RESULT` completion kontratıdır. NATIVE_TYPED per-tick JSON kullanmaz.\n')
out.append('### 20.2 Karar kaydı\n')
table(['ADR','Karar','Sınıf','v1.0 dayanak','Açıklama','Kapanış testi'],[(ident(x),x['title'],x['kind'],bas(x),link(x['human_ref']),ids(x['closure_test_ids'])) for x in A['decisions']])
out.append('### 20.3 Normatif gereksinimler\n')
table(['Gereksinim','Konu','Uygulama sahibi','Kabul kanıtı','Açıklama / v1.0'],[(ident(x),x['title'],ids(x['component_ids']),ids(x['verification_ids']),link(x['human_ref'])+' · '+bas(x)) for x in A['requirements']])
out.append('Her satır MUST’tır; tam normative statement canonical JSON’daki aynı ID’dedir. İlgili Türkçe bölüm sözleşmenin gerekçesini ve algoritmasını açıklar.\n')
out.append('### 20.4 Yetki, operator ve invariant kayıtları\n')
table(['Principal','Unix kimliği','İzinli yazma alanı','Yasak alan','Açıklama'],[(ident(x),x['unix_identity'],ids(x['can_write']),ids(x['forbidden']),link(x['human_ref'])) for x in A['principals']])
table(['Rol','Principal','İzinli operation','Ürün'],[(ident(x),x['principal'],ids(x['allowed_operations']),ids(x['outputs'])) for x in A['agent_protocol']['roles']])
table(['Operator','Girdi → çıktı','State / lookback','Null davranışı'],[(ident(x),','.join(x['input_types'])+' → '+x['output_type'],str(x['stateful'])+' / '+x['lookback_rule'],x['null_behavior']) for x in A['operators']])
table(['Invariant','Kapsam / tetik','Doğrulama','Açıklama'],[(ident(x),x['scope']+' / '+x['trigger'],ids(x['verification_ids']),link(x['human_ref'])) for x in A['invariants']])
out.append('Invariant expression alanları `var/literal/op/args` AST’sidir; operand tipleri `invariant_symbols` kaydından çözülür. Bunlar paket doğrulamasında type-check edilir, runtime enforcement kabul testleri ayrıca çalıştırılır.\n')
out.append('### 20.5 Durum makineleri ve kapılar\n')
for x in A['state_machines']:
 out.append('#### '+ident(x)+' · '+x['title']+'\n')
 out.append('Başlangıç: `'+x['initial']+'`. Terminal: '+ids(x['terminal_states'])+'. Tanımsız state-changing transition reddedilir.\n')
 table(['Önce','Olay','Sonra','Guard / etki'],[(t['from'],t['event'],t['to'],json.dumps(t['conditions'],ensure_ascii=False,separators=(',',':'))+' · '+', '.join(t['effects'])) for t in x['transitions']])
table(['Kapı','Aşama','Ön koşul','Tüm koşullar','Açıklama'],[(ident(x),x['stage'],ids(x['depends_on']),'; '.join(c['metric']+' '+c['operator']+' '+str(c['value']) for c in x['all_conditions']),link(x['human_ref'])) for x in A['gates']])
out.append('### 20.6 Capability ve resource sertifikası\n')
table(['Capability','Sahip','Zorunlu fixture/test','Durum'],[(ident(x),x['owner'],ids(x['fixture_test_ids']),x['status']) for x in A['capabilities']])
table(['Profil','Parent / worker MiB','En fazla ağır iş','Durum'],[(ident(x),str(x['lab_parent_max_mib'])+' / '+str(x['worker_max_mib']),x['max_heavy_workers'],x['status']) for x in A['resource_profiles']])
out.append('### 20.7 Somut fixture kataloğu\n')
table(['Fixture','Sınanan hata / alan','Senaryo sayısı','Açıklama','Durum'],[(ident(x),x['title'],len(x['scenarios']),link(x['human_ref']),x['status']) for x in F])
out.append('Fixture girdileri ve exact expected değerler `fixtures/conformance.json` içindedir. Bu tabloda “DEFINED_NOT_EXECUTED”, native conformance’ın çalıştırılmadığını açıkça belirtir.\n')
out.append('### 20.8 Kabul testleri ve benchmarklar\n')
table(['Test','Amaç','Tür','Açıklama','Durum'],[(ident(x),x['title'],x['kind'],link(x['human_ref']),x['status']) for x in A['tests']])
table(['Benchmark','İş yükü','Hedef / correctness','Durum'],[(ident(x),x['title'],'; '.join(c['metric']+' '+c['operator']+' '+str(c['value']) for c in x['targets'])+' · '+ids(x['correctness_test_ids']),x['status']) for x in A['benchmarks']])
out.append('#### Derleme ve cache ölçüm matrisi\n')
table(['Kimlik','Değişiklik','Ölçülecek','p90 hedefi','Durum'],[(ident(x),x['change'],', '.join(x['measure']),str(x['p90_target_seconds'])+' s' if x['p90_target_seconds'] is not None else 'Ölçüm; sabit süre hedefi yok',x['status']) for x in A['build_benchmarks']])
out.append('### 20.9 Bağımsız mühendislik işleri\n')
tr_titles=['Kontrat SDK, schema ve canonical kimlik','Bağımsız altın oracle ve conformance harness','Ledger ve atomik artifact deposu','Kontrol protokolü ve dayanıklı minimum kuyruk','Ham veri edinme ve kaynak kanıtı','Normalize veri ve PIT universe','Typed feature operatorları ve politika','Native entegrasyon yapılabilirliği ve source lock','Bağımsız muhasebe checker’ı','Venue funding, margin ve liquidation','Tek native taker dikey dilim','Ön kayıt ve walk-forward dataset','Matched kontroller ve batch screening','Dependent istatistik ve frozen protokol','Evaluator principal ve tüketim otoritesi','İmzalı bilimsel evaluator','Passive queue ve composite emirler','İki bilgi dünyası ve queue çapraz denetimi','Sınırlı replay ve güvenli purge','Crash ve backup recovery','Ajan gateway ve CAS görev sahipliği','Ledger’dan türetilen kalıcı hafıza','Cgroup, disk ve ekonomik admission','Operator dashboard ve yetkili kontrol','Frozen benchmark corpus ve performans kanıtı','Tam araştırma null/injected-edge kalibrasyonu','24saat ve7gün sürekli çalışma','İlk frozen confirmation cohort','Forward kalibrasyon planı ve kanıtı','İmzalı bot export ve rollback','İsteğe bağlı tek kernel challenger','İsteğe bağlı ikinci venue/asset profili']
tr_outputs=['Go/Rust/Python type-check, hash vektörleri ve fixture DSL sözleşmesi.','F01–F35 decoder, exact oracle ve hata mutasyonları.','SQLite migration, immutable manifest, pin ve commit-fault davranışı.','Framed socket/HTTP routing, lease/fencing ve idempotency.','Checksum, raw ref, rate-limit ve bağlantı/gap kaydı.','Parquet/Arrow, bitemporal kurallar ve kalite manifest’i.','Incremental DAG ve LocalView üzerinden intent üretimi.','Gerçek pure-Rust prototip, exact lock veya gerekçeli BLOCKED_BACKEND.','Fiziki cash, FIFO attribution, journal ve residual raporu.','Tarihsel rule loader, tek funding owner ve tier/forced-close testleri.','Gerçek scoped veri → fill/hesap → ledger → bağımsız audit.','Takvim split/purge/embargo ve12varyant örnek ön kaydı.','1.000seed kontrol ortalaması, exposure ve reject raporu.','HAC/bootstrap/MC bound, IUT/Holm ve güç sınıflandırması.','Ayrı UID/socket/SQLite, preaccess anchor ve restore kilidi.','G0–G4, batch release ve contamination davranışı.','Ortak queue/likidite, partial bracket, stop ve replace.','Delayed delivery ve ortak semantikte HftBacktest karşılaştırması.','Bytebounded merge, closed-state export/purge ve açık epoch limitleri.','Consistent backup, anchor restore ve descendants quarantine.','Yetkili tool routing, bütçeli aile döngüsü ve immutable handoff.','FTS5, kaynak gösteren claim ve deterministic rescue.','RP4/RP8 enforcement, pin-aware GC ve paid-call rezervasyonu.','Minimal UI, redacted SSE, override ve halt.','B0–B5 frozen input, tekrarlar ve build/task-span raporları.','B6 tam selection süreci, FWER upper CI ve power/coverage.','LLM outage, disk, crash ve maliyet uzlaşması dahil soak.','En fazla8 aday, fresh window receipt ve bütün imzalı verdict’ler.','Ayrı ortam planları, yetkili küçük emir veri seti ve error budget.','Hash/imza, intent parity, unknown submit ve rollback tatbikatı.','Eşit semantikte tam karar maliyeti/hızı ve kaynak kanıtı.','Yeni rule/certificate paketi ve incumbent regression raporu.']
for x,title,delivery in zip(A['tasks'],tr_titles,tr_outputs):
 out.append('#### '+ident(x)+' · '+title+'\n')
 out.append(delivery+'\n')
 out.append('Sahip: `'+x['component_id']+'`; milestone: `'+x['milestone_id']+'`; bağımlılık: '+ids(x['depends_on'])+'.\n')
 out.append('Kabul: '+ids(x['acceptance_test_ids'])+'. İzinli yollar: '+', '.join('`'+p+'`' for p in x['allowed_paths'])+'.\n')
 out.append('Negatif yol ve done evidence bu task’ın canonical kaydında zorunludur; yalnız compile/happy-path teslimi kabul edilmez.\n')
out.append('### 20.10 Milestone ve açık riskler\n')
table(['Milestone','Başlık','Önce gereken','İşler','Çıkış kanıtı'],[(ident(x),x['title'],ids(x['depends_on']),ids(x['task_ids']),ids(x['exit_test_ids'])) for x in A['milestones']])
table(['Risk','Konu','Owner','Bloklanan','Kapanış testi'],[(ident(x),x['title'],x['owner'],ids(x['blocking_refs']),ids(x['closure_test_ids'])) for x in A['open_risks']])
out.append('### 20.11 Runtime model ve logical DDL index’i\n')
table(['Model kimliği','JSON Schema pointer','Sahip','Açıklama'],[(ident(x),'`schemas/'+x['schema_ref']+'`',x['owner'],link(x['human_ref'])) for x in A['model_bindings']])
table(['Tablo','Database / ad','Primary key','Mutation','Açıklama'],[(ident(x),x['database']+' / '+x['name'],ids(x['primary_key']),x['mutation'],link(x['human_ref'])) for x in A['tables']])
table(['Semantic kural','Model','Yürütülecek constraint','Test'],[(ident(x),ids(x['schema_defs']),json.dumps(x['constraint'],ensure_ascii=False,separators=(',',':')),ids(x['test_ids'])) for x in A['runtime_semantic_rules']])
out.append('### 20.12 Hata kodları\n')
table(['Kod','Retry politikası','Finansal olumsuz kanıt mı?'],[(ident(x),x['retry_policy'],'Hayır') for x in A['error_codes']])
out.append('Hata kodu karar değildir. MODEL_UNIDENTIFIABLE gibi aynı sözcüğün reason/verdict olarak kullanılabildiği yerde alan tipi anlamı belirler. OOM ve kaynak yetersizliği NO_EDGE’ye çevrilemez.\n')
out.append('### 20.13 v1.0 → v1.1 ters izleme\n')
table(['Temel bölüm','Konu','Gereksinimler','İnsan bölümleri'],[(ident(x),x['title'],ids(x['requirement_ids']),', '.join(link(h) for h in x['human_refs'])) for x in A['baseline_sections']])
table(['Orijinal gereksinim','Uygulama gereksinimleri','Açıklama'],[(ident(x),ids(x['requirement_ids']),link(x['human_ref'])) for x in A['original_requirements']])
out.append('### 20.14 Kaynak ve kanıt kataloğu\n')
table(['Kaynak','Resmi / birincil bağlantı','Statü / inceleme tarihi'],[(ident(x),'['+x['title']+']('+x['url']+')',x['evidence_status']+' / '+x['access_date']) for x in A['sources']])
out.append('V10-SRC kayıtları temel belgenin kaynak mirasıdır; tümü yeniden doğrulanmış olarak etiketlenmemiştir. EXT kayıtları bu uzantıda incelenen resmi belgelerdir; runtime testinin yerini tutmaz.\n')
DOC.write_text(head+'\n'.join(out)+'\n')
print('Human appendix synchronized:',len(out),'blocks')
