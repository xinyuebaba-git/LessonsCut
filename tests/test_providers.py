import base64,json
from pathlib import Path
import httpx,pytest
from lessonscut.config import Settings
from lessonscut.providers import Providers,ProviderError,parse_json
from lessonscut.store import Store,Call
from sqlalchemy import select


def provider(tmp_path,handler):
    cfg=Settings(_env_file=None,LESSONSCUT_DATA_DIR=tmp_path,MINIMAX_API_KEY="secret-test-key",LESSONSCUT_MAX_CALLS_PER_JOB=2)
    db=Store(f"sqlite:///{tmp_path}/db.sqlite")
    client=httpx.Client(transport=httpx.MockTransport(handler))
    return Providers(cfg,db,"job",1,client),db


def test_usage_missing_not_zero_and_token_not_logged(tmp_path):
    p,db=provider(tmp_path,lambda req:httpx.Response(200,json={"choices":[{"message":{"content":"{\"decision\":\"uncertain\"}"}}]}))
    assert p.chat('supervisor','test')['decision']=='uncertain'
    with db.session() as s:
        call=s.scalar(select(Call));details=json.loads(call.details)
        assert details['usage_status']=='missing' and details['usage'] is None
        assert 'secret-test-key' not in call.details


def test_http200_provider_error_is_failure(tmp_path):
    p,db=provider(tmp_path,lambda req:httpx.Response(200,json={"base_resp":{"status_code":1004,"status_msg":"bad"}}))
    with pytest.raises(ProviderError):p.chat('supervisor','test')
    with db.session() as s:assert s.scalar(select(Call)).status=='failed'


def test_budget_includes_failed_attempts(tmp_path):
    p,db=provider(tmp_path,lambda req:httpx.Response(401,json={}))
    for i in range(2):
        with pytest.raises(ProviderError):p.chat('supervisor','test')
    with pytest.raises(ProviderError,match='budget'):p.chat('supervisor','test')


def test_no_implicit_redirect(tmp_path):
    p,db=provider(tmp_path,lambda req:httpx.Response(302,headers={"Location":"https://other.test"}))
    with pytest.raises(ProviderError,match='302'):p.chat('supervisor','test')


def test_strict_json_and_complete_reasoning():
    assert parse_json('<think>reason</think>```json\n{"x":1}\n```')=={'x':1}
    with pytest.raises(ProviderError):parse_json('Here is {"pass":true}')


def test_aliyun_native_image_schema_and_separate_usage(tmp_path):
    import io
    from PIL import Image
    image=io.BytesIO();Image.new('RGB',(64,36),'white').save(image,format='PNG')
    def handler(req):
        if req.method=='GET':
            assert 'authorization' not in req.headers
            return httpx.Response(200,content=image.getvalue())
        body=json.loads(req.content)
        assert body['model']=='qwen-image-3.0'
        assert body['parameters']['size']=='1920*1080'
        assert body['input']['messages'][0]['role']=='user'
        assert 'image_generation' not in str(req.url)
        return httpx.Response(200,json={'output':{'choices':[{'message':{'content':[{'image':'https://result.oss-cn-beijing.aliyuncs.com/test.png'}]}}]},'usage':{'output_image_count':1,'output_width':1920,'output_height':1080},'request_id':'image-test'})
    p,db=provider(tmp_path,handler)
    p.settings.dashscope_api_key=__import__('pydantic').SecretStr('image-only-key')
    path=p.generate_title('课程',None,'白底绿色字',tmp_path/'out.png')
    assert path.read_bytes()==image.getvalue()
    with db.session() as s:
        d=json.loads(s.scalar(select(Call)).details)
        assert d['usage_kind']=='image_units' and d['usage']['output_image_count']==1
        assert 'total_tokens' not in d['usage']


def test_image_result_rejects_private_or_unknown_host(tmp_path):
    p,db=provider(tmp_path,lambda req:httpx.Response(200,json={'output':{'choices':[{'message':{'content':[{'image':'http://127.0.0.1/secret'}]}}]}}))
    p.settings.dashscope_api_key=__import__('pydantic').SecretStr('image-only-key')
    with pytest.raises(ProviderError,match='unapproved'):p.generate_title('课程',None,'white',tmp_path/'x.png')


def test_completed_request_cache_prevents_duplicate_billing(tmp_path):
    requests=[]
    def handler(req):
        requests.append(req)
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"title":"课程"}'}}],'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}})
    p,db=provider(tmp_path,handler)
    assert p.chat('supervisor','same evidence')==p.chat('supervisor','same evidence')
    assert len(requests)==1
    with db.session() as s:assert len(list(s.scalars(select(Call))))==1


def test_format_repair_does_not_retry_unknown_transport_execution():
    from lessonscut.pipeline import Pipeline
    from lessonscut.providers import ProviderOutputError
    class InvalidThenValid:
        calls=0
        def chat(self,*args):
            self.calls+=1
            if self.calls==1:raise ProviderOutputError('invalid JSON')
            return {'decision':'uncertain','checks':[{'code':'title_text','decision':'uncertain','reason':'unreadable'}],'issues':['unreadable'],'limits':'fixture'}
    p=InvalidThenValid();result=Pipeline(None,None).reviewed(p,'review',[])
    assert p.calls==2 and result.decision=='uncertain'
    class Unknown:
        calls=0
        def chat(self,*args):
            self.calls+=1
            raise ProviderError('execution may be unknown')
    p=Unknown()
    with pytest.raises(ProviderError):Pipeline(None,None).reviewed(p,'review',[])
    assert p.calls==1
