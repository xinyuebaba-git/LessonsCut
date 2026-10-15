from pathlib import Path
import json,subprocess
import pytest
from PIL import Image
from lessonscut.media import render,verify_output,verify_audio,publish,digest,authorized_path,run
from lessonscut.domain import ConfirmedPlan


def test_publish_never_overwrites_and_hash_gate(tmp_path):
    source=tmp_path/'work.mp4';source.write_bytes(b'original')
    dest=tmp_path/'out';dest.mkdir();(dest/'course.mp4').write_bytes(b'existing')
    result=publish(source,dest,'course.mp4',digest(source))
    assert result.name=='course-1.mp4' and (dest/'course.mp4').read_bytes()==b'existing'
    source.write_bytes(b'changed')
    with pytest.raises(ValueError,match='hash'):publish(source,dest,'new.mp4','0'*64)


def test_symlink_escape(tmp_path):
    allowed=tmp_path/'root';allowed.mkdir();outside=tmp_path/'private';outside.write_text('x')
    (allowed/'link').symlink_to(outside)
    with pytest.raises(ValueError):authorized_path(allowed,'link')


def test_real_ffmpeg_render_decode_and_audio(tmp_path):
    source=tmp_path/'source.mp4'
    run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=30','-f','lavfi','-i','anoisesrc=color=pink:seed=42:sample_rate=48000','-t','8','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)])
    title=tmp_path/'title.png';Image.new('RGB',(320,180),'white').save(title)
    before=digest(source)
    plan=ConfirmedPlan(title='测试',lecture={'start':2,'end':5},intro_asset_id='intro',reason='synthetic test')
    output,expected=render(source,source,title,plan,tmp_path/'render',size=(320,180))
    report=verify_output(output,expected)
    assert report['frames']==315 and expected['segment_frames']==[180,45,90]
    checks=verify_audio(source,output,plan)
    assert len(checks)==3
    assert digest(source)==before
    # Truncated bytes must fail a full decode/probe, regardless of prior status.
    bad=tmp_path/'bad.mp4';bad.write_bytes(output.read_bytes()[:1000])
    with pytest.raises(ValueError):verify_output(bad,expected)
