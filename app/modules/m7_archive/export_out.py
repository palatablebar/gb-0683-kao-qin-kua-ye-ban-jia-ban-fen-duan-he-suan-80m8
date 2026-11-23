import io,csv,json,hashlib

def bundle(state):
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(['记录类型','批次','来源行','业务ID','原因码','内容'])
    rid=state.get('run_id','preview')
    writer.writerow(['运行清单',rid,'','','',json.dumps(state.get('manifest',{}),ensure_ascii=False,sort_keys=True)])
    writer.writerow(['对账',rid,'','','',json.dumps(state.get('reconcile',{}),ensure_ascii=False,sort_keys=True)])
    for row in state.get('calculations',[]):writer.writerow(['计算',rid,row.get('line',''),row.get('id',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True)])
    for row in state['quarantine']:writer.writerow(['隔离',rid,row['line'],row.get('id',''),row['code'],json.dumps(row.get('raw',{}),ensure_ascii=False,sort_keys=True)])
    declaration=state.get('declaration') or {}
    if declaration:
        # The declarable result travels with the archive bundle, using the same
        # item rows the screen shows, so page, bundle and filing export agree.
        from app.modules.m8_declare.export_out import rows as declaration_rows
        for row in declaration_rows(declaration):
            writer.writerow(['申报',rid,row[2],row[3],row[10],json.dumps(dict(zip(
                ['记录类型','申报期间','项次','员工','工资期间','班次归属日','日历日','分类或原因','分钟','金额CNY','处理方式','规则版本','申报批次','说明'],row)),
                ensure_ascii=False,sort_keys=True)])
    return '﻿'+out.getvalue()

def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()
