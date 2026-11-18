from datetime import datetime,timezone
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .models import Layout
from .mount_models import ModelMount,RunMount,MountOccupancy
from .run_models import TunnelRun
from .calibration_service import locked
from .services import pack,audit
RHO_KG_M3=1.225
GRAVITY_M_S2=9.80665

def create(s,d,u):
    l=locked(s,Layout,d.layout_id)
    if l.status!='signed':raise HTTPException(409,'仅已审签布局可登记翼模装夹')
    x=ModelMount(**d.model_dump());s.add(x);s.flush();audit(s,u,'登记翼模装夹 '+x.code);return view(s,x)
def revise(s,id,d,u):
    x=locked(s,ModelMount,id)
    if x.state!='draft':raise HTTPException(409,'已验收装夹不能修改，请新建装夹记录')
    if x.code!=d.code or x.layout_id!=d.layout_id:raise HTTPException(409,'装夹编号和布局不可修改')
    for k,v in d.model_dump().items():setattr(x,k,v)
    audit(s,u,'修订翼模装夹 '+x.code);return view(s,x)
def accept(s,id,u):
    x=locked(s,ModelMount,id)
    if x.state!='draft':raise HTTPException(409,'装夹已验收，不能重复签署')
    if not 20<=x.torque_nm<=80:raise HTTPException(409,'固定扭矩须在20–80N·m内')
    x.state='accepted';x.accepted_by=u.email;audit(s,u,'主管验收装夹 '+x.code);return view(s,x)

# ---------- 独占占用 ----------
# 计划关联 RunMount 只表达“这场试验准备用哪套装夹”，不占用装夹。
# 只有负责人开车才写入 MountOccupancy(state=active)，完结才释放；
# active_mount_id 唯一索引保证同一装夹最多一条活动占用（数据库层兜底）。

def _active(s,mount_id,lock=False):
    q=select(MountOccupancy).where(MountOccupancy.active_mount_id==mount_id,MountOccupancy.state=='active')
    if lock:q=q.with_for_update().execution_options(populate_existing=True)
    return s.scalar(q)
def _conflicts(s,mount_id,lock=False):
    q=select(MountOccupancy).where(MountOccupancy.mount_id==mount_id,MountOccupancy.state=='conflict').order_by(MountOccupancy.id)
    if lock:q=q.with_for_update().execution_options(populate_existing=True)
    return list(s.scalars(q))
def _own(s,run_id):
    return s.scalar(select(MountOccupancy).where(MountOccupancy.run_id==run_id).with_for_update().execution_options(populate_existing=True))
def link(s,d,u,replace=False):
    r=locked(s,TunnelRun,d.run_id);m=locked(s,ModelMount,d.mount_id)
    if r.state!='planned':raise HTTPException(409,'只能为未开车计划关联装夹')
    if m.state!='accepted' or m.layout_id!=r.layout_id:raise HTTPException(409,'须匹配本试验布局且已验收的装夹')
    x=s.scalar(select(RunMount).where(RunMount.run_id==r.id).with_for_update())
    if x and not replace:raise HTTPException(409,'试验已关联装夹，可在开车前修订关联')
    if not x:x=RunMount(**d.model_dump());s.add(x)
    else:x.mount_id=m.id
    s.flush();audit(s,u,'关联试验装夹 '+str(r.id)+'→'+m.code);return pack(x)
def _run_code(s,run_id):
    r=s.get(TunnelRun,run_id);return r.code if r else str(run_id)
def view(s,m):
    conflicts=_conflicts(s,m.id)
    if conflicts:
        codes=[_run_code(s,o.run_id) for o in conflicts]
        return pack(m)|{'occupancy_state':'conflict','occupied_by':None,'conflict_runs':codes,'conflict_reason':conflicts[0].conflict_reason or ('装夹 '+m.code+' 历史运行冲突：'+'/'.join(codes))}
    active=_active(s,m.id)
    if active:
        return pack(m)|{'occupancy_state':'occupied','occupied_by':{'run_id':active.run_id,'run_code':_run_code(s,active.run_id),'acquired_at':active.acquired_at},'conflict_runs':[],'conflict_reason':None}
    return pack(m)|{'occupancy_state':'idle','occupied_by':None,'conflict_runs':[],'conflict_reason':None}
