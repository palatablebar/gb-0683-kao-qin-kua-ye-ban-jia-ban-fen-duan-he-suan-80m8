import io,csv,json,hashlib

HEADER=['记录类型','申报期间','项次','员工','工资期间','班次归属日','日历日','分类或原因',
    '分钟','金额CNY','处理方式','规则版本','申报批次','说明']
SUMMARY=['申报清单','政策','汇总']

def rows(record):
    out=[]
    def write(*values):out.append(list(values))
    totals=record.get('totals',{}) or {}
    period=record.get('declaration_period') or record.get('period')
    rule=record.get('declaration_rule_version') or record.get('rule_version')
    filing=record.get('declaration_id') or record.get('filing_id')
    write('申报清单',period,'','','','','','','','','',rule,filing,
        '批次 '+(record.get('declaration_kind') or record.get('kind') or '')+' · 状态 '+(record.get('declaration_status') or ''))
    write('政策',period,'','','','','','','','','',rule,filing,(record.get('policy') or {}).get('text',''))
    write('对账',period,'','','','','','','','','',rule,filing,'输入/采纳/隔离/重复 {}'.format(
        (record.get('reconcile') or {})))
    write('汇总',period,'','','','','','','',totals.get('declared_base',''),'计入本次申报基数',rule,filing,
        f"已确认{totals.get('confirmed_items',0)}项/待核{totals.get('pending_items',0)}项/后续补报候补{totals.get('supplement_items',0)}项")
    write('汇总',period,'','','','','','','',totals.get('supplement_amount',''),'后续补报候补',rule,filing,'')
    write('汇总',period,'','','','','','','',totals.get('filed_base',''),'已申报（不重复计入）',rule,filing,'')
    for index,item in enumerate(record.get('mapping',[]),1):
        base=item.get('disposition')=='计入本次申报基数'
        write('申报项',period,index,item.get('employee',''),item.get('wage_period') or '',item.get('shift_day',''),
            item.get('calendar_day',''),item.get('code','') or item.get('reason',''),
            item.get('minutes') if item.get('minutes') is not None else '',
            item.get('base_amount') if base else (item.get('amount') if item.get('amount') is not None else ''),
            item.get('disposition',''),rule,filing,item.get('note',''))
    return out

def declaration_csv(record):
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(HEADER)
    for row in rows(record):writer.writerow(row)
    return '﻿'+out.getvalue()

def check(record,reconcile=None):
    """Self-check before export: header width, every mapping row, base totals."""
    body=rows(record)
    errors=[]
    if any(len(r)!=len(HEADER) for r in body):errors.append('列数与表头不一致')
    items=[r for r in body if r[0]=='申报项']
    if len(items)!=len(record.get('mapping',[])):errors.append('申报项行数与映射不一致')
    base=sum((float(r[9]) for r in items if r[10]=='计入本次申报基数'),0.0)
    if abs(base-float((record.get('totals') or {}).get('declared_base') or 0))>0.005:errors.append('申报项合计与申报基数不符')
    if record.get('declaration_status') in ('作废','退回'):errors.append('申报已'+record.get('declaration_status'))
    if reconcile and not reconcile.get('balanced'):errors.append('对账未平')
    return errors

def checksum(record):
    return hashlib.sha256(declaration_csv(record).encode('utf-8')).hexdigest()
