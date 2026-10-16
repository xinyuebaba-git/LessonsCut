import pytest
from pydantic import ValidationError
from lessonscut.domain import frame_count,Span,ConfirmedPlan,Review,review_passed


@pytest.mark.parametrize("fps,n",[("25/1",38),("30/1",45),("30000/1001",45)])
def test_title_quantization(fps,n):
    assert frame_count("1.5",fps)==n


@pytest.mark.parametrize("values",[{"start":2,"end":1},{"start":float('nan'),"end":3},{"start":0,"end":float('inf')}])
def test_invalid_spans(values):
    with pytest.raises(ValidationError):Span(**values)


def test_invalid_fps_and_unknown_fields():
    with pytest.raises(ValidationError):
        ConfirmedPlan(title="标题",lecture={"start":1,"end":2},intro_asset_id="x",fps="300/1",reason="核对",shell="rm")


def test_overall_pass_cannot_hide_missing_or_failed_check():
    review=Review(decision='pass',checks=[{'code':'title','decision':'pass','reason':'ok'}],issues=[],limits='samples only')
    assert not review_passed(review,{'title','intro'})
    assert review_passed(review,{'title'})
    review.checks[0].decision='uncertain'
    assert not review_passed(review,{'title'})


def test_held_audio_must_extend_beyond_visual_cut():
    with pytest.raises(ValidationError,match='extend'):
        ConfirmedPlan(title='测试',lecture={'start':1,'end':4},intro_asset_id='x',reason='fixture',ending={'audio_end':4,'evidence_ids':['a'],'reason':'test'})
