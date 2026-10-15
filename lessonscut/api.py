import json
import secrets
from pathlib import Path
from typing import Literal
from fastapi import FastAPI,Depends,HTTPException,UploadFile,File,Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from sqlalchemy import select,update
from sqlalchemy.exc import IntegrityError
from .config import Settings,get_settings
from .domain import ConfirmedPlan
from .store import Store,Asset,Job,Revision,Call,RequestKey,Audit,uid,encode
from .media import authorized_path,digest,probe


class ImportBody(BaseModel):
    relative_path: str
    kind: str = Field(default="video",pattern="^(video|image)$")


class BatchBody(BaseModel):
    asset_ids: list[str] = Field(min_length=1,max_length=100)
    intro_asset_id: str | None = None
    approval_policy: Literal["exceptions_only", "always"] = "exceptions_only"


class RevisionBody(BaseModel):
    base_revision: int
    plan: ConfirmedPlan


def create_app(settings: Settings | None=None):
    settings=settings or get_settings();settings.prepare()
    store=Store(settings.database_url)
    app=FastAPI(title="LessonsCut",version="0.2.0")
    app.state.store=store;app.state.settings=settings

    def auth(authorization: str | None=Header(default=None)):
        expected=settings.admin_token.get_secret_value()
        if not expected:
            raise HTTPException(503,"configure LESSONSCUT_ADMIN_TOKEN")
        if not authorization or not secrets.compare_digest(authorization,"Bearer "+expected):
            raise HTTPException(401,"authentication required")

    def job_view(j):
        return {"id":j.id,"batch_id":j.batch_id,"asset_id":j.asset_id,"revision":j.revision,"state":j.state,"stage":j.stage,"reason":j.reason,"result":json.loads(j.result)}

    def register(path,name,kind):
        if kind=="video":
            probe(path)
        else:
            from PIL import Image
            with Image.open(path) as im:im.verify()
        with store.session() as s:
            a=Asset(path=str(Path(path).resolve()),name=name,sha256=digest(path),kind=kind);s.add(a);s.flush()
            return {"id":a.id,"name":a.name,"sha256":a.sha256,"kind":a.kind}

    @app.get("/health")
    def health():return {"status":"ok","version":"0.2.0"}

    @app.get("/api/v1/config",dependencies=[Depends(auth)])
    def config():
        return {"qwen_model":settings.qwen_model,"minimax_supervisor_model":settings.minimax_supervisor_model,"image_model":settings.qwen_image_model,"image_provider":"aliyun","minimax_key_configured":bool(settings.minimax_api_key.get_secret_value()),"image_key_configured":bool(settings.dashscope_api_key.get_secret_value()),"max_calls_per_job":settings.max_calls_per_job,"gpu":"3090 development / existing external service","automatic_plan_approval":settings.automatic_plan_approval,"asr_backend":settings.asr_backend,"asr_device":"cpu/int8","default_intro_asset_id":settings.default_intro_asset_id}

    @app.post("/api/v1/assets/imports",dependencies=[Depends(auth)])
    def import_asset(body: ImportBody):
        try:return register(authorized_path(settings.input_root,body.relative_path),Path(body.relative_path).name,body.kind)
        except (ValueError,OSError) as exc:raise HTTPException(422,str(exc)) from None

    @app.post("/api/v1/assets/uploads",dependencies=[Depends(auth)])
    def upload(file: UploadFile=File(),kind: str="video"):
        if kind not in ("video","image"):raise HTTPException(422,"invalid asset kind")
        name=Path(file.filename or "asset").name
        target=settings.data_dir/"uploads"/uid()
        total=0
        try:
            with target.open("xb") as out:
                while chunk:=file.file.read(1024*1024):
                    total+=len(chunk)
                    if total>20*1024**3:raise ValueError("upload exceeds 20GiB")
                    out.write(chunk)
            return register(target,name,kind)
        except Exception as exc:
            target.unlink(missing_ok=True)
            raise HTTPException(422,"invalid upload: "+str(exc)[:200]) from None

    @app.get("/api/v1/assets",dependencies=[Depends(auth)])
    def assets():
        with store.session() as s:return [{"id":a.id,"name":a.name,"kind":a.kind,"sha256":a.sha256} for a in s.scalars(select(Asset))]

    @app.post("/api/v1/batches",status_code=202,dependencies=[Depends(auth)])
    def batch(body: BatchBody,idempotency_key: str=Header(min_length=1,max_length=160)):
        fingerprint=store.fingerprint(body.model_dump())
        def replay(row):
            if row.fingerprint!=fingerprint:raise HTTPException(409,"idempotency key reused with different body")
            return json.loads(row.response)
        try:
            with store.session() as s:
                old=s.get(RequestKey,idempotency_key)
                if old:return replay(old)
                for asset_id in set(body.asset_ids):
                    a=s.get(Asset,asset_id)
                    if not a or a.kind!="video":raise HTTPException(422,"missing video asset")
                intro_id=body.intro_asset_id or settings.default_intro_asset_id
                if intro_id:
                    intro=s.get(Asset,intro_id)
                    if not intro or intro.kind!="video":raise HTTPException(422,"approved intro asset missing")
                bid=uid();jobs=[]
                for aid in dict.fromkeys(body.asset_ids):
                    j=Job(id=uid(),batch_id=bid,asset_id=aid,result=encode({"intro_asset_id":intro_id,"approval_policy":body.approval_policy}));s.add(j);jobs.append(j.id)
                response={"batch_id":bid,"job_ids":jobs}
                s.add(RequestKey(key=idempotency_key,fingerprint=fingerprint,response=encode(response)))
                return response
        except IntegrityError:
            with store.session() as s:
                row=s.get(RequestKey,idempotency_key)
                if not row:raise HTTPException(409,"concurrent request; retry same key") from None
                return replay(row)

    @app.get("/api/v1/jobs",dependencies=[Depends(auth)])
    def jobs():
        with store.session() as s:return [job_view(j) for j in s.scalars(select(Job).order_by(Job.created.desc()).limit(100))]

    @app.get("/api/v1/jobs/{jid}",dependencies=[Depends(auth)])
    def get_job(jid: str):
        with store.session() as s:
            job=s.get(Job,jid)
            if not job:raise HTTPException(404,"job not found")
            return job_view(job)

    @app.post("/api/v1/jobs/{jid}/revisions",status_code=202,dependencies=[Depends(auth)])
    def revise(jid: str,body: RevisionBody):
        with store.session() as s:
            j=s.scalar(select(Job).where(Job.id==jid).with_for_update())
            if not j:raise HTTPException(404,"job not found")
            if j.revision!=body.base_revision or j.token is not None or j.state=="RUNNING":
                raise HTTPException(409,"revision conflict or active worker; wait for stage completion")
            intro=s.get(Asset,body.plan.intro_asset_id)
            title=s.get(Asset,body.plan.title_asset_id) if body.plan.title_asset_id else None
            if not intro or intro.kind!="video" or (body.plan.title_asset_id and (not title or title.kind!="image")):
                raise HTTPException(422,"invalid intro/title asset")
            new=j.revision+1
            previous=json.loads(j.result)
            # Keep source evidence, never present a previous plan's acceptance or output as current.
            retained={k:previous[k] for k in ('automation_version','intro_asset_id','approval_policy','candidate','evidence','observations','coarse_observations','transcript','style_reference_paths') if k in previous}
            retained['source_analysis_revision']=j.revision
            updated=s.execute(update(Job).where(Job.id==jid,Job.revision==body.base_revision,Job.token.is_(None)).values(revision=new,state="QUEUED",stage="render",reason="",result=encode(retained))).rowcount
            if updated!=1:raise HTTPException(409,"revision changed")
            s.add(Revision(job_id=jid,number=new,plan=encode(body.plan.model_dump())))
            s.add(Audit(job_id=jid,revision=new,event="Explicit plan confirmation: "+body.plan.reason))
            return {"job_id":jid,"revision":new,"state":"QUEUED"}

    @app.post("/api/v1/jobs/{jid}/cancel",dependencies=[Depends(auth)])
    def cancel(jid: str):
        with store.session() as s:
            j=s.scalar(select(Job).where(Job.id==jid).with_for_update())
            if not j:raise HTTPException(404,"job not found")
            if j.token or j.state=="DELIVERED":raise HTTPException(409,"v0.1 cannot cancel active or delivered job")
            j.state="CANCELLED";s.add(Audit(job_id=jid,revision=j.revision,event="Cancelled by administrator"))
            return {"state":"CANCELLED"}

    @app.get("/api/v1/jobs/{jid}/usage",dependencies=[Depends(auth)])
    def usage(jid: str):
        with store.session() as s:return [{"id":c.id,"role":c.role,"model":c.model,"status":c.status,**json.loads(c.details)} for c in s.scalars(select(Call).where(Call.job_id==jid))]

    @app.get("/api/v1/jobs/{jid}/report",dependencies=[Depends(auth)])
    def report(jid: str):
        with store.session() as s:
            j=s.get(Job,jid)
            if not j:raise HTTPException(404,"job not found")
            return {"job_id":j.id,"revision":j.revision,"state":j.state,"reason":j.reason,"evidence_report":json.loads(j.result)}

    @app.get("/api/v1/jobs/{jid}/transcript",dependencies=[Depends(auth)])
    def transcript(jid: str):
        with store.session() as s:
            j=s.get(Job,jid)
            if not j:raise HTTPException(404,"job not found")
            transcript=json.loads(j.result).get("transcript")
            if not transcript:raise HTTPException(404,"transcript not ready")
            return transcript

    @app.get("/api/v1/jobs/{jid}/delivery",dependencies=[Depends(auth)])
    def delivery(jid: str):
        with store.session() as s:
            j=s.get(Job,jid)
            if not j or j.state!="DELIVERED":raise HTTPException(404,"no delivered output for current revision")
            data=json.loads(j.result)
            path=authorized_path(settings.data_dir,data["delivery_path"])
            if digest(path)!=data["technical"]["sha256"]:raise HTTPException(409,"delivery hash mismatch")
            return FileResponse(path,media_type="video/mp4",filename=path.name)

    @app.get("/api/v1/jobs/{jid}/evidence/{eid}",dependencies=[Depends(auth)])
    def evidence(jid: str,eid: str):
        with store.session() as s:
            j=s.get(Job,jid)
            if not j:raise HTTPException(404,"job not found")
            e=next((e for e in json.loads(j.result).get("evidence",[]) if e["id"]==eid),None)
            if not e:raise HTTPException(404,"evidence not found")
            path=authorized_path(settings.data_dir,e["path"])
            if digest(path)!=e["sha256"]:raise HTTPException(409,"evidence changed")
            return FileResponse(path,media_type="image/jpeg")

    frontend=Path(__file__).resolve().parent.parent/"frontend/dist"
    if frontend.exists():app.mount("/",StaticFiles(directory=frontend,html=True),name="frontend")
    return app