def listing(s):return [view(s,m) for m in s.scalars(select(ModelMount).order_by(ModelMount.id.desc()))]
def occupancy_listing(s):
    return [pack(o)|{'run_code':_run_code(s,o.run_id),'mount_code':(s.get(ModelMount,o.mount_id).code if s.get(ModelMount,o.mount_id) else str(o.mount_id))} for o in s.scalars(select(MountOccupancy).order_by(MountOccupancy.id))]

def snapshot_mount_id(r):
    # 历史运行只认开车快照里的装夹依据；快照缺失或格式不符时不猜测。
    snap=r.snapshot if isinstance(r.snapshot,dict) else None
    if not snap:return None
    mounting=snap.get('mounting')
    if not isinstance(mounting,dict):return None
    mount=mounting.get('mount')
    if not isinstance(mount,dict):return None
    try:return int(mount.get('id'))
    except (TypeError,ValueError):return None

def acquire(s,r,now,actor=None):
    link=s.scalar(select(RunMount).where(RunMount.run_id==r.id).with_for_update())
    if not link:raise HTTPException(409,'新开车须先关联已验收翼模装夹')
    m=locked(s,ModelMount,link.mount_id)
    own=_own(s,r.id)
    if own and own.state=='active' and own.mount_id==m.id:
        return {'state':'active','mount_id':m.id,'mount_code':m.code,'acquired_at':own.acquired_at}
    conflicts=_conflicts(s,m.id,lock=True)
    if conflicts:
        codes=[_run_code(s,o.run_id) for o in conflicts]
        raise HTTPException(409,'装夹 '+m.code+' 存在历史冲突占用（'+'/'.join(codes)+'），须逐场完结后才能开车')
    active=_active(s,m.id,lock=True)
    if active:
        if active.run_id==r.id:
            return {'state':'active','mount_id':m.id,'mount_code':m.code,'acquired_at':active.acquired_at}
        raise HTTPException(409,'装夹 '+m.code+' 正被试验 '+_run_code(s,active.run_id)+' 独占使用')
    if own:
        # 仅复用本试验原有的占用行，绝不新增第二条运行记录。
        own.mount_id=m.id;own.active_mount_id=m.id;own.state='active';own.acquired_at=now;own.released_at=None;own.conflict_reason=None
    else:
        s.add(MountOccupancy(run_id=r.id,mount_id=m.id,active_mount_id=m.id,state='active',acquired_at=now))
    try:s.flush()
    except IntegrityError:
        # 唯一索引兜底：并发下另一事务已抢先取得同一装夹。整单回滚即未开车。
        s.rollback()
        raise HTTPException(409,'装夹 '+m.code+' 已被其他试验独占，本次开车未放行')
    if actor:audit(s,actor,'取得装夹独占占用 '+m.code+' → '+r.code)
    return {'state':'active','mount_id':m.id,'mount_code':m.code,'acquired_at':now}

def _refresh_conflict_reason(s,mount_id):
    """某场冲突完结后，把仍冲突的记录说明刷新为剩余试验，避免列已完结场次。"""
    remaining=_conflicts(s,mount_id,lock=True)
    if not remaining:return
    m=s.get(ModelMount,mount_id)
    codes=sorted(_run_code(s,o.run_id) for o in remaining)
    reason='装夹 '+(m.code if m else str(mount_id))+' 有 '+str(len(codes))+' 场历史运行同时占用：'+'、'.join(codes)+'；须逐场完结，全部完结前封锁新开车'
    for o in remaining:o.conflict_reason=reason

def release(s,r,now,actor=None):
    own=_own(s,r.id)
    if not own:return None
    if own.state=='released':return pack(own)
    mount_id=own.mount_id
    own.state='released';own.active_mount_id=None;own.released_at=now
    s.flush()
    _refresh_conflict_reason(s,mount_id)
    s.flush()
    if actor:audit(s,actor,'释放装夹独占占用 '+str(mount_id)+' ← '+r.code)
    return pack(own)

