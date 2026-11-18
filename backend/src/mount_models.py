from sqlalchemy import String,Float,ForeignKey,CheckConstraint,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from .db import Base
class ModelMount(Base):
    __tablename__='model_mounts'
    __table_args__=(CheckConstraint('mass_kg > 0 AND area_m2 > 0 AND load_limit > 0'),CheckConstraint("state in ('draft','accepted')"))
    id:Mapped[int]=mapped_column(primary_key=True)
    code:Mapped[str]=mapped_column(String(60),unique=True)
    layout_id:Mapped[int]=mapped_column(ForeignKey('layouts.id'))
    mass_kg:Mapped[float]=mapped_column(Float)
    area_m2:Mapped[float]=mapped_column(Float)
    torque_nm:Mapped[float]=mapped_column(Float)
    load_limit:Mapped[float]=mapped_column(Float)
    state:Mapped[str]=mapped_column(String(20),default='draft')
    accepted_by:Mapped[str|None]=mapped_column(String(100),nullable=True)
class RunMount(Base):
    __tablename__='run_mounts'
    id:Mapped[int]=mapped_column(primary_key=True)
    run_id:Mapped[int]=mapped_column(ForeignKey('tunnel_runs.id'),unique=True)
    mount_id:Mapped[int]=mapped_column(ForeignKey('model_mounts.id'))
class MountOccupancy(Base):
    __tablename__='mount_occupancies'
    __table_args__=(UniqueConstraint('run_id'),UniqueConstraint('active_mount_id'),CheckConstraint("state in ('active','released','conflict')"),)
    id:Mapped[int]=mapped_column(primary_key=True)
    run_id:Mapped[int]=mapped_column(ForeignKey('tunnel_runs.id'))
    mount_id:Mapped[int]=mapped_column(ForeignKey('model_mounts.id'))
    # 活动占用时等于 mount_id，释放/冲突时为 NULL；唯一索引保证一个装夹最多一场占用。
    active_mount_id:Mapped[int|None]=mapped_column(ForeignKey('model_mounts.id'),nullable=True)
    state:Mapped[str]=mapped_column(String(20),default='active')
    acquired_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
    released_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
    conflict_reason:Mapped[str|None]=mapped_column(String(300),nullable=True)
