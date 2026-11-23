import base64
from flask import Blueprint,render_template,jsonify,request
from app import store
from app.config import TITLE,MODULES,SHORT
from app.web import api,payload,_context_lookup
from app.workflow import run,load
bp=Blueprint('m8_declare',__name__)

def _state():
    state=store.latest()
    if state is None:state=run(load('dirty.csv'),{},context_lookup=_context_lookup)
    return state

def _computed(state):
    source=base64.b64decode(state['source_b64']) if state.get('source_b64') else load('dirty.csv')
    parameters=state.get('manifest',{}).get('parameters',{}) or {}
    return run(source,parameters,8,context_lookup=_context_lookup)


def overview():
    """Declarable view of the currently stored batch.

    The filed record is the exportable result only when it matches the plan
    computed for the stored batch. After a rule change the plan (and its digest)
    changes, so the stored plan is shown as 待提交 and the old filed record is
    only offered as history; the screen can therefore never contradict the CSV.
    """
    state=_state();computed=_computed(state)
    plan=computed['declaration'];period=plan['period']
    filings=store.declarations(period=period)
    live=[d for d in filings if d['status'] in ('待复核','已生效')]
    # A filing corresponds to this batch when it was produced from the same
    # payroll batch under the same rule signature. Matching on the plan digest
    # would miss it: the recompute after filing already sees the prior filing
    # and legitimately marks those items 已申报.
    current=next((d for d in live if d['source_run']==state.get('run_id')
        and d['rule_signature']==plan['rule_signature']),None)
    declaration=dict(plan);declaration['filed']=False;declaration['declaration_id']=None;declaration['declaration_status']='待提交'
    export_target=None
    if current:
        declaration=store.get_declaration(current['id']);declaration['filed']=True
        export_target=current
    return {'state':state,'computed':computed,'declaration':declaration,'filings':filings,
        'export_target':export_target,'history':store.history(),'parameters':state.get('manifest',{}).get('parameters',{})}


@bp.get('/m8_declare')
def screen():
    view=overview()
    return render_template('m8_declare.html',title=TITLE,short=SHORT,modules=MODULES,current=8,
        data={'declaration':view['declaration'],'rows':view['state'].get('rows',[]),
            'quarantine':view['state'].get('quarantine',[]),'run_id':view['state'].get('run_id'),
            'manifest':view['state'].get('manifest',{}),'business':view['state'].get('business'),
            'reconcile':view['state'].get('reconcile',{})},
        run=view['state'],history=view['history'],declarations=view['filings'],
        export_target=view['export_target'])

@bp.route('/api/m8_declare',methods=['GET','POST'])
def preview():return api(8)

@bp.get('/api/m8_declare/plan')
def plan_view():
    """The declarable plan for the stored batch (filed record when already filed)."""
    view=overview()
    return jsonify({'declaration':view['declaration'],'filed':view['declaration']['filed'],
        'export_target':view['export_target']})
