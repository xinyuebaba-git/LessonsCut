"""v0.1 workflow: recognition -> explicit plan confirmation -> render/review."""
import json
from fractions import Fraction
from pathlib import Path
from sqlalchemy import select
from .domain import Candidate,ConfirmedPlan,Review,review_passed
from .store import Asset,Job,Revision,Audit,encode
from .providers import Providers
from .media import probe,extract_frame,render,verify_output,verify_audio,digest,publish


class Pipeline:
    def __init__(self,settings,store,providers_factory=Providers):
        self.settings,self.store,self.providers_factory=settings,store,providers_factory

    def execute(self,job_id,revision,token):
        with self.store.session() as s:
            job=s.get(Job,job_id);source=s.get(Asset,job.asset_id)
            source_path,source_hash=source.path,source.sha256
            stage=job.stage
            old=json.loads(job.result)
            rev=s.get(Revision,(job_id,revision))
            saved_plan=json.loads(rev.plan) if rev else None
        if digest(source_path)!=source_hash:
            raise ValueError("source changed since import")
        work=self.settings.data_dir/"work"/job_id/str(revision)
        work.mkdir(parents=True,exist_ok=True)
        p=self.providers_factory(self.settings,self.store,job_id,revision)
        if stage=="analyze":
            self.analyze(job_id,revision,token,Path(source_path),p,work)
        else:
            self.produce(job_id,revision,token,Path(source_path),source_hash,ConfirmedPlan.model_validate(saved_plan),old,p,work)

    def analyze(self,jid,rev,token,source,p,work):
        info=probe(source);duration=float(info["format"]["duration"])
        # This is proposal sampling, never frame-exact boundary approval.
        times=sorted(set([min(x,max(0,duration-0.1)) for x in [0,2,4,6,8,10,15,20,duration/2]+[duration-i for i in [20,15,10,8,6,4,2,0.1]] if x>=0]))
        evidence=[]
        for i,t in enumerate(times):
            path=extract_frame(source,t,work/f"evidence-{i}.jpg")
            evidence.append({"id":f"e{i}","source_seconds":round(t,6),"path":str(path.relative_to(self.settings.data_dir)),"sha256":digest(path)})
        observations=[]
        for offset in range(0,len(evidence),4):
            batch=evidence[offset:offset+4]
            response=p.chat("recognizer","逐张识别图片，顺序与列表一致。返回 {\"samples\":[{\"evidence_id\":\"e0\",\"category\":\"intro|title|presenter|slide|outro|other|uncertain\",\"text\":\"可见文字\",\"reason\":\"依据\"}]}。不得漏样本。列表="+json.dumps([{k:v for k,v in x.items() if k in ('id','source_seconds')} for x in batch],ensure_ascii=False),[self.settings.data_dir/x["path"] for x in batch])
            rows=response.get("samples",[])
            if sorted(x.get("evidence_id","") for x in rows)!=sorted(x["id"] for x in batch):
                raise ValueError("recognizer sample coverage mismatch")
            observations.extend(rows)
        candidate=Candidate.model_validate(p.chat("recognizer","依据刚才本地识别结果提出课程计划候选。不能凭抽样确认精确切点；uncertainties必须说明精查和音频待核实。课名只留本课，不留系列/制作单位。输出 {title,presenter:null或姓名,origin:visible_text或explicit_speech或inferred,lecture:{start:秒,end:秒},style_description,evidence_ids:[],uncertainties:[]}。若只有图片不能声称explicit_speech。素材时长="+str(duration)+"；观察="+json.dumps(observations,ensure_ascii=False)))
        ids={e["id"] for e in evidence}
        if not set(candidate.evidence_ids)<=ids or candidate.lecture.end>duration or candidate.origin=="explicit_speech":
            raise ValueError("candidate references unsupported evidence or speech")
        # Independent supervisor sees source images and task rubric, not local pass/candidate.
        selected=evidence[:5]+evidence[-4:]
        blind=Review.model_validate(p.chat("supervisor","独立检查这些课程视频源抽样，判断是否充分覆盖具体课名、讲师、授课起止。不要猜测精确切点或音频。返回 {decision:pass|fail|uncertain,checks:[{code,decision,reason}],issues:[],limits:字符串}。样本时间="+json.dumps([{k:x[k] for k in ('id','source_seconds')} for x in selected]),[self.settings.data_dir/x["path"] for x in selected]))
        result={"probe":info,"evidence":evidence,"observations":observations,"candidate":candidate.model_dump(),"blind_review":blind.model_dump()}
        (work/"analysis.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
        self.store.transition(jid,rev,token,"NEEDS_REVIEW",result,"v0.1: review source evidence and confirm frame-accurate plan before rendering; no local ASR configured")

    def produce(self,jid,rev,token,source,source_hash,plan,old,p,work):
        with self.store.session() as s:
            intro=s.get(Asset,plan.intro_asset_id)
            title=s.get(Asset,plan.title_asset_id) if plan.title_asset_id else None
            if intro is None or intro.kind!="video":
                raise ValueError("approved intro video is missing")
            intro_path,intro_hash=intro.path,intro.sha256
            title_path,title_hash=(title.path,title.sha256) if title else (None,None)
        if digest(intro_path)!=intro_hash:
            raise ValueError("intro changed since registration")
        style=old.get("candidate",{}).get("style_description","")
        if title_path:
            if not title or title.kind!="image" or digest(title_path)!=title_hash:
                raise ValueError("registered title image invalid or changed")
            title=Path(title_path)
        else:
            if not style:
                raise ValueError("source title style evidence missing; upload an approved title image")
            title=p.generate_title(plan.title,plan.presenter,style,work/"title.png")
        target={"title":plan.title,"presenter":plan.presenter,"style":style}
        title_checks={"title_text","presenter_text","no_extra_text","not_clipped","style_match"}
        rubric="核对标题图片：只允许批准的课名和已核实讲师文字，无系列/制作单位/额外字，无错字裁字，与给定风格描述相符。先读实际文字再比较。每个必需code必须出现一次："+json.dumps(sorted(title_checks))+"；姓名为空时presenter_text应检查无编造姓名。返回 {decision:pass|fail|uncertain,checks:[{code,decision,reason}],issues:[],limits:字符串}。批准目标="+json.dumps(target,ensure_ascii=False)
        title_review=Review.model_validate(p.chat("supervisor",rubric,[title]))
        if not review_passed(title_review,title_checks):
            self.store.transition(jid,rev,token,"NEEDS_REVIEW",{**old,"title_review":title_review.model_dump()},"title image requires correction")
            return
        output,expected=render(source,intro_path,title,plan,work/"render",self.settings.encoder)
        technical=verify_output(output,expected)
        audio=verify_audio(source,output,plan)
        duration=technical["duration"]
        fps=float(Fraction(plan.fps))
        intro_seconds=expected["segment_frames"][0]/fps
        lecture_offset=sum(expected["segment_frames"][:2])/fps
        frames=[]
        for i,t in enumerate([intro_seconds/2,intro_seconds-1/fps,intro_seconds,(intro_seconds+lecture_offset)/2,lecture_offset-1/fps,lecture_offset,min(duration-0.1,lecture_offset+(plan.lecture.end-plan.lecture.start)/2),duration-0.1]):
            frames.append(extract_frame(output,max(0,t),work/f"export-{i}.jpg"))
        reference=[]
        for i,t in enumerate([plan.lecture.start,(plan.lecture.start+plan.lecture.end)/2,max(plan.lecture.start,plan.lecture.end-0.1)]):
            reference.append(extract_frame(source,t,work/f"source-reference-{i}.jpg"))
        export_checks={"intro_identity","title_text","presenter_text","no_extra_text","not_clipped","lecture_start","lecture_body","lecture_end","no_black_frame"}
        review=Review.model_validate(p.chat("supervisor","前8张是实际MP4抽帧，依次是片头代表/末尾、标题首中末、授课首帧/中段/末帧。最后3张是批准切点映射的原片授课首中尾参考，请比较教学内容是否一致。批准目标="+json.dumps(target,ensure_ascii=False)+"。必须检查广州开放大学片头身份、准确标题姓名、无额外标题文字、无裁字、授课而非制作名单结尾、无异常黑屏。每个必需code必须出现一次："+json.dumps(sorted(export_checks))+"。只根据给定帧，不声称听过音频或看过全片。返回 {decision:pass|fail|uncertain,checks:[{code,decision,reason}],issues:[],limits:字符串}。",frames+reference))
        result={**old,"plan":plan.model_dump(),"expected":expected,"technical":technical,"audio":audio,"title_review":title_review.model_dump(),"export_review":review.model_dump(),"render_path":str(output.relative_to(self.settings.data_dir))}
        if not review_passed(review,export_checks):
            self.store.transition(jid,rev,token,"NEEDS_REVIEW",result,"export sample review requires correction")
            return
        if digest(source)!=source_hash:
            raise ValueError("source changed during render")
        # Revision edits are rejected during RUNNING in v0.1, preventing publish/edit races.
        with self.store.session() as s:
            job=s.scalar(select(Job).where(Job.id==jid).with_for_update())
            if job.revision!=rev or job.token!=token:
                raise ValueError("stale revision before publish")
            dest=publish(output,self.settings.data_dir/"output",f"{jid}-r{rev}.mp4",technical["sha256"])
            result["delivery_path"]=str(dest.relative_to(self.settings.data_dir))
            (work/"project.json").write_text(json.dumps({"schema_version":"0.1","source_hash":source_hash,"intro_hash":intro_hash,"title_hash":digest(title),"plan":plan.model_dump(),"expected":expected},ensure_ascii=False,indent=2))
            (work/"report.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
            job.state="DELIVERED";job.token=None;job.lease_until=0;job.result=encode(result)
            s.add(Audit(job_id=jid,revision=rev,event="Published verified MP4 with explicit plan confirmation"))
