import base64,csv,io,json
import pytest
from app import store
from app.errors import DomainError
from app.workflow import load,run
from app.modules.m8_declare.policy import plan,digest,stable
from app.modules.m8_declare.export_out import declaration_csv,check
from app.modules.m8_declare.handler import overview

def prior_lookup(period,signature):return store.declaration_prior(period,signature)

@pytest.fixture
def db(tmp_path,monkeypatch):
    # Direct (non-client) tests need an isolated store; client tests must not
    # request this fixture because it would repoint DB_PATH under the client.
    monkeypatch.setenv('DB_PATH',str(tmp_path/'records.db'))
    return tmp_path

def declarable_state(raw,parameters=None):
    return run(raw,parameters or {},8,context_lookup=prior_lookup)

def test_in_flight_items_excluded_from_base(raw,db):
    state=declarable_state(raw)
    declaration=state['declaration']
    assert declaration['totals']['declared_base']=='3960.00'
    assert declaration['totals']['pending_items']==4
    assert all(r['stage']!='待核' for r in declaration['mapping'] if r['disposition']=='计入本次申报基数')

def test_every_item_maps_to_a_stage_and_disposition(raw,db):
    state=declarable_state(raw)
    declaration=state['declaration']
    assert len(declaration['mapping'])==len(state['calculations'])+len(state['quarantine'])
    for row in declaration['mapping']:
        assert row['stage'] in ('已确认','待核')
        assert row['disposition'] in ('计入本次申报基数','已申报（不重复计入）','后续补报候补','非本期')
        assert row['employee']
        assert row['code']
    assert all(r['disposition']=='后续补报候补' for r in declaration['mapping'] if r['stage']=='待核')

def test_pending_items_never_carry_an_amount(raw,db):
    declaration=declarable_state(raw)['declaration']
    pending=[r for r in declaration['mapping'] if r['stage']=='待核']
    assert all(r['amount'] is None and r['base_amount']=='0.00' for r in pending)
    assert declaration['totals']['supplement_amount']=='0.00'

def test_blocked_without_balance(raw,db):
    state=run(raw,{},8,context_lookup=prior_lookup)
    broken=dict(state,reconcile=dict(state['reconcile'],balanced=False))
    declarations=plan(broken)
    assert declarations['declarable'] is False
    assert any('对账未平' in b for b in declarations['blockers'])

def test_plan_is_deterministic(raw,db):
    a=declarable_state(raw)['declaration'];b=declarable_state(raw)['declaration']
    assert a['plan_digest']==b['plan_digest'] and a['filing_id']==b['filing_id']
    assert digest(a)==a['plan_digest']

def test_review_blocks_export_and_submission(client):
    state=client.post('/api/runs',json={}).get_json()
    bad=dict(state,declaration=dict(state['declaration'],declarable=False))
    # tamper via the store so the gate is exercised end to end
    store.save(state['run_id'],bad,reason='人为破坏')
    assert client.get('/api/runs/'+state['run_id']+'/export.csv').status_code==409
    record=client.post('/api/declarations',json={'run_id':state['run_id']})
    assert record.status_code==409 and record.get_json()['code']=='DECLARE_BLOCKED'

def test_first_filing_and_rerun_idempotent(client):
    first=client.post('/api/declarations',json={}).get_json()
    assert first['outcome']=='created' and first['declaration']['declaration_status']=='待复核'
    second=client.post('/api/declarations',json={}).get_json()
    assert second['outcome']=='unchanged'
    assert second['declaration']['declaration_id']==first['declaration']['declaration_id']
    assert len(store.declarations())==1

def test_supplement_after_late_confirmation(client):
    raw=load('dirty.csv').decode('utf-8-sig')
    in_only='00201,E020,2026-10-31,1,IN,2026-10-31T22:00:00+08:00,22:00,06:00,02:00,03:00,20,220,跨月加班\n'
    out_only='00202,E020,2026-10-31,1,OUT,2026-11-01T06:00:00+08:00,22:00,06:00,02:00,03:00,20,0,跨月加班\n'
    first=client.post('/api/runs',json={'csv':raw}).get_json()
    assert first['declaration']['totals']['declared_base']=='3960.00'
    filed=client.post('/api/declarations',json={'run_id':first['run_id']}).get_json()
    assert filed['declaration']['totals']['declared_base']=='3960.00'
    pending=client.post('/api/runs',json={'csv':raw+in_only}).get_json()
    assert pending['declaration']['totals']['pending_items']==5
    confirmed=client.post('/api/runs',json={'csv':raw+in_only+out_only}).get_json()
    totals=confirmed['declaration']['totals']
    assert totals['declared_base']=='280.00' and totals['filed_base']=='3960.00'
    assert totals['cumulative_base']=='4240.00'
    supplement=client.post('/api/declarations',json={'run_id':confirmed['run_id']}).get_json()
    assert supplement['declaration']['declaration_kind']=='后续补报'
    assert supplement['declaration']['totals']['declared_base']=='280.00'

