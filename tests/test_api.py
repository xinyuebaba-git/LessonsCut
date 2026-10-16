from pathlib import Path
from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest
from lessonscut.api import create_app
from lessonscut.config import Settings
from lessonscut.store import Asset,Job,Revision,encode


@pytest.fixture
def client(tmp_path):
    settings=Settings(_env_file=None,LESSONSCUT_ADMIN_TOKEN="test-admin",LESSONSCUT_DATABASE_URL=f"sqlite:///{tmp_path}/db.sqlite",LESSONSCUT_DATA_DIR=tmp_path/"data",LESSONSCUT_INPUT_ROOT=tmp_path/"input")
    app=create_app(settings)
    with app.state.store.session() as s:
        s.add_all([Asset(id="source",path=str(tmp_path/"source"),sha256="a"*64,name="source.mp4",kind="video"),Asset(id="intro",path=str(tmp_path/"intro"),sha256="b"*64,name="intro.mp4",kind="video")])
    c=TestClient(app);c.headers["Authorization"]="Bearer test-admin"
    yield c,app


def new_job(c):
    r=c.post('/api/v1/batches',json={"asset_ids":["source"]},headers={"Idempotency-Key":"batch-1"})
    assert r.status_code==202
    return r.json()['job_ids'][0]


def test_auth_and_redaction(client):
    c,app=client
    assert c.get('/api/v1/config',headers={"Authorization":"Bearer wrong"}).status_code==401
    d=c.get('/api/v1/config').json()
    assert 'admin_token' not in d and 'minimax_api_key' not in d


def test_idempotency_and_conflict(client):
    c,app=client;jid=new_job(c)
    r=c.post('/api/v1/batches',json={"asset_ids":["source"]},headers={"Idempotency-Key":"batch-1"})
    assert r.json()['job_ids']==[jid]
    assert c.post('/api/v1/batches',json={"asset_ids":["intro"]},headers={"Idempotency-Key":"batch-1"}).status_code==409


def test_revision_fencing_and_delivery_invalidated(client):
    c,app=client;jid=new_job(c)
    with app.state.store.session() as s:
        job=s.get(Job,jid);job.state="DELIVERED";job.result=encode({"delivery_path":"output/old.mp4","technical":{"full_decode":True},"plan_review":{"decision":"pass"},"automatic_plan":{"title":"旧标题"},"transcript":{"segments":[]},"evidence":[]})
    body={"base_revision":1,"plan":{"title":"课程", "lecture":{"start":1,"end":3},"intro_asset_id":"intro","reason":"人工核对"}}
    assert c.post(f'/api/v1/jobs/{jid}/revisions',json=body).status_code==202
    assert c.post(f'/api/v1/jobs/{jid}/revisions',json=body).status_code==409
    assert c.get(f'/api/v1/jobs/{jid}/delivery').status_code==404
    assert c.get(f'/api/v1/jobs/{jid}').json()['revision']==2
    report=c.get(f'/api/v1/jobs/{jid}/report').json()['evidence_report']
    assert not {'delivery_path','technical','plan_review','automatic_plan'}&report.keys()
    assert report['source_analysis_revision']==1 and 'transcript' in report


def test_active_revision_rejected(client):
    c,app=client;jid=new_job(c);claim=app.state.store.claim()
    assert claim[0]==jid
    body={"base_revision":1,"plan":{"title":"课程", "lecture":{"start":1,"end":3},"intro_asset_id":"intro","reason":"核对"}}
    assert c.post(f'/api/v1/jobs/{jid}/revisions',json=body).status_code==409
    assert c.post(f'/api/v1/jobs/{jid}/cancel').status_code==409


def test_path_traversal(client):
    c,app=client
    assert c.post('/api/v1/assets/imports',json={"relative_path":"../../.env"}).status_code==422


def test_claim_single_slot_and_stale_fence(client):
    c,app=client;jid=new_job(c);store=app.state.store
    claim=store.claim();assert store.claim() is None
    with pytest.raises(RuntimeError):store.transition(jid,1,"wrong-token","DELIVERED",{})
    store.transition(*claim,"NEEDS_REVIEW",{},"test")
    assert c.get(f'/api/v1/jobs/{jid}').json()['state']=='NEEDS_REVIEW'


def test_reanalysis_creates_new_revision_and_invalidates_previous_output(client):
    c,app=client;jid=new_job(c)
    with app.state.store.session() as s:
        j=s.get(Job,jid);j.state='NEEDS_REVIEW';j.result=encode({'intro_asset_id':'intro','delivery_path':'old.mp4','plan_review':{'decision':'pass'}})
    payload={'base_revision':1,'reason':'用户批准新收束规则'}
    assert c.post(f'/api/v1/jobs/{jid}/reanalyze',json=payload).json()['revision']==2
    assert c.post(f'/api/v1/jobs/{jid}/reanalyze',json=payload).status_code==409
    result=c.get(f'/api/v1/jobs/{jid}').json()
    assert result['stage']=='analyze' and result['result']=={'intro_asset_id':'intro','config_version':1}
