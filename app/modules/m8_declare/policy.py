import hashlib,json,re
from decimal import Decimal,ROUND_HALF_UP
from app.errors import DomainError

SCHEMA='social-insurance-base/v1'
POLICY_CODE='INFLIGHT_EXCLUDED_SUPPLEMENT'
POLICY_TEXT=('在途工资（待核项目）不计入本次缴费基数，登记为后续补报候补；本期只申报已确认工资，'
             '待核项目复核确认后按后续补报入账，既不漏报跨月加班，也不延后当期社保权益。')
PERIOD_PATTERN=re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')

BASE='计入本次申报基数'
FILED='已申报（不重复计入）'
SUPPLEMENT='后续补报候补'
OTHER='非本期'

def money(value):
    return str(Decimal(str(value)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))

def stable(value,length=16):
    payload=json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:length]

def month_of(value):
    text=str(value or '').strip()
    return text[:7] if PERIOD_PATTERN.match(text[:7]) else None

def rule_signature(state):
    """Rule version plus calculation parameters: both belong to the payroll rule."""
    parameters=(state.get('parameters') or {})
    return stable({'rule_version':state.get('rule_version') or state.get('manifest',{}).get('rule_version'),
        'parameters':parameters},24)

def _assign_ids(items):
    seen={}
    for item in items:
        previous=seen.get(item['item_id'],0);seen[item['item_id']]=previous+1
        if previous:item['item_id']=f"{item['item_id']}#{previous+1}"
    return items

def confirmed_items(state):
    items=[]
    for calc in state.get('calculations',[]):
        shift_day=str(calc.get('shift_day',''))
        calendar_day=str(calc.get('calendar_day',''))
        identity={'employee':str(calc.get('employee','')),'shift_day':shift_day,'calendar_day':calendar_day,
            'start':str(calc.get('start','')),'end':str(calc.get('end','')),'code':str(calc.get('code','')),
            'minutes':int(calc.get('minutes',0) or 0)}
        items.append({'employee':identity['employee'],'wage_period':month_of(shift_day) or month_of(calendar_day),
            'shift_day':shift_day,'calendar_day':calendar_day,'start':identity['start'],'end':identity['end'],
            'code':identity['code'],'minutes':identity['minutes'],'rate':str(calc.get('rate','')),
            'amount':money(calc.get('amount','0')),'stage':'已确认','source':'薪资对账计算证据',
            'line':calc.get('line'),'item_id':stable(identity),'note':''})
    items.sort(key=lambda r:(r['employee'],r['wage_period'] or '',r['shift_day'],r['start'],r['end'],r['code'],r['minutes']))
    return _assign_ids(items)

def pending_items(state):
    items=[]
    for entry in state.get('quarantine',[]):
        raw=entry.get('raw') if isinstance(entry.get('raw'),dict) else {}
        shift_day=str(raw.get('shift_day',''))
        identity={'line':entry.get('line'),'employee':raw.get('employee'),'shift_day':shift_day,
            'punch':raw.get('punch'),'at':raw.get('at'),'hourly_rate':raw.get('hourly_rate'),'code':entry.get('code')}
        items.append({'employee':str(raw.get('employee') or '未标注'),'wage_period':month_of(shift_day),
            'shift_day':shift_day,'calendar_day':'','start':'','end':'','code':str(entry.get('code','')),
            'reason':str(entry.get('code','')),'minutes':None,'amount':None,'rate':str(raw.get('hourly_rate','')),
            'punch':str(raw.get('punch','')),'at':str(raw.get('at','')),'hourly_rate':str(raw.get('hourly_rate','')),
            'stage':'待核','source':'打卡采信隔离证据','line':entry.get('line'),'item_id':stable(identity),
            'note':''})
    items.sort(key=lambda r:(r['employee'],r['wage_period'] or '',r['shift_day'],r['code'],str(r['line'])))
    return _assign_ids(items)

def resolve_period(state,confirmed,pending):
    parameters=state.get('parameters',{}) or {}
    requested=parameters.get('declare_period')
    if requested is not None:
        if not isinstance(requested,str) or not PERIOD_PATTERN.match(requested):
            raise DomainError('PERIOD','申报期间必须为YYYY-MM')
        return requested,'parameters.declare_period'
    months=sorted({r['wage_period'] for r in confirmed if r['wage_period']})
    if months:
        # A batch may span several months; file the latest one and list the rest
        # as 非本期 rather than silently mixing bases of different periods.
        latest=months[-1]
        source='班次归属日所在月' if len(months)==1 else f"多工资期间{','.join(months)}，取{latest}申报，其余列为非本期"
        return latest,source
    for row in pending:
        if row['wage_period']:return row['wage_period'],'待核记录班次归属日所在月'
    raise DomainError('PERIOD','无法确定申报期间，请传入parameters.declare_period')

def kind_of(prior,rule_changed,base_rows):
    if rule_changed:return '规则变更重新申报'
    if not prior:return '初次申报'
    if base_rows:return '后续补报'
    return '无新增已确认项目'