def test_rule_change_voids_previous_filing(client,monkeypatch):
    from app import config
    first=client.post('/api/runs',json={}).get_json()
    filed=client.post('/api/declarations',json={'run_id':first['run_id']}).get_json()
    old=filed['declaration']['declaration_id']
    monkeypatch.setattr(config,'RULE_VERSION','2026.12.01-v9')
    changed=client.post('/api/runs',json={}).get_json()
    assert changed['declaration']['rule_changed'] is True
    assert changed['declaration']['totals']['declared_base']=='3960.00'
    refiled=client.post('/api/declarations',json={'run_id':changed['run_id']}).get_json()
    assert refiled['outcome']=='created'
    old_record=store.get_declaration(old)
    assert old_record['declaration_status']=='作废'
    assert old_record['superseded_by']==refiled['declaration']['declaration_id']
    assert refiled['declaration']['declaration_status']=='待复核'
    assert old_record['declaration_id']!=refiled['declaration']['declaration_id']
    assert store.declaration_events(old)[-1]['event']=='规则变更作废'

def test_void_filing_cannot_be_reviewed_or_exported(client,monkeypatch):
    from app import config
    first=client.post('/api/runs',json={}).get_json()
    filed=client.post('/api/declarations',json={'run_id':first['run_id']}).get_json()
    old=filed['declaration']['declaration_id']
    monkeypatch.setattr(config,'RULE_VERSION','2026.12.01-v9')
    changed=client.post('/api/runs',json={}).get_json()
    client.post('/api/declarations',json={'run_id':changed['run_id']})
    assert client.post(f'/api/declarations/{old}/review',json={'version':1,'decision':'通过'}).status_code==409
    assert client.get(f'/api/declarations/{old}/export.csv').status_code==409

def test_declaration_review_version_conflict(client):
    filed=client.post('/api/declarations',json={}).get_json()
    did=filed['declaration']['declaration_id']
    reviewed=client.post(f'/api/declarations/{did}/review',json={'version':1,'decision':'通过'}).get_json()
    assert reviewed['declaration_status']=='已生效'
    stale=client.post(f'/api/declarations/{did}/review',json={'version':1,'decision':'退回'})
    assert stale.status_code==409

def test_page_and_export_agree(client):
    page=client.get('/m8_declare')
    assert page.status_code==200
    text=page.data.decode()
    filed=client.post('/api/declarations',json={}).get_json()['declaration']
    export=client.get(f"/api/declarations/{filed['declaration_id']}/export.csv")
    assert export.status_code==200
    rows=list(csv.reader(io.StringIO(export.data.decode('utf-8-sig'))))
    items=[r for r in rows if r[0]=='申报项']
    base=sum(float(r[9]) for r in items if r[10]=='计入本次申报基数')
    assert len(items)==len(filed['mapping'])
    assert round(base,2)==float(filed['totals']['declared_base'])
    assert check(filed)==[]
    for row in filed['mapping']:
        assert row['disposition'] in text

def test_submit_rejects_unbalanced_stored_batch(client):
    state=client.post('/api/runs',json={}).get_json()
    broken=dict(state,declaration=dict(state['declaration'],declarable=False,blockers=['对账未平：输入≠采纳+隔离+空行+重复表头+完全重复']))
    client.post('/api/runs',json={})
    store.save(state['run_id'],broken)
    response=client.post('/api/declarations',json={'run_id':state['run_id']})
    assert response.status_code==409

def test_export_check_catches_wrong_total(raw,db):
    record=declarable_state(raw)['declaration']
    bad=dict(record,totals=dict(record['totals'],declared_base='1.00'))
    assert '申报项合计与申报基数不符' in check(bad)

def test_source_recoverable_after_recompute(client):
    state=client.post('/api/runs',json={}).get_json()
    source=client.get('/api/runs/'+state['run_id']+'/source.csv')
    assert source.status_code==200 and 'id,employee' in source.data.decode('utf-8-sig')

def test_interrupted_regeneration_restores_previous_result(client,monkeypatch):
    good=client.post('/api/runs',json={}).get_json()
    store.save(good['run_id'],good)
    # a regeneration that crashes after computing must not overwrite the batch
    original=run
    def boom(*args,**kwargs):
        state=original(*args,**kwargs)
        raise RuntimeError('generation interrupted')
    monkeypatch.setattr('app.main.run',boom)
    # Under TESTING the exception propagates to the test instead of a 500 page.
    with pytest.raises(RuntimeError):client.post('/api/runs',json={'csv':load('clean.csv').decode('utf-8-sig')})
    monkeypatch.setattr('app.main.run',original)
    restored=store.latest()
    assert restored['run_id']==good['run_id']
    assert restored['declaration']['totals']['declared_base']=='3960.00'
    assert base64.b64decode(restored['source_b64']).decode('utf-8-sig').startswith('id,employee')

