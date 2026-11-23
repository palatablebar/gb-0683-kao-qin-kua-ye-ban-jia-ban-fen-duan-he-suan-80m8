import base64,importlib,json,os
from flask import Flask,jsonify,request,redirect,Response
from app.config import MODULES
from app.errors import DomainError
from app.workflow import run,load
from app import store
from app.web import payload
from app.modules.m7_archive.export_out import bundle

def context_lookup(period, signature):
    return store.declaration_prior(period, signature)

def declarable(state):
    return bool(state.get('declaration',{}).get('declarable')) and bool(state.get('reconcile',{}).get('balanced'))

def create_app():
    app=Flask(__name__);app.config['MAX_CONTENT_LENGTH']=2_000_000
    for entry in MODULES:app.register_blueprint(importlib.import_module('app.modules.'+entry[0]+'.handler').bp)
    @app.errorhandler(DomainError)
    def domain_error(error):return jsonify(code=error.code,message=error.message),error.status
    @app.errorhandler(404)
    def missing(error):return jsonify(code='NOT_FOUND',message='入口不存在'),404
    @app.get('/')
    def index():return redirect('/'+MODULES[0][0])
    @app.get('/healthz')
    def health():return jsonify(status='ok')

    @app.post('/api/runs')
    def submit():
        content,params=payload()
        # Compute first, persist second: a generation failure returns an error
        # and the previously stored batch/latest pointer remain untouched.
        state=run(content,params,context_lookup=context_lookup)
        return jsonify(store.save(state['run_id'],state))

    @app.get('/api/runs')
    def list_runs():return jsonify(store.history())

    @app.get('/api/runs/<run_id>')
    def get_run(run_id):return jsonify(store.get(run_id))

    @app.get('/api/runs/<run_id>/source.csv')
    def get_source(run_id):
        # Original payroll and punch rows stay recoverable from the stored bytes.
        state=store.get(run_id)
        if not state.get('source_b64'):raise DomainError('NOT_FOUND','该批次没有保留原始文件',404)
        raw=base64.b64decode(state['source_b64'])
        return Response(raw,mimetype='text/csv; charset=utf-8',
            headers={'Content-Disposition':f'attachment; filename=source-{run_id}.csv'})

    @app.get('/api/runs/<run_id>/revisions')
    def get_revisions(run_id):return jsonify({'run_id':run_id,'revisions':store.revisions(run_id),'generation_log':store.generation_log(run_id)})

    @app.post('/api/runs/<run_id>/revisions/<int:revision>/restore')
    def restore_revision(run_id,revision):return jsonify(store.restore(run_id,revision))

    @app.post('/api/runs/<run_id>/restore')
    def restore(run_id):return jsonify(store.restore(run_id))

    @app.post('/api/runs/<run_id>/review')
    def review(run_id):
        data=request.get_json(silent=True) or {}
        if not isinstance(data.get('version'),int):raise DomainError('VERSION','请提交复核版本')
        return jsonify(store.review(run_id,data['version'],data.get('decision')))

    @app.get('/api/runs/<run_id>/export.csv')
    def download_run(run_id):
        state=store.get(run_id)
        if not declarable(state):raise DomainError('UNBALANCED','对账未平或申报未对平，不得导出申报结果',409)
        return Response(bundle(state),mimetype='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename=evidence.csv'})

    @app.post('/api/declarations')
    def file_declaration():
        # The stored batch is the filing authority. It is recomputed from the
        # stored payroll bytes only to merge the declaration history that exists
        # now; any stored plan that is blocked or un-balanced refuses to file,
        # because a screen render must never compute a plan the screen didn't show.
        data=request.get_json(silent=True) or {}
        run_id=data.get('run_id')
        state=store.get(run_id) if isinstance(run_id,str) else store.latest()
        if not state:raise DomainError('NOT_FOUND','尚无可申报批次',404)
        stored=state.get('declaration') or {}
        if not stored.get('declarable') or not state.get('reconcile',{}).get('balanced'):
            raise DomainError('DECLARE_BLOCKED','未对平或存在阻断项，不得申报',409)
        source=base64.b64decode(state.get('source_b64') or '')
        parameters=state.get('manifest',{}).get('parameters',{}) or {}
        body=run(source,parameters,context_lookup=context_lookup)
        declaration=body['declaration']
        if not declaration.get('declarable'):
            raise DomainError('DECLARE_BLOCKED','；'.join(declaration.get('blockers') or ['申报未对平']),409)
        record,outcome=store.submit_declaration(declaration['filing_id'],declaration,declaration['plan_digest'])
        return jsonify(outcome=outcome,declaration=record)

    @app.get('/api/declarations')
    def list_declarations():return jsonify(store.declarations(period=request.args.get('period')))

    @app.get('/api/declarations/<declaration_id>')
    def get_declaration(declaration_id):return jsonify(store.get_declaration(declaration_id))

    @app.get('/api/declarations/<declaration_id>/export.csv')
    def download_declaration(declaration_id):
        from app.modules.m8_declare.export_out import declaration_csv,check
        record=store.get_declaration(declaration_id)
        if record.get('declaration_status')=='作废':raise DomainError('VOID','申报已作废，不得导出',409)
        if record.get('declaration_status')=='退回':raise DomainError('RETURNED','申报已退回，须重新核对后提交',409)
        problems=check(record,record.get('reconcile'))
        if problems:raise DomainError('UNBALANCED','；'.join(problems),409)
        return Response(declaration_csv(record),mimetype='text/csv; charset=utf-8',
            headers={'Content-Disposition':f'attachment; filename=declaration-{declaration_id}.csv'})

    @app.post('/api/declarations/<declaration_id>/review')
    def review_declaration(declaration_id):
        data=request.get_json(silent=True) or {}
        if not isinstance(data.get('version'),int):raise DomainError('VERSION','请提交复核版本')
        return jsonify(store.review_declaration(declaration_id,data['version'],data.get('decision')))

    @app.get('/api/archive/<run_id>/export.csv')
    def download(run_id):return download_run(run_id)

    with app.app_context():
        if store.latest() is None:
            state=run(load('dirty.csv'),{},context_lookup=context_lookup);store.save(state['run_id'],state)
    return app

app=create_app()
if __name__=='__main__':app.run(host='0.0.0.0',port=8000)