def readiness(s,r):
    link=s.scalar(select(RunMount).where(RunMount.run_id==r.id).with_for_update())
    if not link:raise HTTPException(409,'新开车须先关联已验收翼模装夹')
    m=locked(s,ModelMount,link.mount_id)
    if m.state!='accepted' or m.layout_id!=r.layout_id:raise HTTPException(409,'翼模装夹与本试验布局不匹配或未验收')
    q=.5*RHO_KG_M3*r.speed**2;wind=q*m.area_m2;weight=m.mass_kg*GRAVITY_M_S2;load=wind+weight
    if load>m.load_limit:raise HTTPException(409,'动压面积载荷加静重超过装夹核准载荷N')
    return {'mount':pack(m),'rho_kg_m3':RHO_KG_M3,'gravity_m_s2':GRAVITY_M_S2,'dynamic_pressure_pa':round(q,6),'wind_load_n':round(wind,6),'weight_n':round(weight,6),'estimated_load_n':round(load,6),'formula':'0.5 * rho_kg_m3 * speed_m_s^2 * area_m2 + mass_kg * gravity_m_s2'}

# ---------- 历史升级 ----------
def reconcile_history(s,now=None):
    """把升级前已在运行的试验按开车快照接入占用簿。

    - 快照带 mounting.mount.id 的运行中试验：独占接入。
    - 同一装夹已有多场历史运行：全部列 conflict 并封锁新开车，逐场完结后解除。
    - 快照缺装夹依据：不猜配，不建立占用。
    可重复执行；已有占用行只更新状态，不重复插入。
    """
    now=now or datetime.now(timezone.utc).isoformat()
    running=list(s.scalars(select(TunnelRun).where(TunnelRun.state=='running').with_for_update()))
    running_ids={r.id for r in running}
    rows=list(s.scalars(select(MountOccupancy).with_for_update()))
    by_run={o.run_id:o for o in rows}
    for o in rows:
        if o.state in ('active','conflict') and o.run_id not in running_ids:
            o.state='released';o.active_mount_id=None;o.released_at=o.released_at or now
    groups={}
    for r in running:
        mid=snapshot_mount_id(r)
        if mid is None:continue
        m=s.get(ModelMount,mid)
        if not m:continue
        groups.setdefault(m.id,[]).append(r)
    for mid,runs in groups.items():
        m=s.get(ModelMount,mid)
        # 已经处于历史冲突的运行，即使只剩一场，也必须保留 conflict 直到本场完结，
        # 不能在重启接管时被当作单场历史运行转为 active 而提前解除封锁。
        if len(runs)==1 and (by_run.get(runs[0].id) is None or by_run[runs[0].id].state!='conflict'):
            r=runs[0];o=by_run.get(r.id)
            if o:
                o.mount_id=m.id;o.state='active';o.active_mount_id=m.id;o.conflict_reason=None;o.released_at=None
                o.acquired_at=o.acquired_at or r.started_at or now
            else:
                s.add(MountOccupancy(run_id=r.id,mount_id=m.id,active_mount_id=m.id,state='active',acquired_at=r.started_at or now))
        else:
            codes=sorted(r.code for r in runs)
            reason='装夹 '+m.code+' 有 '+str(len(runs))+' 场历史运行同时占用：'+'、'.join(codes)+'；须逐场完结，全部完结前封锁新开车'
            for r in runs:
                o=by_run.get(r.id)
                if o:
                    o.mount_id=m.id;o.state='conflict';o.active_mount_id=None;o.released_at=None
                    o.conflict_reason=reason
                else:
                    o=MountOccupancy(run_id=r.id,mount_id=m.id,active_mount_id=None,state='conflict',acquired_at=r.started_at or now,conflict_reason=reason)
                    s.add(o);by_run[r.id]=o
    s.flush()
