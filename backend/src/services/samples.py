from sqlalchemy import select
from fastapi import HTTPException
from src.models.entities import Sample,Process,ProcessMerge
from src.services import merges
def listing(db):return db.scalars(select(Sample).order_by(Sample.id)).all()
def save(db,data,id=None):
    # 与归并创建互斥：归并确认后新收样统一归入最终工艺（旧编号作为别名重定向）
    merges.lock_merge_state(db)
    process=db.get(Process,data.process_id)
    if not process:raise HTTPException(404,'工艺方案不存在')
    canonical=merges.canonical_of(db,process.id)
    item=None
    if id:
        item=db.scalar(select(Sample).where(Sample.id==id).with_for_update())
        if not item:raise HTTPException(404,'拉力样本不存在')
        if item.process_id!=canonical.id:
            # 样本离开仍在迁移的源工艺：计入该次归并的迁出数，保证完工核对不丢样
            active=db.scalar(select(ProcessMerge).where(ProcessMerge.source_id==item.process_id,ProcessMerge.status=='migrating'))
            if active:active.moved+=1;active.updated_at=merges.utcnow()
    payload=data.model_dump();payload['process_id']=canonical.id
    if not item:item=Sample()
    for k,v in payload.items():setattr(item,k,v)
    db.add(item);db.commit();db.refresh(item);return item
