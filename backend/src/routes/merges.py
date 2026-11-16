from fastapi import APIRouter,Depends
from src.config.database import get_db
from src.utils.security import current_user,editor
from src.schemas.business import MergeInput
from src.services import merges
router=APIRouter()
@router.get('/merges')
def listing(db=Depends(get_db),user=Depends(current_user)):return [merges.serialize(db,m) for m in merges.listing(db)]
@router.post('/merges/preview')
def preview(data:MergeInput,db=Depends(get_db),user=Depends(current_user)):return merges.preview(db,data.source_id,data.target_id)
@router.post('/merges',status_code=201)
def create(data:MergeInput,db=Depends(get_db),user=Depends(editor)):return merges.serialize(db,merges.create(db,data,user))
@router.get('/merges/{id}')
def detail(id:int,db=Depends(get_db),user=Depends(current_user)):return merges.serialize(db,merges.get(db,id))
@router.post('/merges/{id}/retry')
def retry(id:int,db=Depends(get_db),user=Depends(editor)):return merges.serialize(db,merges.retry(db,id))
