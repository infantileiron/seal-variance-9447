import os
from datetime import datetime,timezone
from sqlalchemy import select,func,update,text
from fastapi import HTTPException
from src.config.database import engine
from src.models.entities import Process,Sample,ProcessMerge

BATCH_SIZE=int(os.getenv('MERGE_BATCH_SIZE','50'))
MERGE_LOCK_KEY=730105

def utcnow():return datetime.now(timezone.utc).isoformat()

def lock_merge_state(db):
    """串行化归并状态变更与收样归属判定：同一事务内归并创建/重试、收样重定向、参数修订、评议快照互斥，
    保证归并链解析与数量核对基于一致状态（并发收样不丢样、不混用归并前后分组）。"""
    if engine.dialect.name=='postgresql':db.execute(text(f'SELECT pg_advisory_xact_lock({MERGE_LOCK_KEY})'))

def canonical_of(db,process_id):
    """沿 merged_into 链解析到最终工艺；归并链落到最终工艺。"""
    seen=set()
    process=db.get(Process,process_id)
    while process and process.merged_into_id is not None and process.id not in seen:
        seen.add(process.id)
        process=db.get(Process,process.merged_into_id)
    return process

def group_process_ids(db,canonical_id):
    """归并到同一最终工艺的全部编号（含自身与各级别名），用于评议取样与样本去向追踪。"""
    ids=[]
    for p in db.scalars(select(Process).order_by(Process.id)).all():
        c=canonical_of(db,p.id)
        if c and c.id==canonical_id:ids.append(p.id)
    return ids

def _sample_count(db,process_id):
    return db.scalar(select(func.count()).select_from(Sample).where(Sample.process_id==process_id)) or 0

def _flagged_count(db,process_id):
    return db.scalar(select(func.count()).select_from(Sample).where(Sample.process_id==process_id,Sample.flagged.is_(True))) or 0

def _params(p):
    return {'temperature':p.temperature,'dwell':p.dwell,'pressure':p.pressure}

PARAM_LABELS=(('temperature','封合温度 ℃'),('dwell','保压时间 s'),('pressure','封合压力 MPa'))

def preview(db,source_id,target_id):
    """归并预览：参数差异与样本影响，不改动任何数据。"""
    if source_id==target_id:raise HTTPException(422,'源工艺与目标工艺不能相同')
    source=db.get(Process,source_id);target=db.get(Process,target_id)
    if not source or not target:raise HTTPException(404,'工艺方案不存在')
    resolved=canonical_of(db,target.id)
    problems=[]
    if source.merged_into_id is not None:
        into=canonical_of(db,source.id)
        problems.append(f'源工艺 {source.code} 已归并为 {into.code} 的别名，不能重复归并')
    if db.scalar(select(ProcessMerge.id).where(ProcessMerge.source_id==source.id,ProcessMerge.status=='migrating')):
        problems.append(f'源工艺 {source.code} 已有进行中的归并')
    if resolved.id==source.id:
        problems.append(f'循环归并被拒绝：{target.code} 已沿归并链落到 {source.code}')
    diffs=[{'field':f,'label':label,'source':_params(source)[f],'target':_params(resolved)[f],'changed':abs(_params(source)[f]-_params(resolved)[f])>1e-9} for f,label in PARAM_LABELS]
    source_samples=_sample_count(db,source.id);target_samples=_sample_count(db,resolved.id)
    return {'ok':not problems,'problems':problems,
        'source':{'id':source.id,'code':source.code,'name':source.name,'samples':source_samples,'flagged':_flagged_count(db,source.id)},
        'target':{'id':resolved.id,'code':resolved.code,'name':resolved.name,'samples':target_samples,'flagged':_flagged_count(db,resolved.id)},
        'target_is_alias':resolved.id!=target.id,
        'diffs':diffs,
        'after':{'total_samples':source_samples+target_samples,'alias_code':source.code,'canonical_code':resolved.code}}

