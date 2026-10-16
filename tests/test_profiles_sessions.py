import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from lessonscut.api import create_app
from lessonscut.config import Settings
from lessonscut.profiles import ProfileBody, Profiles, ProfileConflict
from lessonscut.store import Asset, Call, Job, LoginSession, ConfigurationVersion
from lessonscut.providers import Providers, ProviderError


@pytest.fixture
def app(tmp_path):
    cfg=Settings(_env_file=None,LESSONSCUT_ADMIN_TOKEN='access-password',LESSONSCUT_DATABASE_URL=f'sqlite:///{tmp_path}/db.sqlite',LESSONSCUT_DATA_DIR=tmp_path/'data',LESSONSCUT_INPUT_ROOT=tmp_path/'input',MINIMAX_API_KEY='original-secret',DASHSCOPE_API_KEY='image-secret')
    app=create_app(cfg)
    with app.state.store.session() as s:
        s.add_all([Asset(id='course-1',name='1.mp4',path='source',sha256='a'*64,kind='video'),Asset(id='course-3',name='3.mp4',path='source',sha256='b'*64,kind='video')])
    return app


def body(public):
    return {'base_version':public['version'],'roles':{r:{k:v for k,v in c.items() if k!='key_configured'} for r,c in public['roles'].items()},'template':public['template'],'max_calls_per_job':public['max_calls_per_job']}


def test_cookie_login_csrf_logout_and_session_expiry(app):
    c=TestClient(app)
    assert c.get('/api/v1/config').status_code==401
    assert c.post('/api/v1/auth/login',json={'password':'不正确'}).status_code==401
    assert c.post('/api/v1/auth/login',json={'password':'access-password'},headers={'Origin':'https://other.test'}).status_code==403
    r=c.post('/api/v1/auth/login',json={'password':'access-password'})
    assert r.status_code==200 and 'HttpOnly' in r.headers['set-cookie'] and 'SameSite=strict' in r.headers['set-cookie']
    assert c.get('/api/v1/auth/session').status_code==200
    payload=body(c.get('/api/v1/config').json())
    assert c.put('/api/v1/config',json=payload).status_code==403
    headers={'X-LessonsCut-Request':'1'}
    assert c.put('/api/v1/config',json=payload,headers={**headers,'Origin':'https://other.test'}).status_code==403
    assert c.put('/api/v1/config',json=payload,headers=headers).status_code==200
    assert c.post('/api/v1/auth/logout',headers=headers).status_code==200
    assert c.get('/api/v1/config').status_code==401
    c.post('/api/v1/auth/login',json={'password':'access-password'})
    with app.state.store.session() as s:
        for row in s.scalars(select(LoginSession)):row.expires=time.time()-1
    assert c.get('/api/v1/config').status_code==401


def test_login_rate_limit(app):
    c=TestClient(app)
    for _ in range(10):assert c.post('/api/v1/auth/login',json={'password':'wrong'}).status_code==401
    assert c.post('/api/v1/auth/login',json={'password':'wrong'}).status_code==429


def test_versions_and_secret_redaction_and_endpoint_binding(app):
    c=TestClient(app,headers={'Authorization':'Bearer access-password'})
    original=c.get('/api/v1/config').json()
    assert original['roles']['supervisor']['key_configured']
    assert 'original-secret' not in json.dumps(original) and 'credential_ref' not in json.dumps(original)
    update=body(original);update['roles']['supervisor']['model']='custom-model';update['roles']['supervisor']['api_key']='replacement-secret'
    saved=c.put('/api/v1/config',json=update)
    assert saved.status_code==200 and saved.json()['version']==2
    assert 'replacement-secret' not in saved.text
    assert c.put('/api/v1/config',json=update).status_code==409
    profiles=app.state.profiles
    assert profiles.effective(1).minimax_api_key.get_secret_value()=='original-secret'
    assert profiles.effective(2).minimax_api_key.get_secret_value()=='replacement-secret'
    assert profiles.effective(1).minimax_supervisor_model!='custom-model'
    # Owner-readable credentials; no raw keys in the versioned database.
    with app.state.store.session() as s:
        for row in s.scalars(select(ConfigurationVersion)):
            assert 'replacement-secret' not in row.data and 'original-secret' not in row.data
    for file in profiles.vault.iterdir():assert file.stat().st_mode&0o777==0o600
    assert profiles.vault.stat().st_mode&0o777==0o700
    update=body(saved.json());update['roles']['supervisor']['url']='https://new.example/v1'
    assert c.put('/api/v1/config',json=update).status_code==422
    update['roles']['supervisor']['clear_key']=True
    r=c.put('/api/v1/config',json=update)
    assert r.status_code==200 and not r.json()['roles']['supervisor']['key_configured']


