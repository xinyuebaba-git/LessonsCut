import json
import pytest
from pathlib import Path
from PIL import Image
from lessonscut.config import Settings
from lessonscut.store import Store,Asset,Job,Revision,encode
from lessonscut.media import run,digest,render as real_render
from lessonscut.pipeline import Pipeline
from lessonscut.worker import run_once


class FakeReviewProvider:
    fail_final=False
    def __init__(self,*args):pass
    def chat(self,role,prompt,images):
        assert role in ('recognizer','supervisor')
        if self.fail_final and role=='supervisor' and prompt.startswith('成片标题文字复核'):
            return {'decision':'fail','checks':[{'code':'title_text','decision':'fail','reason':'fixture review failure'}],'issues':['fixture failure'],'limits':'synthetic only'}
        if prompt.startswith('核对标题图片'):codes=['title_text','presenter_text','no_extra_text','not_clipped']
        elif prompt.startswith('核对标题风格'):codes=['style_match']
        elif prompt.startswith('成片片头身份复核'):codes=['intro_identity']
        elif prompt.startswith('成片标题文字复核'):codes=['title_text','presenter_text','no_extra_text','not_clipped']
        else:codes=['lecture_start','lecture_body','lecture_end','no_black_frame']
        return {'decision':'pass','checks':[{'code':c,'decision':'pass','reason':'synthetic fixture'} for c in codes],'issues':[],'limits':'synthetic samples only'}


@pytest.mark.parametrize('header_failure,tamper',[(False,False),(True,False),(True,True)])
def test_confirmed_revision_to_real_mp4_with_mock_model_review(tmp_path,monkeypatch,header_failure,tamper):
    cfg=Settings(_env_file=None,LESSONSCUT_DATA_DIR=tmp_path/'data',LESSONSCUT_INPUT_ROOT=tmp_path/'input',LESSONSCUT_DATABASE_URL=f'sqlite:///{tmp_path}/db.sqlite')
    cfg.prepare();store=Store(cfg.database_url)
    source=tmp_path/'source.mp4'
    run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=30','-f','lavfi','-i','anoisesrc=color=pink:seed=77:sample_rate=48000','-t','8','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)])
    title=tmp_path/'title.png';Image.new('RGB',(320,180),'white').save(title)
    original=digest(source)
    with store.session() as s:
        s.add_all([Asset(id='source',path=str(source),sha256=original,name='source',kind='video'),Asset(id='intro',path=str(source),sha256=original,name='intro',kind='video'),Asset(id='title',path=str(title),sha256=digest(title),name='title',kind='image')])
        s.add(Job(id='job',batch_id='batch',asset_id='source',revision=2,stage='render'))
        s.add(Revision(job_id='job',number=2,plan=encode({'title':'测试','lecture':{'start':2,'end':5},'intro_asset_id':'intro','title_asset_id':'title','reason':'fixture confirmation'})))
    def small_render(*args,**kwargs):
        return real_render(*args,**kwargs,size=(320,180))
    monkeypatch.setattr('lessonscut.pipeline.render',small_render)
    monkeypatch.setattr(FakeReviewProvider,'fail_final',header_failure)
    pipeline=Pipeline(cfg,store,FakeReviewProvider)
    assert run_once(cfg,store,pipeline)
    if header_failure:
        with store.session() as s:
            job=s.get(Job,'job');assert job.state=='NEEDS_REVIEW'
            result=json.loads(job.result);output=cfg.data_dir/result['render_path']
            job.state='QUEUED'
        monkeypatch.setattr(FakeReviewProvider,'fail_final',False)
        if tamper:output.write_bytes(output.read_bytes()[:1000])
        else:
            def no_repeat(*args):raise AssertionError('successful same-hash decode should be reused')
            monkeypatch.setattr('lessonscut.pipeline.verify_output',no_repeat)
        assert run_once(cfg,store,pipeline)
        if tamper:
            with store.session() as s:assert s.get(Job,'job').state=='NEEDS_REVIEW'
            assert not list((cfg.data_dir/'output').glob('*.mp4'))
            return
    with store.session() as s:
        job=s.get(Job,'job');result=json.loads(job.result)
        assert job.state=='DELIVERED',job.reason
        assert result['technical']['frames']==315
        assert len(result['audio'])==3
        assert (cfg.data_dir/result['delivery_path']).is_file()
        assert (cfg.data_dir/'work/job/2/project.json').is_file()
    assert digest(source)==original
    assert not run_once(cfg,store,pipeline)


