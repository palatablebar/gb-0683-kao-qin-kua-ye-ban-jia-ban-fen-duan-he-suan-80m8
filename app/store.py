import sqlite3,json,os
from datetime import datetime,timezone
from pathlib import Path
from app.errors import DomainError

ACTIVE_STATUSES=('待复核','已生效')
DECLARATION_STATUSES=('待复核','已生效','退回','作废')
DECLARATION_DECISIONS=('通过','退回')

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10,isolation_level=None);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS run_revisions (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, body TEXT NOT NULL,
        version INTEGER NOT NULL, decision TEXT NOT NULL, replaced_at TEXT NOT NULL, reason TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS declarations (
        id TEXT PRIMARY KEY, period TEXT NOT NULL, rule_signature TEXT NOT NULL, rule_version TEXT NOT NULL,
        kind TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, body TEXT NOT NULL,
        source_run TEXT, plan_digest TEXT NOT NULL, created_at TEXT NOT NULL,
        superseded_by TEXT, supersedes TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS declaration_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, declaration_id TEXT NOT NULL,
        event TEXT NOT NULL, detail TEXT, at TEXT NOT NULL)''')
    db.execute('CREATE TABLE IF NOT EXISTS generation_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, event TEXT NOT NULL, at TEXT NOT NULL)')
    return db

def _now():
    return datetime.now(timezone.utc).isoformat()

def _log(db,run_id,event,at=None):
    db.execute('INSERT INTO generation_log(run_id,event,at) VALUES (?,?,?)',(run_id,event,at or _now()))

def save(run_id, body, reason='生成批次', event=None):
    """Upsert the current batch in one explicit transaction.

    The previous body is first copied to run_revisions (append-only), then the
    current row and the "latest" pointer are updated together. An interrupted
    generation therefore never leaves a half-written batch: the prior body and
    its raw payroll/punch bytes stay recoverable from run_revisions and from the
    stored source_b64.
    """
    body=dict(body)
    for transient in ('review_version','decision'):body.pop(transient,None)
    body_json=json.dumps(body,ensure_ascii=False)
    db=connection()
    try:
        db.execute('BEGIN IMMEDIATE')
        existing=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if existing and existing['body']!=body_json:
            db.execute('INSERT INTO run_revisions(run_id,body,version,decision,replaced_at,reason) VALUES (?,?,?,?,?,?)',
                (run_id,existing['body'],existing['version'],existing['decision'],_now(),reason))
        db.execute('INSERT INTO runs(id,body) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',(run_id,body_json))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
        _log(db,run_id,event or ('保存批次' if not existing else '更新批次'))
        db.execute('COMMIT')
    except Exception:
        try:db.execute('ROLLBACK')
        except Exception:pass
        raise
    finally:
        db.close()
    return get(run_id)

def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    body=json.loads(r['body']);body.update(run_id=r['id'],review_version=r['version'],decision=r['decision']);return body

def latest():
    with connection() as db:
        row=db.execute("SELECT value FROM meta WHERE key='latest'").fetchone()
        if row and db.execute('SELECT 1 FROM runs WHERE id=?',(row['value'],)).fetchone():return get(row['value'])
        fallback=db.execute('SELECT id FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
        if fallback:
            # Self-heal an interrupted pointer without touching batch records.
            db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(fallback['id'],))
            return get(fallback['id'])
    return None

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def revisions(run_id):
    with connection() as db:rows=db.execute('SELECT seq,version,decision,replaced_at,reason FROM run_revisions WHERE run_id=? ORDER BY seq DESC',(run_id,)).fetchall()
    return [dict(r) for r in rows]

def restore(run_id, revision=None):
    """Promote a stored batch (or one of its older revisions) to "latest".

    Records are never deleted: the current body is archived as a revision first.
    The restored body keeps its own raw source_b64, so payroll rows and punches
    stay recoverable after a failed regeneration.
    """
    version=decision=None
    if revision is None:body=get(run_id)
    else:
        with connection() as db:
            r=db.execute('SELECT * FROM run_revisions WHERE run_id=? AND seq=?',(run_id,revision)).fetchone()
            if not r:raise DomainError('NOT_FOUND','历史版本不存在',404)
            body=json.loads(r['body']);version=r['version'];decision=r['decision']
    saved=save(run_id,body,reason='恢复历史版本' if revision is not None else '恢复旧版结果',
        event='恢复旧版结果' if revision is None else f'恢复历史版本#{revision}')
    if version is not None:
        with connection() as db:
            db.execute('UPDATE runs SET version=?,decision=? WHERE id=?',(version,decision,run_id))
        saved=get(run_id)
    return saved

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
            raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)

def generation_log(run_id=None):
    with connection() as db:
        if run_id:rows=db.execute('SELECT * FROM generation_log WHERE run_id=? ORDER BY seq',(run_id,)).fetchall()
        else:rows=db.execute('SELECT * FROM generation_log ORDER BY seq').fetchall()
    return [dict(r) for r in rows]

# ---- declarations ---------------------------------------------------------

def _declaration_row(row):
    body=json.loads(row['body'])
    body.update(declaration_id=row['id'],declaration_status=row['status'],declaration_version=row['version'],
        declaration_kind=row['kind'],declaration_period=row['period'],declaration_rule_version=row['rule_version'],
        declaration_rule_signature=row['rule_signature'],declaration_plan_digest=row['plan_digest'],
        superseded_by=row['superseded_by'],supersedes=row['supersedes'],created_at=row['created_at'])
    return body

def get_declaration(declaration_id):
    with connection() as db:r=db.execute('SELECT * FROM declarations WHERE id=?',(declaration_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','申报记录不存在',404)
    return _declaration_row(r)

def declaration_prior(period, signature):
    """Return the prior-filing context for a period under the current rule.

    Only non-voided filings count as already declared. Filings made under a
    different rule signature are surfaced through rule_signature so the planner
    can force a re-check and the store can void them on submit.
    """
    with connection() as db:rows=db.execute('SELECT * FROM declarations WHERE period=? ORDER BY rowid',(period,)).fetchall()
    if not rows:return None
    active=[r for r in rows if r['status'] in ACTIVE_STATUSES]
    if not active:return None
    latest_active=active[-1]
    filed=set()
    for r in active:
        if r['rule_signature']==signature:
            filed.update(json.loads(r['body']).get('base_ids') or [])
    return {'rule_signature':latest_active['rule_signature'],'filing_count':len(active),
        'filed_ids':sorted(filed),'latest_id':latest_active['id']}

def active_declaration(period):
    with connection() as db:r=db.execute("SELECT * FROM declarations WHERE period=? AND status IN ('待复核','已生效') ORDER BY rowid DESC LIMIT 1",(period,)).fetchone()
    return _declaration_row(r) if r else None

def declaration_counts(period):
    with connection() as db:
        return {'filed':db.execute("SELECT COUNT(*) c FROM declarations WHERE period=? AND status IN ('待复核','已生效')",(period,)).fetchone()['c'],
            'voided':db.execute("SELECT COUNT(*) c FROM declarations WHERE period=? AND status='作废'",(period,)).fetchone()['c']}

def declarations(period=None,limit=50):
    query='SELECT * FROM declarations';args=[]
    if period:query+=' WHERE period=?';args.append(period)
    query+=' ORDER BY rowid DESC LIMIT ?';args.append(limit)
    with connection() as db:rows=db.execute(query,args).fetchall()
    out=[]
    for r in rows:
        body=json.loads(r['body'])
        out.append({'id':r['id'],'period':r['period'],'kind':r['kind'],'status':r['status'],'version':r['version'],
            'rule_version':r['rule_version'],'rule_signature':r['rule_signature'],'source_run':r['source_run'],
            'created_at':r['created_at'],'superseded_by':r['superseded_by'],'supersedes':r['supersedes'],
            'plan_digest':r['plan_digest'],'declared_base':body.get('totals',{}).get('declared_base'),
            'supplement_amount':body.get('totals',{}).get('supplement_amount')})
    return out

def declaration_events(declaration_id):
    with connection() as db:rows=db.execute('SELECT * FROM declaration_events WHERE declaration_id=? ORDER BY seq',(declaration_id,)).fetchall()
    return [dict(r) for r in rows]

def submit_declaration(filing_id, plan_body, plan_digest):
    """Persist a filing atomically; identical content is never declared twice.

    Guards, all inside one BEGIN IMMEDIATE transaction:
    * blocked / un-balanced plans are refused, so no partial rows leak;
    * primary key on the content-derived filing_id makes a re-run a no-op;
      a filing previously 退回 is re-opened for re-check rather than duplicated;
    * an active declaration made under another rule signature is voided and
      superseded; the new filing starts as 待复核 so it must be re-checked.
    """
    period=plan_body['period'];signature=plan_body['rule_signature'];rule=plan_body.get('rule_version')
    now=_now();db=connection()
    try:
        db.execute('BEGIN IMMEDIATE')
        # Idempotency is checked before the balance guard: re-running the same
        # batch after a filing has no new base and must return the stored record.
        existing=db.execute('SELECT * FROM declarations WHERE id=?',(filing_id,)).fetchone()
        if existing:
            if existing['status']=='退回':
                db.execute("UPDATE declarations SET status='待复核',version=version+1 WHERE id=?",(filing_id,))
                db.execute('INSERT INTO declaration_events(declaration_id,event,detail,at) VALUES (?,?,?,?)',
                    (filing_id,'退回后重新提交',json.dumps({'version':existing['version']+1,'base':json.loads(existing['body']).get('totals',{}).get('declared_base')},ensure_ascii=False),now))
                db.execute('COMMIT')
                return get_declaration(filing_id),'reopened'
            db.execute('ROLLBACK');return _declaration_row(existing),'unchanged'
        if not plan_body.get('declarable'):
            db.execute('ROLLBACK');raise DomainError('DECLARE_BLOCKED','未对平或存在阻断项，不得申报',409)
        if not plan_body.get('base_ids'):
            # A different batch that adds no new confirmed item must not create
            # an empty declaration: the period already has its active filing.
            same=db.execute("SELECT * FROM declarations WHERE period=? AND status IN ('待复核','已生效') AND rule_signature=? ORDER BY rowid DESC LIMIT 1",(period,signature)).fetchone()
            if same:
                db.execute('ROLLBACK');return _declaration_row(same),'unchanged'
        supersedes=None;status='待复核'
        other=db.execute("SELECT * FROM declarations WHERE period=? AND status IN ('待复核','已生效') AND rule_signature!=? ORDER BY rowid DESC LIMIT 1",(period,signature)).fetchone()
        if other:
            db.execute('UPDATE declarations SET status=?,superseded_by=? WHERE id=?',('作废',filing_id,other['id']))
            db.execute('INSERT INTO declaration_events(declaration_id,event,detail,at) VALUES (?,?,?,?)',
                (other['id'],'规则变更作废',json.dumps({'old_rule':other['rule_version'],'old_signature':other['rule_signature'],
                    'new_rule':rule,'new_signature':signature,'new_declaration':filing_id,
                    'old_base':json.loads(other['body']).get('totals',{}).get('declared_base')},ensure_ascii=False),now))
            supersedes=other['id']
        db.execute('INSERT INTO declarations(id,period,rule_signature,rule_version,kind,status,version,body,source_run,plan_digest,created_at,superseded_by,supersedes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (filing_id,period,signature,rule,plan_body.get('kind','初次申报'),status,1,json.dumps(plan_body,ensure_ascii=False),
             plan_body.get('source_run'),plan_digest,now,None,supersedes))
        db.execute('INSERT INTO declaration_events(declaration_id,event,detail,at) VALUES (?,?,?,?)',
            (filing_id,'提交申报',json.dumps({'period':period,'rule_version':rule,'signature':signature,
                'kind':plan_body.get('kind'),'base':plan_body['totals'].get('declared_base'),'supersedes':supersedes},ensure_ascii=False),now))
        db.execute('COMMIT')
    except Exception:
        try:db.execute('ROLLBACK')
        except Exception:pass
        raise
    finally:
        db.close()
    return get_declaration(filing_id),'created'

def review_declaration(declaration_id, version, decision):
    if decision not in DECLARATION_DECISIONS:raise DomainError('DECISION','复核状态无效')
    new_status='已生效' if decision=='通过' else '退回'
    with connection() as db:
        row=db.execute('SELECT * FROM declarations WHERE id=?',(declaration_id,)).fetchone()
        if not row:raise DomainError('NOT_FOUND','申报记录不存在',404)
        if row['status']=='作废':raise DomainError('VOID','申报已作废，须按新规则重新核对',409)
        cur=db.execute('UPDATE declarations SET version=version+1,status=? WHERE id=? AND version=?',(new_status,declaration_id,version))
        if cur.rowcount!=1:raise DomainError('STALE','复核版本已更新，请刷新',409)
        db.execute('INSERT INTO declaration_events(declaration_id,event,detail,at) VALUES (?,?,?,?)',
            (declaration_id,'复核'+new_status,json.dumps({'version':version+1,'decision':decision},ensure_ascii=False),_now()))
    return get_declaration(declaration_id)
