import json
from pathlib import Path
import pytest
from PIL import Image, ImageChops
from pydantic import ValidationError
from lessonscut.automatic import single_transition, evidence_supports, NeedsReview,end_bracket
from lessonscut.asr import Transcript
from lessonscut.frame_index import FrameIndex
from lessonscut.media import run


@pytest.mark.parametrize("states",[[False,True,False,True],[True,True,True,True],[False,False,False,False]])
def test_revised_or_nonmonotonic_boundary_goes_to_review(states):
    with pytest.raises(NeedsReview):single_transition([1,2,3,4],states,False,True)


def test_adjacent_first_after_frame():
    assert single_transition([10,11,12,13],[False,False,True,True],False,True)==(11,12)
    with pytest.raises(NeedsReview):single_transition([10,10],[False,True],False,True)


def test_brand_card_after_final_teaching_is_only_an_end_candidate():
    from types import SimpleNamespace
    rows=[SimpleNamespace(category=c) for c in ['intro_guangzhou','presenter','intro_guangzhou','teaching_slide','intro_guangzhou']]
    # The brand in the middle must not stop a continuous course.
    assert end_bracket(rows,{'presenter','teaching_slide'})==(3,4)


def test_title_evidence_not_inferred_from_broad_category():
    texts={'asr-1':'今天我们来说一下名誉侵权。','frame-10':'人格安全教育'}
    assert evidence_supports('名誉侵权',['asr-1'],texts)
    assert not evidence_supports('名誉侵权',['frame-10'],texts)
    assert not evidence_supports('名誉侵权',['asr-1','made-up-id'],texts)
    assert evidence_supports('名誉侵权',['asr-1'],{'asr-1':'今天我們來說名譽侵權'})


def test_transcript_rejects_empty_or_bad_timestamps():
    base={'schema_version':'1.0','engine':'faster-whisper','device':'cpu','compute_type':'int8','model':'test','audio_sha256':'x','language':'zh','duration':10,'duration_ms':1,'segments':[]}
    with pytest.raises(ValidationError):Transcript(**base)
    base['segments']=[{'id':'asr-0','start':9,'end':8,'text':'test','avg_logprob':0,'no_speech_prob':0,'words':[]}]
    with pytest.raises(ValidationError):Transcript(**base)


def test_exact_pts_extraction_matches_full_decode(tmp_path):
    source=tmp_path/'source.mp4'
    run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=30','-t','4','-c:v','libx264','-preset','ultrafast',str(source)])
    index=FrameIndex(source,tmp_path/'pts.json')
    assert index.nearest(73/30)==73
    selected=index.extract(73,tmp_path/'selected.png')
    reference=tmp_path/'reference.png'
    run(['ffmpeg','-v','error','-i',str(source),'-vf','select=eq(n\\,73),scale=1280:-2','-frames:v','1','-fps_mode','vfr',str(reference)])
    assert ImageChops.difference(Image.open(selected),Image.open(reference)).getbbox() is None


@pytest.mark.parametrize('cloud_conflict',[False,True])
def test_automatic_plan_from_asr_and_adjacent_frames_or_conflict(tmp_path,cloud_conflict):
    from lessonscut.automatic import AutomaticAnalysis
    from lessonscut.config import Settings
    from lessonscut.store import Store,Asset,Job,Revision,encode
    from lessonscut.media import digest
    import re
    source=tmp_path/'source.mp4'
    run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=30','-t','12','-c:v','libx264','-preset','ultrafast',str(source)])
    cfg=Settings(_env_file=None,LESSONSCUT_DATA_DIR=tmp_path/'data',LESSONSCUT_DATABASE_URL=f'sqlite:///{tmp_path}/db.sqlite',ASR_BACKEND='local')
    cfg.prepare();store=Store(cfg.database_url)
    with store.session() as s:
        s.add(Asset(id='source',path=str(source),sha256=digest(source),name='fixture',kind='video'))
        s.add(Job(id='job',batch_id='batch',asset_id='source',result=encode({'intro_asset_id':'source'})))
    jid,rev,token=store.claim()
    class FakeASR:
        def __init__(self,*a):pass
        def transcribe(self,*a):
            return Transcript(schema_version='1.0',engine='fixture',device='cpu',compute_type='int8',model='fixture',audio_sha256='x',language='zh',duration=12,duration_ms=1,segments=[{'id':'asr-0','start':6.1,'end':8.9,'text':'今天讲测试课程','avg_logprob':-0.1,'no_speech_prob':0.01,'words':[]}])
    class Provider:
        def chat(self,role,prompt,images=()):
            if prompt.startswith('逐张识别'):
                out=[]
                for p in images:
                    f=int(Path(p).stem)
                    c='intro_guangzhou' if f<60 else 'series_title' if f<120 else 'credits' if f<180 else 'presenter' if f<270 else 'outro'
                    out.append({'evidence_id':f'frame-{f}','category':c,'visible_text':'测试课程 主讲人张三','confidence':0.99,'reason':'fixture'})
                return {'samples':out}
            if '逐帧切点精查' in prompt or prompt.startswith('独立复核以下相邻') or prompt.startswith('真人讲解核验'):
                match=re.search(r'(?:图片顺序|图片帧号)=(\[[^\]]+\])',prompt)
                frames=json.loads(match.group(1));start='是否已经进入' in prompt
                return {'frames':[{'frame':f,'belongs':f>=180 if start else f<270,'confidence':0.99,'reason':'fixture'} for f in frames]}
            if prompt.startswith('从实际OCR'):
                return {'title':'测试课程','presenter':'张三','origin':'visible_text','title_evidence_ids':['frame-60'],'presenter_evidence_ids':['frame-180'],'presenter_status':'verified','style_evidence_ids':['frame-60'],'style_description':'白底绿色字体','conflicts':[]}
            if prompt.startswith('独立课名读取'):
                return {'title':'其他课程' if cloud_conflict else '测试课程','issues':[]}
            if prompt.startswith('独立姓名读取'):
                return {'presenter':'张三','issues':[]}
            if prompt.startswith('切点独立复核'):
                codes=['start_boundary'] if 'start_boundary' in prompt else ['end_boundary']
            else:codes=['intro_identity','title_evidence','presenter_evidence','continuous_teaching','speech_context']
            return {'decision':'pass','checks':[{'code':c,'decision':'pass','reason':'fixture'} for c in codes],'issues':[],'limits':'fixture samples only'}
    work=cfg.data_dir/'work/job/1';work.mkdir(parents=True)
    analysis=AutomaticAnalysis(cfg,store,jid,rev,token,source,Provider(),work,asr_factory=FakeASR)
    if cloud_conflict:
        with pytest.raises(NeedsReview,match='disagree'):analysis.run('source')
        with store.session() as s:assert s.get(Revision,('job',1)) is None
    else:
        plan,result=analysis.run('source')
        assert result['source_frame_range']==[180,270] and plan.lecture.start==6 and plan.lecture.end==9
        with store.session() as s:assert s.get(Revision,('job',1)) is not None


def test_cross_boundary_speech_is_recorded_without_auto_truncation():
    from lessonscut.automatic import speech_cut_conflicts
    segments=[{'id':'s1','text':'收束语','words':[{'word':'再见','start':9.7,'end':10.4,'probability':0.99}]}]
    conflicts=speech_cut_conflicts(segments,{'start':2.0,'end':10.0})
    assert len(conflicts)==1 and conflicts[0]['boundary']=='end'
    assert conflicts[0]['segment_id']=='s1' and conflicts[0]['word']['end']==10.4
    assert speech_cut_conflicts(segments,{'start':2.0,'end':10.5})==[]