def test_two_course_batch_generates_distinct_titles_with_frozen_configuration(tmp_path,monkeypatch):
    """Upload -> one batch -> two worker runs -> two real, decodable MP4 files.

    Models use synthetic fixtures; no private courses or external paid calls.
    """
    from fastapi.testclient import TestClient
    from lessonscut.api import create_app
    from lessonscut.domain import ConfirmedPlan
    cfg=Settings(_env_file=None,LESSONSCUT_ADMIN_TOKEN='test',LESSONSCUT_DATA_DIR=tmp_path/'data',LESSONSCUT_INPUT_ROOT=tmp_path/'input',LESSONSCUT_DATABASE_URL=f'sqlite:///{tmp_path}/db.sqlite')
    app=create_app(cfg);store=app.state.store
    client=TestClient(app,headers={'Authorization':'Bearer test'})
    source=tmp_path/'fixture.mp4'
    run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=30','-f','lavfi','-i','anoisesrc=color=pink:seed=77:sample_rate=48000','-t','8','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)])
    assets=[]
    for name in ('1.mp4','3.mp4','intro.mp4'):
        with source.open('rb') as f:r=client.post('/api/v1/assets/uploads',files={'file':(name,f,'video/mp4')})
        assert r.status_code==200;assets.append(r.json()['id'])
    assert client.get('/api/v1/jobs').json()==[]
    created=client.post('/api/v1/batches',json={'asset_ids':assets[:2],'intro_asset_id':assets[2],'config_version':1,'name':'课程批次'},headers={'Idempotency-Key':'two-uploads'})
    assert created.status_code==202
    # Saving a new model while old jobs are queued must not change their calls.
    public=app.state.profiles.public()
    update={'base_version':1,'roles':{r:{k:v for k,v in d.items() if k!='key_configured'} for r,d in public['roles'].items()},'template':public['template'],'max_calls_per_job':160}
    update['roles']['recognizer']['model']='changed-after-queue'
    assert client.put('/api/v1/config',json=update).status_code==200
    generated=[];models_seen=[]
    class PerCourseProvider(FakeReviewProvider):
        def __init__(self,settings,*args):models_seen.append(settings.qwen_model)
        def generate_title(self,title,presenter,style,target,reference_images=()):
            assert style==cfg.title_fallback_style
            generated.append((title,Path(target)))
            Image.new('RGB',(320,180),'white').save(target)
            return Path(target)
    class AcceptedAnalysis:
        def __init__(self,settings,store,jid,rev,token,source,p,work):self.store,self.jid,self.rev=store,jid,rev
        def run(self,asset_id):
            with self.store.session() as s:
                job=s.get(Job,self.jid);meta=json.loads(job.result);asset=s.get(Asset,asset_id)
                title='课程一' if asset.name=='1.mp4' else '课程三'
                plan=ConfirmedPlan(title=title,lecture={'start':2,'end':5},intro_asset_id=assets[2],reason='synthetic independently accepted analysis')
                s.add(Revision(job_id=self.jid,number=self.rev,plan=encode(plan.model_dump())))
                job.stage='render'
            return plan,{**meta,'candidate':{'title':title,'style_description':''}}
    monkeypatch.setattr('lessonscut.pipeline.AutomaticAnalysis',AcceptedAnalysis)
    monkeypatch.setattr('lessonscut.pipeline.render',lambda *a,**kw:real_render(*a,**kw,size=(320,180)))
    pipeline=Pipeline(cfg,store,PerCourseProvider)
    assert run_once(cfg,store,pipeline) and run_once(cfg,store,pipeline)
    assert not run_once(cfg,store,pipeline)
    assert {t for t,_ in generated}=={'课程一','课程三'}
    assert len({p for _,p in generated})==2
    assert models_seen==[cfg.qwen_model,cfg.qwen_model]
    for jid in created.json()['job_ids']:
        item=client.get('/api/v1/jobs/'+jid).json()
        assert item['state']=='DELIVERED',item['reason']
        assert item['result']['config_version']==1
        assert item['result']['technical']['full_decode']
        assert client.get('/api/v1/jobs/'+jid+'/title').status_code==200
        assert client.get('/api/v1/jobs/'+jid+'/delivery').status_code==200