def plan(state,prior=None):
    """Produce the declarable result: one row per confirmed calc and per pending item.

    prior describes declarations already filed for the period:
    {'rule_signature','filed_ids':[...],'filing_count':n}
    Items filed under the *same* rule signature are marked 已申报 and excluded
    from the new base, so re-runs and later batches never double-declare.
    When the rule signature changed, prior filings are ignored: the whole
    confirmed payroll re-enters the base and the earlier filing is voided by
    the store at submit time.
    """
    confirmed=confirmed_items(state)
    pending=pending_items(state)
    signature=rule_signature(state)
    period,period_source=resolve_period(state,confirmed,pending)
    prior=prior or None
    rule_changed=bool(prior) and prior.get('rule_signature')!=signature
    filed=set(prior.get('filed_ids') or []) if prior and not rule_changed else set()
    mapping=[]
    for item in confirmed:
        if item['wage_period']!=period:
            item.update(in_base=False,in_period=False,disposition=OTHER,base_amount='0.00',
                note='工资期间'+str(item['wage_period'])+'，不属本期')
        elif item['item_id'] in filed:
            item.update(in_base=False,in_period=True,disposition=FILED,base_amount='0.00',
                note='本期先前申报已计入，不重复申报')
        else:
            item.update(in_base=True,in_period=True,disposition=BASE,base_amount=item['amount'],
                note='待核项目复核确认，按后续补报入账' if prior and not rule_changed else '')
        mapping.append(item)
    for item in pending:
        item.update(in_base=False,in_period=None,disposition=SUPPLEMENT,base_amount='0.00',
            note='在途工资不计入本次基数，复核确认后补报')
        mapping.append(item)
    base_rows=[r for r in mapping if r['disposition']==BASE]
    filed_rows=[r for r in mapping if r['disposition']==FILED]
    candidate_rows=[r for r in mapping if r['disposition']==SUPPLEMENT]
    other_rows=[r for r in mapping if r['disposition']==OTHER]
    base_total=sum((Decimal(r['base_amount']) for r in base_rows),Decimal(0))
    filed_total=sum((Decimal(r['amount']) for r in filed_rows),Decimal(0))
    cumulative=base_total+filed_total
    employees={}
    def bucket(employee):
        return employees.setdefault(employee,{'employee':employee,'wage_period':period,
            'confirmed_items':0,'declared_base':Decimal(0),'filed_base':Decimal(0),'pending_items':0,
            'pending_codes':[],'supplement_items':0,'supplement_amount':Decimal(0)})
    for row in base_rows:
        b=bucket(row['employee']);b['confirmed_items']+=1;b['declared_base']+=Decimal(row['base_amount'])
    for row in filed_rows:
        b=bucket(row['employee']);b['filed_base']+=Decimal(row['amount'])
    for row in candidate_rows:
        b=bucket(row['employee']);b['supplement_items']+=1
        if row['code'] and row['code'] not in b['pending_codes']:b['pending_codes'].append(row['code'])
        if row['stage']=='已确认':b['supplement_amount']+=Decimal(row['amount'])
        else:b['pending_items']+=1
    for b in employees.values():
        b['declared_base']=money(b['declared_base']);b['filed_base']=money(b['filed_base']);b['supplement_amount']=money(b['supplement_amount'])
    evidence=len(state.get('calculations',[]))+len(state.get('quarantine',[]))
    reconcile=state.get('reconcile',{}) or {}
    blockers=[]
    if not reconcile.get('balanced'):
        blockers.append('对账未平：输入≠采纳+隔离+空行+重复表头+完全重复')
    if len(mapping)!=evidence:
        blockers.append('申报映射未覆盖全部计算证据与待核证据')
    if len({r['item_id'] for r in mapping})!=len(mapping):
        blockers.append('申报项目存在重复标识')
    kind=kind_of(prior,rule_changed,base_rows)
    base_ids=[r['item_id'] for r in base_rows]
    plan_body={'schema':SCHEMA,'period':period,'period_source':period_source,
        'rule_version':state.get('rule_version') or state.get('manifest',{}).get('rule_version'),
        'rule_signature':signature,'source_run':state.get('run_id'),
        'source_sha256':state.get('manifest',{}).get('sha256'),
        'kind':kind,'rule_changed':rule_changed,'recheck_required':rule_changed,
        'policy':{'code':POLICY_CODE,'text':POLICY_TEXT,'in_flight_treatment':'excluded_from_current_base',
            'supplement':'后续补报候补','blocks_declaration':False},
        'prior_filings':{'count':prior.get('filing_count'),'latest_id':prior.get('latest_id'),'rule_signature':prior.get('rule_signature'),
            'filed_items':len(filed_rows)} if prior else None,
        'declarable':not blockers,'blockers':blockers,'ready':bool(reconcile.get('balanced')),
        'nothing_new':not base_rows,
        'mapping':mapping,'employees':sorted(employees.values(),key=lambda e:e['employee']),
        'totals':{'mapping_items':len(mapping),'evidence_items':evidence,
            'confirmed_items':len(base_rows),'filed_items':len(filed_rows),
            'supplement_items':len(candidate_rows),'other_period_items':len(other_rows),
            'declared_base':money(base_total),'filed_base':money(filed_total),
            'cumulative_base':money(cumulative),
            'supplement_amount':money(sum((Decimal(r['amount']) for r in candidate_rows if r['stage']=='已确认'),Decimal(0))),
            'pending_items':len(pending),'minutes':sum((r['minutes'] or 0) for r in base_rows),
            'unit':'CNY','balanced':len(mapping)==evidence and not blockers},
        'reconcile':{k:reconcile.get(k) for k in ('input','accepted','isolated','blank','headers','duplicates','accounted','balanced')},
        'base_ids':base_ids}
    return plan_body

DIGEST_FIELDS_EXCLUDED=('plan_digest','filing_id')

def digest(plan_body):
    payload={k:v for k,v in plan_body.items() if k not in DIGEST_FIELDS_EXCLUDED}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