def create(db,data,user):
    """发起归并：旧编号立即成为别名，新收样归入目标，历史样本由后台分批迁移。"""
    if data.source_id==data.target_id:raise HTTPException(422,'源工艺与目标工艺不能相同')
    lock_merge_state(db)
    source=db.get(Process,data.source_id);target=db.get(Process,data.target_id)
    if not source or not target:raise HTTPException(404,'工艺方案不存在')
    if source.merged_into_id is not None:
        into=canonical_of(db,source.id)
        raise HTTPException(409,f'源工艺 {source.code} 已归并为 {into.code} 的别名，不能重复归并')
    if db.scalar(select(ProcessMerge.id).where(ProcessMerge.source_id==source.id,ProcessMerge.status=='migrating')):
        raise HTTPException(409,f'源工艺 {source.code} 已有进行中的归并，请等待完成')
    resolved=canonical_of(db,target.id)
    if resolved.id==source.id:raise HTTPException(409,f'循环归并被拒绝：{target.code} 已沿归并链落到 {source.code}')
    now=utcnow()
    merge=ProcessMerge(source_id=source.id,target_id=resolved.id,status='migrating',
        expected_total=_sample_count(db,source.id),moved=0,batch_size=BATCH_SIZE,
        actor=user.email,created_at=now,updated_at=now)
    db.add(merge)
    source.merged_into_id=resolved.id
    db.commit();db.refresh(merge)
    return merge

def listing(db):
    return db.scalars(select(ProcessMerge).order_by(ProcessMerge.id.desc())).all()

def get(db,id):
    merge=db.get(ProcessMerge,id)
    if not merge:raise HTTPException(404,'归并任务不存在')
    return merge

def retry(db,id):
    """失败归并的重试：剩余样本从未完成处继续，已迁移样本不会重复移动。"""
    lock_merge_state(db)
    merge=get(db,id)
    if merge.status!='failed':raise HTTPException(409,'仅数量核对失败的归并可重试')
    source=db.get(Process,merge.source_id)
    if not source or source.merged_into_id is None:raise HTTPException(409,'源工艺别名关系已不存在，无法重试')
    merge.expected_total=_sample_count(db,merge.source_id)
    merge.moved=0;merge.status='migrating';merge.error='';merge.updated_at=utcnow()
    db.commit();db.refresh(merge)
    return merge

def migrate_batch(db,merge):
    """迁移一批历史样本到归并链最终工艺；返回本批移动条数。批量提交保证重试不重复移动。"""
    canonical=canonical_of(db,merge.target_id)
    ids=db.scalars(select(Sample.id).where(Sample.process_id==merge.source_id).order_by(Sample.id)
        .limit(merge.batch_size).with_for_update(skip_locked=True)).all()
    if ids:
        db.execute(update(Sample).where(Sample.id.in_(ids)).values(process_id=canonical.id))
        merge.moved+=len(ids);merge.updated_at=utcnow()
        db.commit()
        return len(ids)
    if _sample_count(db,merge.source_id)==0:
        if merge.moved==merge.expected_total:
            merge.status='completed';merge.finished_at=utcnow();merge.error=''
        else:
            merge.status='failed'
            merge.error=f'数量核对不一致：源工艺应迁出 {merge.expected_total} 条，实迁出 {merge.moved} 条'
        merge.updated_at=utcnow();db.commit()
    return 0

def serialize(db,merge):
    source=db.get(Process,merge.source_id);target=db.get(Process,merge.target_id)
    canonical=canonical_of(db,merge.target_id)
    return {'id':merge.id,'source_id':merge.source_id,'target_id':merge.target_id,
        'source_code':source.code if source else '?','target_code':target.code if target else '?',
        'canonical_id':canonical.id if canonical else None,'canonical_code':canonical.code if canonical else '?',
        'status':merge.status,'expected_total':merge.expected_total,'moved':merge.moved,
        'remaining':_sample_count(db,merge.source_id),'batch_size':merge.batch_size,
        'actor':merge.actor,'error':merge.error,
        'created_at':merge.created_at,'updated_at':merge.updated_at,'finished_at':merge.finished_at}
