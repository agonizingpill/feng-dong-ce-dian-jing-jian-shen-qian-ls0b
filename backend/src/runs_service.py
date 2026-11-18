from copy import deepcopy
from datetime import datetime,timezone
from fastapi import HTTPException
from sqlalchemy import select
from .models import Layout,Probe
from .run_models import TunnelRun
from .calibration_service import locked,current_certificate,view as certificate_view
from .services import pack,pack_layout,selected,audit

def signed_layout(s,id):
    l=locked(s,Layout,id)
    if l.status!='signed':raise HTTPException(409,'只有已审签布置可以建立或开车试验')
    return l

def create(s,d,u):
    signed_layout(s,d.layout_id);x=TunnelRun(**d.model_dump());s.add(x);s.flush();audit(s,u,'编制开车核对 '+x.code);return pack(x)
def revise(s,id,d,u):
    x=locked(s,TunnelRun,id)
    if x.state!='planned':raise HTTPException(409,'运行中或已完结试验不可修改开车条件')
    if x.layout_id!=d.layout_id or x.code!=d.code:raise HTTPException(409,'试验编号与布置关联不可变更')
    signed_layout(s,x.layout_id)
    for k,v in d.model_dump().items():setattr(x,k,v)
    audit(s,u,'修订开车核对 '+x.code);return pack(x)
def start(s,id,u):
    x=locked(s,TunnelRun,id)
    if x.state=='running':
        # 开车响应丢失后的重试：不新增占用、不重写原快照，原样返回已开车记录。
        return pack(x)
    if x.state!='planned':raise HTTPException(409,'仅计划试验可开车，不得重复启动')
    l=signed_layout(s,x.layout_id)
    if not 10<=x.speed<=50 or not -10<=x.target_angle<=20:raise HTTPException(409,'开车包线为风速10–50m/s、攻角−10至20°')
    keys=('air_path_clear','emergency_stop_tested','model_secured')
    if any(x.checklist.get(k) is not True for k in keys):raise HTTPException(409,'风道清场、急停测试与模型固定联锁须全部确认')
    ids=selected(s,l.id)
    if not ids:raise HTTPException(409,'已签布置缺少测点')
    probes=[locked(s,Probe,i) for i in ids];certificates=[]
    for p in probes:
        if not p.enabled:raise HTTPException(409,f'{p.code}已经停用，不能开车')
        certificates.append(certificate_view(s,current_certificate(s,p)))
    from .mount_service import readiness,acquire
    mounting=readiness(s,x)
    now=datetime.now(timezone.utc).isoformat()
    # 计划关联不占用；开车这一刻才取得装夹独占占用，失败则整个开车事务回滚。
    occupancy=acquire(s,x,now,u)
    x.snapshot=deepcopy({'layout':pack_layout(s,l),'probes':[pack(p) for p in probes],'certificates':certificates,'speed':x.speed,'target_angle':x.target_angle,'checklist':x.checklist,'checked_at':now,'mounting':mounting,'occupancy':occupancy})
    x.state='running';x.started_at=now;x.started_by=u.email;audit(s,u,'负责人开车 '+x.code);return pack(x)
def finish(s,id,d,u):
    x=locked(s,TunnelRun,id)
    if x.state=='completed':
        # 完结响应丢失后的重试：保留原完结时刻与说明，不重复释放、不改写原快照。
        return pack(x)
    if x.state!='running':raise HTTPException(409,'只有运行中的试验可以完结，不能重复收尾')
    now=datetime.now(timezone.utc).isoformat()
    x.state='completed';x.completed_at=now;x.result_note=d.result_note
    from .mount_service import release
    release(s,x,now,u)
    audit(s,u,'完结风洞试验 '+x.code);return pack(x)
