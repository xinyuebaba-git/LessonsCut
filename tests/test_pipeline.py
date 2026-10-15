import json
from pathlib import Path
from PIL import Image
from lessonscut.config import Settings
from lessonscut.store import Store,Asset,Job,Revision,encode
from lessonscut.media import run,digest,render as real_render
from lessonscut.pipeline import Pipeline
from lessonscut.worker import run_once


class FakeReviewProvider:
    def __init__(self,*args):pass
    def chat(self,role,prompt,images):
        assert role=='supervisor'
        if prompt.startswith('核对标题图片'):codes=['title_text','presenter_text','no_extra_text','not_clipped','style_match']
        elif prompt.startswith('成片片头标题复核'):codes=['intro_identity','title_text','presenter_text','no_extra_text','not_clipped']
        else:codes=['lecture_start','lecture_body','lecture_end','no_black_frame']
        return {'decision':'pass','checks':[{'code':c,'decision':'pass','reason':'synthetic fixture'} for c in codes],'issues':[],'limits':'synthetic samples only'}


def test_confirmed_revision_to_real_mp4_with_mock_model_review(tmp_path,monkeypatch):
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
    pipeline=Pipeline(cfg,store,FakeReviewProvider)
    assert run_once(cfg,store,pipeline)
    with store.session() as s:
        job=s.get(Job,'job');result=json.loads(job.result)
        assert job.state=='DELIVERED',job.reason
        assert result['technical']['frames']==315
        assert len(result['audio'])==3
        assert (cfg.data_dir/result['delivery_path']).is_file()
        assert (cfg.data_dir/'work/job/2/project.json').is_file()
    assert digest(source)==original
    assert not run_once(cfg,store,pipeline)
