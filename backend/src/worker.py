import os,time,traceback,json
from pathlib import Path
from sqlalchemy import select
from src.config.database import SessionLocal
from src.models.entities import Assessment,ProcessMerge
from src.services.assessments import queue
from src.services.statistics import anova
from src.services import merges
ARTIFACTS=Path(os.getenv('ARTIFACTS_DIR','/app/artifacts'))
ARTIFACTS.mkdir(parents=True,exist_ok=True)
with SessionLocal() as db:
    for task in db.scalars(select(Assessment).where(Assessment.status=='running')).all():task.status='queued'
    db.commit()
print('seal variance worker started',flush=True)
while True:
    Path('/tmp/worker-heartbeat').touch()
    try:
        queue.brpop('seal:assessments',timeout=2)
        with SessionLocal() as db:
            task=db.scalar(select(Assessment).where(Assessment.status=='queued').order_by(Assessment.id).with_for_update(skip_locked=True))
            if task:
                task.status='running';db.commit()
                try:
                    task.result=anova(task.snapshot);task.status='completed'
                    (ARTIFACTS/(str(task.id)+'.json')).write_text(json.dumps(task.result,ensure_ascii=False))
                except Exception as e:task.error=str(e);task.status='failed'
                db.commit();print(f'assessment={task.id} status={task.status}',flush=True)
            merge=db.scalar(select(ProcessMerge).where(ProcessMerge.status=='migrating').order_by(ProcessMerge.id).with_for_update(skip_locked=True))
            if merge:
                moved=merges.migrate_batch(db,merge)
                print(f'merge={merge.id} batch={moved} moved={merge.moved}/{merge.expected_total} status={merge.status}',flush=True)
    except Exception:traceback.print_exc();time.sleep(2)
