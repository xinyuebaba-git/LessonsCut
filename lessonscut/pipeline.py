"""Recognition, local speech evidence, automatic cuts, render and independent review."""
import json
from fractions import Fraction
from pathlib import Path
from pydantic import ValidationError
from sqlalchemy import select
from .domain import Candidate,ConfirmedPlan,Review,review_passed
from .store import Asset,Job,Revision,Audit,encode
from .providers import Providers,ProviderOutputError
from .automatic import AutomaticAnalysis
from .media import probe,extract_frame,render,verify_output,verify_audio,digest,publish


class Pipeline:
    def __init__(self,settings,store,providers_factory=Providers):
        self.settings,self.store,self.providers_factory=settings,store,providers_factory

    def reviewed(self,p,prompt,images):
        prompt+='\n返回严格JSON，字符串中的引号必须转义；结构='+json.dumps(Review.model_json_schema(),ensure_ascii=False)
        for attempt in range(3):
            try:return Review.model_validate(p.chat('supervisor',prompt,images))
            except (ProviderOutputError,ValidationError) as exc:
                if attempt==2:raise ValueError('review format invalid after bounded repairs') from exc
                prompt+='\n上一答复格式错误，仅修正格式、不改变证据和判断：'+str(exc)[:1000]

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
            plan,analysis=AutomaticAnalysis(self.settings,self.store,job_id,revision,token,Path(source_path),p,work).run(job.asset_id)
            self.produce(job_id,revision,token,Path(source_path),source_hash,plan,analysis,p,work)
        else:
            self.produce(job_id,revision,token,Path(source_path),source_hash,ConfirmedPlan.model_validate(saved_plan),old,p,work)

    def produce(self,jid,rev,token,source,source_hash,plan,old,p,work):
        self.store.transition(jid,rev,token,"RUNNING",old,"title_generation_and_review")
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
        target={"title":plan.title,"presenter":plan.presenter,"style":style}
        title_checks={"title_text","presenter_text","no_extra_text","not_clipped","style_match"}
        rubric="核对标题图片：只允许批准的课名和已核实讲师文字，无系列/制作单位/额外字，无错字裁字，与给定风格描述相符。先读实际文字再比较。每个必需code必须出现一次："+json.dumps(sorted(title_checks))+"；姓名为空时presenter_text应检查无编造姓名。返回 {decision:pass|fail|uncertain,checks:[{code,decision,reason}],issues:[],limits:字符串}。批准目标="+json.dumps(target,ensure_ascii=False)
        references=[self.settings.data_dir/x for x in old.get("style_reference_paths",[])][:1]
        for attempt in range(1 if title_path else 3):
            if not title_path:
                if not style:raise ValueError("source title style evidence missing; upload an approved title image")
                repair="" if attempt==0 else "；上一版复核失败，必须修正："+json.dumps(title_review.model_dump(),ensure_ascii=False)
                title=p.generate_title(plan.title,plan.presenter,style+repair,work/f"title-{attempt}.png",reference_images=references)
            title_review=self.reviewed(p,rubric+"。第一张是生成标题，后续图是原片风格参考，仅参考构图色彩字体，不保留其系列/制作文字。",[title]+references)
            if review_passed(title_review,title_checks) and not title_review.issues:break
        else:
            self.store.transition(jid,rev,token,"NEEDS_REVIEW",{**old,"title_review":title_review.model_dump()},"title image requires correction after bounded attempts")
            return
        old={**old,"plan":plan.model_dump(),"title_review":title_review.model_dump(),"generated_title_path":str(title.relative_to(self.settings.data_dir)) if title.is_relative_to(self.settings.data_dir) else str(title)}
        self.store.transition(jid,rev,token,"RUNNING",old,"rendering")
        render_meta=work/"render-meta.json"
        if render_meta.exists():
            meta=json.loads(render_meta.read_text())
            if meta["plan"]!=plan.model_dump() or meta["title_hash"]!=digest(title):
                raise ValueError("render checkpoint dependencies changed; create a new revision")
            output=work/meta["path"];expected=meta["expected"]
        else:
            output,expected=render(source,intro_path,title,plan,work/"render",self.settings.encoder)
            render_meta.write_text(json.dumps({"plan":plan.model_dump(),"title_hash":digest(title),"expected":expected,"path":str(output.relative_to(work))},ensure_ascii=False,indent=2))
        self.store.transition(jid,rev,token,"RUNNING",{**old,"expected":expected,"render_path":str(output.relative_to(self.settings.data_dir))},"verifying_output")
        technical=verify_output(output,expected)
        audio=verify_audio(source,output,plan)
        self.store.transition(jid,rev,token,"RUNNING",{**old,"expected":expected,"technical":technical,"audio":audio,"render_path":str(output.relative_to(self.settings.data_dir))},"reviewing_export")
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
        head_checks={"intro_identity","title_text","presenter_text","no_extra_text","not_clipped"}
        head=self.reviewed(p,"成片片头标题复核：前两张是实际MP4片头代表/末帧，后三张是1.5秒标题首/中/末帧。文件名逐张标识，不混用帧号。只对标题页检查准确课名和姓名、无额外标题文字、无裁字。姓名为空则检查无编造姓名。批准目标="+json.dumps(target,ensure_ascii=False)+"；必需code，每个一次="+json.dumps(sorted(head_checks))+"。只根据给定图，不声称看过全片或听过原声。",frames[:5])
        body_checks=export_checks-head_checks
        paired=[x for pair in zip(frames[5:],reference) for x in pair]
        body=self.reviewed(p,"成片授课复核：图片按首、中、尾三组排列，每组第一张为实际导出export，第二张为对应source-reference源画面。核对教学内容一致、末尾仍是授课且未留制作名单、无异常黑屏。不要要求本组没有提供的片头/标题。必需code，每个一次="+json.dumps(sorted(body_checks))+"。只根据给定帧，不声称听过音频或看过全片。",paired)
        parts=[head,body]
        review=Review(decision="pass" if review_passed(head,head_checks) and review_passed(body,body_checks) and not any(x.issues for x in parts) else "fail",checks=[c for x in parts for c in x.checks],issues=[i for x in parts for i in x.issues],limits="Separate actual-MP4 intro/title and three source/output pairs; sampled visual review only")
        result={**old,"plan":plan.model_dump(),"expected":expected,"technical":technical,"audio":audio,"title_review":title_review.model_dump(),"export_review":review.model_dump(),"render_path":str(output.relative_to(self.settings.data_dir))}
        if not review_passed(review,export_checks) or review.issues:
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
            (work/"project.json").write_text(json.dumps({"schema_version":"0.2","source_hash":source_hash,"intro_hash":intro_hash,"title_hash":digest(title),"plan":plan.model_dump(),"expected":expected},ensure_ascii=False,indent=2))
            (work/"report.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
            job.state="DELIVERED";job.reason="";job.token=None;job.lease_until=0;job.result=encode(result)
            s.add(Audit(job_id=jid,revision=rev,event="Published verified MP4 from "+("automatic evidence-approved plan" if old.get("automatic_plan") else "explicitly confirmed plan")))