def test_batch_snapshot_idempotency_and_stale_configuration(app):
    c=TestClient(app,headers={'Authorization':'Bearer access-password'})
    payload={'asset_ids':['course-1','course-3'],'name':'两课批次','config_version':1}
    headers={'Idempotency-Key':'two-courses'}
    first=c.post('/api/v1/batches',json=payload,headers=headers)
    assert first.status_code==202 and len(first.json()['job_ids'])==2
    initial=c.get('/api/v1/config').json();update=body(initial);update['roles']['recognizer']['model']='next-model'
    assert c.put('/api/v1/config',json=update).status_code==200
    assert c.post('/api/v1/batches',json=payload,headers=headers).json()==first.json()
    assert c.post('/api/v1/batches',json=payload,headers={'Idempotency-Key':'new-request'}).status_code==409
    for jid in first.json()['job_ids']:
        job=c.get('/api/v1/jobs/'+jid).json()
        assert job['result']['config_version']==1 and job['result']['batch_name']=='两课批次'
    assert app.state.profiles.effective(1).qwen_model!='next-model'
    assert app.state.profiles.effective(2).qwen_model=='next-model'


def test_saved_configuration_survives_restart_and_never_changes_base_settings(app):
    profiles=app.state.profiles
    before=app.state.settings.qwen_model
    update=body(profiles.public());update['roles']['recognizer']['model']='new-model'
    profiles.save(ProfileBody.model_validate(update))
    restarted=create_app(app.state.settings)
    assert restarted.state.profiles.effective().qwen_model=='new-model'
    assert app.state.settings.qwen_model==before


def test_invalid_endpoints_and_unsupported_role_protocol_rejected(app):
    c=TestClient(app,headers={'Authorization':'Bearer access-password'})
    update=body(c.get('/api/v1/config').json())
    for url in ['file:///etc/passwd','https://secret@host/v1','http://host:99999/v1','https://host/v1?key=secret']:
        update['roles']['recognizer']['url']=url
        assert c.put('/api/v1/config',json=update).status_code==422
    update['roles']['recognizer']['url']='http://127.0.0.1:30000/v1'
    update['roles']['image']['protocol']='openai'
    assert c.put('/api/v1/config',json=update).status_code==422


def test_image_configuration_check_does_not_generate_image(app,monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('configuration check must not generate a paid image')
    monkeypatch.setattr(Providers,'generate_title',forbidden)
    c=TestClient(app,headers={'Authorization':'Bearer access-password'})
    r=c.post('/api/v1/config/checks/image')
    assert r.status_code==200 and r.json()['status']=='configured'
    with app.state.store.session() as s:assert not list(s.scalars(select(Call)))


def test_role_limits_and_custom_supervisor_payload(app):
    import httpx
    cfg=app.state.settings.model_copy(update={'supervisor_protocol':'openai','role_call_limits':{'supervisor':1},'role_timeouts':{'supervisor':42}})
    calls=[]
    def handler(req):
        payload=json.loads(req.content);calls.append(payload)
        assert 'thinking' not in payload and payload['max_tokens']==4000
        assert req.extensions['timeout']['read']==42
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'}}]})
    p=Providers(cfg,app.state.store,'budget',1,httpx.Client(transport=httpx.MockTransport(handler)))
    p.chat('supervisor','first')
    with pytest.raises(ProviderError,match='budget'):p.chat('supervisor','different')
    assert len(calls)==1


def test_missing_credential_can_be_repaired_from_configuration_page(app):
    c=TestClient(app,headers={'Authorization':'Bearer access-password'})
    _,raw=app.state.profiles.raw()
    (app.state.profiles.vault/raw['roles']['supervisor']['credential_ref']).unlink()
    current=c.get('/api/v1/config')
    assert current.status_code==200
    assert not current.json()['roles']['supervisor']['key_configured']
    assert c.post('/api/v1/config/checks/supervisor').status_code==422
    update=body(current.json());update['roles']['supervisor']['api_key']='repaired-key'
    assert c.put('/api/v1/config',json=update).status_code==200
    assert app.state.profiles.effective().minimax_api_key.get_secret_value()=='repaired-key'