def test_recomputation_keeps_prior_record_visible(client):
    a=client.post('/api/runs',json={}).get_json()
    b=client.post('/api/runs',json={'csv':load('clean.csv').decode('utf-8-sig')}).get_json()
    assert store.get(a['run_id'])['run_id']==a['run_id']
    assert len(store.history())==2
    assert store.latest()['run_id']==b['run_id']

def test_restore_revision_never_deletes_records(client):
    a=client.post('/api/runs',json={}).get_json()
    edited=dict(a,business=dict(a['business'],total='1.00'))
    store.save(a['run_id'],edited,reason='重算')
    revisions=store.revisions(a['run_id'])
    assert revisions
    restored=store.restore(a['run_id'],revisions[0]['seq'])
    assert restored['business']['total']=='3960.00'
    assert len(store.revisions(a['run_id']))>=2

def test_clean_batch_declares_all_confirmed(db):
    state=run(load('clean.csv'),{},8,context_lookup=prior_lookup)
    declaration=state['declaration']
    assert declaration['kind']=='初次申报'
    assert declaration['totals']['pending_items']==0
    assert declaration['totals']['declared_base']=='3960.00'
    assert declaration['declarable'] is True

def test_bundle_carries_declarable_result(client):
    filed=client.post('/api/declarations',json={}).get_json()['declaration']
    export=client.get('/api/archive/'+filed['source_run']+'/export.csv')
    assert export.status_code==200
    lines=[l for l in export.data.decode('utf-8-sig').splitlines() if l.startswith('申报,')]
    assert len(lines)>=len(filed['mapping'])
    assert filed['totals']['declared_base'] in export.data.decode('utf-8-sig')

def test_declare_screen_reflects_rule_change(client,monkeypatch):
    from app import config
    client.post('/api/declarations',json={})
    monkeypatch.setattr(config,'RULE_VERSION','2026.12.01-v9')
    page=client.get('/m8_declare')
    assert page.status_code==200
    assert '规则变更重新申报' in page.data.decode()

def test_screen_and_export_link_same_record(client):
    filed=client.post('/api/declarations',json={}).get_json()['declaration']
    page=client.get('/m8_declare').data.decode()
    import re
    link=re.search(r'id="export"[^>]*href="([^"]+)"',page)
    assert link and filed['declaration_id'] in link.group(1)
    export=client.get(link.group(1))
    rows=list(csv.reader(io.StringIO(export.data.decode('utf-8-sig'))))
    items=[r for r in rows if r[0]=='申报项']
    assert len(items)==page.count('<td class="disp">')

def test_declare_page_without_filing_has_no_export(client):
    page=client.get('/m8_declare').data.decode()
    assert 'id="export"' not in page and '待提交' in page

def test_batch_without_new_items_does_not_create_empty_filing(client):
    filed=client.post('/api/declarations',json={}).get_json()['declaration']
    clean=client.post('/api/runs',json={'csv':load('clean.csv').decode('utf-8-sig')}).get_json()
    assert clean['declaration']['totals']['declared_base']=='0.00'
    again=client.post('/api/declarations',json={'run_id':clean['run_id']}).get_json()
    assert again['outcome']=='unchanged'
    assert again['declaration']['declaration_id']==filed['declaration_id']
    assert len(store.declarations())==1

def test_multi_month_batch_files_latest_and_defers_rest():
    from app.config import REQUIRED
    header=','.join(REQUIRED+['note'])+'\n'
    night=lambda ids,day,out: f"{ids[0]},E001,{day},1,IN,{day}T22:00:00+08:00,22:00,06:00,02:00,03:00,20,220,s\n{ids[1]},E001,{day},1,OUT,{out},22:00,06:00,02:00,03:00,20,0,s\n"
    # Wage period follows the shift's owning day, so the 09-30 shift is
    # September payroll and the 10-31 shift is October payroll.
    data=(header+night(('1','2'),'2026-09-30','2026-10-01T06:00:00+08:00')+night(('3','4'),'2026-10-31','2026-11-01T06:00:00+08:00')).encode()
    declaration=run(data,{},8)['declaration']
    assert declaration['period']=='2026-10'
    assert declaration['totals']['other_period_items']==3
    assert declaration['totals']['mapping_items']==declaration['totals']['evidence_items']

def test_returned_filing_not_exportable(client):
    filed=client.post('/api/declarations',json={}).get_json()['declaration']
    returned=client.post(f"/api/declarations/{filed['declaration_id']}/review",json={'version':1,'decision':'退回'}).get_json()
    assert returned['declaration_status']=='退回'
    did=filed['declaration_id']
    assert client.get(f'/api/declarations/{did}/export.csv').status_code==409
    # resubmitting the same batch re-opens the returned record for re-check
    again=client.post('/api/declarations',json={}).get_json()
    assert again['outcome']=='reopened' and again['declaration']['declaration_status']=='待复核'
    assert again['declaration']['declaration_id']==did
