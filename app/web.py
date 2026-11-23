import base64
from flask import request,render_template,jsonify
from app.errors import DomainError
from app.config import TITLE,MODULES,SHORT
from app.workflow import run,load
from app import store

def payload():
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict):raise DomainError('PAYLOAD','请求必须是对象')
    if 'csv' in data and not isinstance(data['csv'],str):raise DomainError('CSV','CSV内容必须是文本')
    sample=data.get('sample',request.args.get('sample','dirty.csv'))
    content=data.get('csv')
    return (content.encode() if isinstance(content,str) else load(sample)),data.get('parameters',{})

def _context_lookup(period,signature):
    return store.declaration_prior(period,signature)

def api(stage):
    content,parameters=payload();return jsonify(run(content,parameters,stage,context_lookup=_context_lookup))

def _match_declaration(plan,items):
    """Find the stored declaration that this plan corresponds to.

    A plan is exportable from the screen only when a filed declaration carries
    the same plan digest. Otherwise the link points at a historical filing and
    is labelled as such, so the screen never offers a CSV that differs from the
    numbers on screen.
    """
    if not plan:return None,None
    digest=plan.get('plan_digest')
    for item in items:
        if item.get('plan_digest')==digest and item['status']!='作废':
            return item,'current'
    filed=[item for item in items if item['status']!='作废']
    return (filed[0],'historical') if filed else (None,None)

def page(stage,template):
    latest=store.history();state=store.latest()
    if state is None:state=run(load('dirty.csv'),{},context_lookup=_context_lookup)
    source=load(request.args['sample']) if 'sample' in request.args else base64.b64decode(state['source_b64']) if state.get('source_b64') else load('dirty.csv')
    data=run(source,state.get('manifest',{}).get('parameters',{}),stage,context_lookup=_context_lookup)
    declarations=[]
    export_target=None;export_kind=None
    if stage>=8 and data.get('declaration'):
        declarations=store.declarations(period=data['declaration']['period'])
        export_target,export_kind=_match_declaration(data['declaration'],declarations)
    return render_template(template,title=TITLE,short=SHORT,modules=MODULES,current=stage,data=data,run=state,
        history=latest,declarations=declarations,export_target=export_target,export_kind=export_kind)
