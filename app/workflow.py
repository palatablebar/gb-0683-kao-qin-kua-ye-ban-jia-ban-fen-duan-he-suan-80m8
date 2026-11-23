import time,importlib,json,base64
from pathlib import Path
from app import config
from app.errors import DomainError
from app.manifest import identity,manifest

PIPELINE_END=7
DECLARE_STAGE=8

def load(sample):
    if sample not in ('dirty.csv','clean.csv','legacy.csv'):raise DomainError('SAMPLE','样例名称无效')
    return (Path(__file__).resolve().parents[1]/'sample_data'/sample).read_bytes()

def run(content,parameters=None,stage=8,context_lookup=None):
    if len(content)>2_000_000:raise DomainError('SIZE','文件超过2MB',413)
    params=parameters or {}
    if not isinstance(params,dict):raise DomainError('PARAMETERS','参数必须是对象')
    start=time.monotonic();state=None
    upper=min(stage,PIPELINE_END)
    for n,entry in enumerate(config.MODULES[:PIPELINE_END],1):
        if n>upper:break
        fn=importlib.import_module('app.modules.'+entry[0]+'.service').execute
        state=fn(content,params) if n==1 else fn(state)
    state['rule_version']=config.RULE_VERSION
    # Decimal values are serialized as strings before any storage or response.
    state=json.loads(json.dumps(state,default=str,ensure_ascii=False))
    state['run_id']=identity(content,params);state['manifest']=manifest(content,params,time.monotonic()-start)
    state['source_b64']=base64.b64encode(content).decode('ascii')
    if stage>=DECLARE_STAGE:
        from app.modules.m8_declare.service import plan_declaration
        state['declaration']=plan_declaration(state,context_lookup)
    if stage>=PIPELINE_END:
        from app.modules.m7_archive.export_out import checksum
        state['archive']['sha256']=checksum(state)
    return state
