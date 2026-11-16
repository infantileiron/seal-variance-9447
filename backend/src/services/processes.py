from fastapi import HTTPException
from sqlalchemy import select
from src.models.entities import Process
from src.services import merges
def listing(db):return db.scalars(select(Process).order_by(Process.id)).all()
def save(db,data,id=None):
    # 与归并创建互斥；已归并的别名编号冻结，只能修订归并后的目标工艺
    merges.lock_merge_state(db)
    item=db.get(Process,id) if id else Process()
    if not item:raise HTTPException(404,'工艺方案不存在')
    if id and item.merged_into_id is not None:raise HTTPException(409,'该编号已归并为别名，请修订归并后的目标工艺')
    for k,v in data.model_dump().items():setattr(item,k,v)
    db.add(item);db.commit();db.refresh(item);return item
