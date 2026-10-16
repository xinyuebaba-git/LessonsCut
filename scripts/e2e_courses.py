"""Real 1/3 end-to-end run; baseline is read only AFTER independent processing.

No fixture title, presenter or cut point is provided to recognition or rendering.
"""
import argparse
import json
import threading
import time
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import select
from lessonscut.api import create_app
from lessonscut.config import Settings
from lessonscut.store import Job,Call,Audit,encode
from lessonscut.worker import run_once
from lessonscut.media import digest


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source-dir',type=Path,required=True)
    parser.add_argument('--data-dir',type=Path,default=Path('data/e2e-20261015'))
    parser.add_argument('--retry-job',help='Retry a stopped development job after fixing the reported cause; never changes plan evidence')
    parser.add_argument('--only-job')
    parser.add_argument('--reanalyze-job',help='Create a new analysis revision after a user-approved policy change')
    args=parser.parse_args()
    root=args.data_dir.resolve();root.mkdir(parents=True,exist_ok=True)
    cfg=Settings(LESSONSCUT_DATA_DIR=root,LESSONSCUT_DATABASE_URL='sqlite:///'+str(root/'e2e.db'),LESSONSCUT_INPUT_ROOT=args.source_dir.resolve())
    cfg.prepare();app=create_app(cfg);store=app.state.store
    client=TestClient(app);client.headers['Authorization']='Bearer '+cfg.admin_token.get_secret_value()
    mapping=root/'run.json'
    if mapping.exists():state=json.loads(mapping.read_text())
    else:
        assets={}
        for n in [0,1,3]:
            r=client.post('/api/v1/assets/imports',json={'relative_path':f'{n}.mp4'})
            r.raise_for_status();assets[str(n)]=r.json()
        r=client.post('/api/v1/batches',headers={'Idempotency-Key':'real-e2e-0.2'},json={'asset_ids':[assets['1']['id'],assets['3']['id']],'intro_asset_id':assets['0']['id'],'approval_policy':'exceptions_only'})
        r.raise_for_status()
        state={'assets':assets,'jobs':dict(zip(['1','3'],r.json()['job_ids'])),'started_at':time.time()}
        mapping.write_text(json.dumps(state,ensure_ascii=False,indent=2))
    if args.retry_job:
        with store.session() as s:
            j=s.get(Job,args.retry_job)
            if not j or j.token or j.state!='NEEDS_REVIEW':raise SystemExit('job is not a stopped development exception')
            s.add(Audit(job_id=j.id,revision=j.revision,event='Development retry after cause fixed; previous reason: '+j.reason))
            j.state='QUEUED';j.reason=''
    if args.reanalyze_job:
        j=client.get('/api/v1/jobs/'+args.reanalyze_job).json()
        r=client.post('/api/v1/jobs/'+args.reanalyze_job+'/reanalyze',json={'base_revision':j['revision'],'reason':'User approved automatically freezing last teaching frame to retain complete closing speech; exclude outro speech'})
        r.raise_for_status()
    stopped=threading.Event()
    def progress():
        while not stopped.wait(20):
            with store.session() as s:
                print(json.dumps({'progress':[{'number':n,'job_id':jid,'state':s.get(Job,jid).state,'phase':s.get(Job,jid).reason} for n,jid in state['jobs'].items()]},ensure_ascii=False),flush=True)
    thread=threading.Thread(target=progress,daemon=True);thread.start()
    try:
        while True:
            if args.only_job:
                with store.session() as s:
                    target=s.get(Job,args.only_job)
                    if target.state!='QUEUED':break
                    # Keep unrelated queued test jobs out of this explicit diagnostic run.
                    other=[j for j in s.scalars(select(Job).where(Job.state=='QUEUED',Job.id!=args.only_job))]
                    for j in other:j.state='WAITING_RESOURCE'
            if not run_once(cfg,store):break
            if args.only_job:break
    finally:
        stopped.set();thread.join(timeout=1)
        if args.only_job:
            with store.session() as s:
                for j in s.scalars(select(Job).where(Job.state=='WAITING_RESOURCE')):j.state='QUEUED'
    # The historical baseline is deliberately unavailable until both pipelines have stopped.
    baseline=json.loads(Path('docs/course-video-app/examples/historical-baseline.json').read_text())
    summary=[]
    with store.session() as s:
        for n,jid in state['jobs'].items():
            j=s.get(Job,jid);result=json.loads(j.result)
            b=next(c for c in baseline['cases'] if c['case_id']==f'H{int(n):02d}')
            src=args.source_dir/f'{n}.mp4'
            summary.append({'number':int(n),'job_id':jid,'state':j.state,'reason':j.reason,
                'source_unchanged':digest(src)==state['assets'][n]['sha256'],
                'actual_source_frame_range':result.get('expected',{}).get('source_frame_range') or result.get('source_frame_range'),'historical_source_frame_range':b['source_lecture_frame_range'],
                'candidate_source_frame_range':result.get('candidate_source_frame_range'),'audio_boundary_conflicts':result.get('audio_boundary_conflicts',[]),
                'ending':result.get('plan',{}).get('ending'),'ending_review':result.get('ending_review'),
                'candidate':result.get('candidate'),'technical':result.get('technical'),'audio':result.get('audio'),
                'plan_review':result.get('plan_review'),'title_review':result.get('title_review'),'export_review':result.get('export_review'),
                'delivery_path':str(root/result['delivery_path']) if result.get('delivery_path') else None,
                'calls':[{'role':c.role,'model':c.model,'status':c.status,**json.loads(c.details)} for c in s.scalars(select(Call).where(Call.job_id==jid))]})
    (root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps({'summary':[{'number':x['number'],'state':x['state'],'reason':x['reason'],'source_range':x['actual_source_frame_range'],'delivery':x['delivery_path']} for x in summary]},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
