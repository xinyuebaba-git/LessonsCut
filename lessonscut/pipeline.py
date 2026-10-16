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
from .asr import LocalASR
from .media import probe,extract_frame,render,verify_output,verify_audio,verify_hold,digest,publish,run


class Pipeline:
    def __init__(self,settings,store,providers_factory=Providers):
        self.settings,self.store,self.providers_factory=settings,store,providers_factory

    def reviewed(self,p,prompt,images,role='supervisor'):
        prompt+='\n返回严格JSON，字符串中的引号必须转义；结构='+json.dumps(Review.model_json_schema(),ensure_ascii=False)
        for attempt in range(3):
            try:return Review.model_validate(p.chat(role,prompt,images))
            except (ProviderOutputError,ValidationError) as exc:
                if attempt==2:raise ValueError('review format invalid after bounded repairs') from exc
                prompt+='\n上一答复格式错误，仅修正格式、不改变证据和判断：'+str(exc)[:1000]

    def execute(self,job_id,revision,token):
        from .profiles import Profiles
        with self.store.session() as s:
            metadata=json.loads(s.get(Job,job_id).result)
        cfg=Profiles(self.settings,self.store).effective(metadata['config_version']) if metadata.get('config_version') else self.settings
        # Separate instance per claimed revision; no process-wide mutable settings.
        return Pipeline(cfg,self.store,self.providers_factory)._execute(job_id,revision,token)

    def _execute(self,job_id,revision,token):
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
        try:
            if stage=="analyze":
                plan,analysis=AutomaticAnalysis(self.settings,self.store,job_id,revision,token,Path(source_path),p,work).run(job.asset_id)
                self.produce(job_id,revision,token,Path(source_path),source_hash,plan,analysis,p,work)
            else:
                self.produce(job_id,revision,token,Path(source_path),source_hash,ConfirmedPlan.model_validate(saved_plan),old,p,work)
        finally:
            client=getattr(p,'client',None)
            if client is not None:client.close()

    def check_ending(self,jid,rev,source,output,plan,old,p,work,expected):
        if not plan.ending:return None
        from .frame_index import FrameIndex
        mechanical=verify_hold(output,expected)
        folder=work/'ending-review';folder.mkdir(exist_ok=True)
        fps=float(Fraction(plan.fps));offset=sum(expected['segment_frames'][:2])/fps
        begin=offset+plan.lecture.end-plan.lecture.start-3
        clip=folder/'actual-ending.wav'
        if not clip.exists():
            run(['ffmpeg','-v','error','-i',str(output),'-ss',str(max(offset,begin)),'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le','-n',str(clip)])
        actual=LocalASR(self.settings,self.store,jid,rev).transcribe(clip,folder/'asr')
        source_index=FrameIndex(source,work/'frame-pts.json')
        ref=source_index.extract(expected['source_frame_range'][1]-1,folder/'last-teaching.jpg')
        frames=[ref]
        for i,n in enumerate(mechanical['sampled_output_frames'][1:]):
            frames.append(extract_frame(output,n/fps,folder/f'held-{i}.jpg'))
        original=old.get('transcript',{}).get('segments',[])
        selected=[s for s in original if s['id'] in plan.ending.evidence_ids]
        excluded=old.get('ending_recognition',{}).get('local',{}).get('excluded_segment_ids',[])
        excluded=[s for s in original if s['id'] in excluded]
        codes={'closing_complete','outro_speech_removed','held_frame'}
        prompt=('定格收束语成片复核：第一张为原片最后一帧有效授课，后面为实际导出定格首/中/末样本。用户批准只定格该帧并保留本课完整收束语，删除后续预告。'
                '批准保留的源ASR='+json.dumps(selected,ensure_ascii=False)+'；应删除的源片尾语音='+json.dumps(excluded,ensure_ascii=False)+
                '；对实际MP4末尾重新本地ASR的结果='+actual.model_dump_json()+'；计划='+plan.ending.model_dump_json()+
                '。根据转写证据检查收束文字完整、未混入应删预告；根据实际图片核对末帧一致且无片尾画面。只读ASR文字，不声称听过音频；近音错字须谨慎判断，无法确认则uncertain。必需code每个一次='+json.dumps(sorted(codes)))
        # Both roles inspect the same actual output evidence; no local pass conclusion is provided.
        prompt+='\n严格JSON结构='+json.dumps(Review.model_json_schema(),ensure_ascii=False)
        local=self.reviewed(p,prompt,frames,role='recognizer')
        cloud=self.reviewed(p,prompt,frames)
        return {'mechanical':mechanical,'actual_transcript':actual.model_dump(),'local_review':local.model_dump(),'cloud_review':cloud.model_dump(),
                'passed':review_passed(local,codes) and not local.issues and review_passed(cloud,codes) and not cloud.issues}

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
        if not style and self.settings.title_style_fallback:
            style=self.settings.title_fallback_style
            old={**old,'title_style_source':'configured_template'}
        if title_path:
            if not title or title.kind!="image" or digest(title_path)!=title_hash:
                raise ValueError("registered title image invalid or changed")
            title=Path(title_path)
        target={"title":plan.title,"presenter":plan.presenter,"style":style}
        title_checks={"title_text","presenter_text","no_extra_text","not_clipped","style_match"}
        text_checks=title_checks-{'style_match'}
        presenter_rule=('姓名必须准确为'+plan.presenter+'，本任务存在已确认姓名。' if plan.presenter else '本任务没有已确认姓名，不能出现编造姓名。')
        rubric="核对标题图片：本组只提供实际成品标题图。只允许批准的课名和已核实讲师文字，无系列/制作单位/额外字，无错字裁字。先读实际文字再比较，不猜未提供的参考图文字。每个必需code必须出现一次："+json.dumps(sorted(text_checks))+"；"+presenter_rule+"返回 {decision:pass|fail|uncertain,checks:[{code,decision,reason}],issues:[],limits:字符串}。批准目标="+json.dumps({k:target[k] for k in ('title','presenter')},ensure_ascii=False)
        references=[self.settings.data_dir/x for x in old.get("style_reference_paths",[])][:1]
        for attempt in range(1 if title_path else 3):
            if not title_path:
                if not style:raise ValueError("source title style evidence missing; confirm style or enable the configured fallback template")
                repair="" if attempt==0 else "；上一版复核失败，必须修正："+json.dumps(title_review.model_dump(),ensure_ascii=False)
                title=p.generate_title(plan.title,plan.presenter,style+repair,work/f"title-{attempt}.png",reference_images=references)
            content_parts=[self.reviewed(p,rubric,[title],role=role) for role in ('recognizer','supervisor')]
            content_ok=all(review_passed(x,text_checks) and not x.issues for x in content_parts)
            if content_ok:
                style_review=self.reviewed(p,'核对标题风格：第一张是成品标题，其余是原片风格参考。只比较构图、配色与字体风格，参考图的原系列/制作文字正是要删除的内容，不属于成品错误。本组不重新审标题文字。风格描述='+style+'；必需code仅style_match，一个检查。',[title]+references)
                title_review=Review(decision='pass' if review_passed(style_review,{'style_match'}) and not style_review.issues else 'fail',checks=content_parts[1].checks+style_review.checks,issues=style_review.issues,limits='Both models read generated image alone; style comparison separately uses original reference')
            else:
                failed=next(x for x in content_parts if not review_passed(x,text_checks) or x.issues)
                title_review=failed
            (work/f'title-review-{attempt}.json').write_text(json.dumps({'local_text':content_parts[0].model_dump(),'cloud_text':content_parts[1].model_dump(),'combined':title_review.model_dump()},ensure_ascii=False,indent=2))
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
        reusable=old.get('expected')==expected and old.get('technical',{}).get('full_decode') and old['technical'].get('sha256')==digest(output) and len(old.get('audio',[]))==3 and (not plan.ending or old.get('ending_review',{}).get('passed'))
        technical=old['technical'] if reusable else verify_output(output,expected)
        audio=old['audio'] if reusable else verify_audio(source,output,plan)
        ending_review=old.get('ending_review') if reusable else self.check_ending(jid,rev,source,output,plan,old,p,work,expected)
        if ending_review and not ending_review['passed']:
            self.store.transition(jid,rev,token,'NEEDS_REVIEW',{**old,'expected':expected,'technical':technical,'audio':audio,'ending_review':ending_review},'held closing speech output requires correction')
            return
        if ending_review:old={**old,'ending_review':ending_review}
        self.store.transition(jid,rev,token,"RUNNING",{**old,"expected":expected,"technical":technical,"audio":audio,"render_path":str(output.relative_to(self.settings.data_dir))},"reviewing_export")
        duration=technical["duration"]
        fps=float(Fraction(plan.fps))
        intro_seconds=expected["segment_frames"][0]/fps
        lecture_offset=sum(expected["segment_frames"][:2])/fps
        frames=[]
        for i,t in enumerate([intro_seconds/2,intro_seconds-1/fps,intro_seconds,(intro_seconds+lecture_offset)/2,lecture_offset-1/fps,lecture_offset,min(duration-0.1,lecture_offset+(plan.lecture.end-plan.lecture.start)/2),duration-0.1]):
            frames.append(extract_frame(output,max(0,t),work/f"export-{i}.jpg"))
        reference=[]
        from .frame_index import FrameIndex
        source_index=FrameIndex(source,work/'frame-pts.json')
        a,b=expected['source_frame_range']
        for i,n in enumerate([a,(a+b)//2,b-1]):
            reference.append(source_index.extract(n,work/f"source-reference-{i}.jpg"))
        export_checks={"intro_identity","title_text","presenter_text","no_extra_text","not_clipped","lecture_start","lecture_body","lecture_end","no_black_frame"}
        head_checks={"intro_identity","title_text","presenter_text","no_extra_text","not_clipped"}
        intro_review=self.reviewed(p,'成片片头身份复核：仅这两张实际MP4片头代表/末帧，检查广州开放大学身份。必需code仅intro_identity一次，不检查没有提供的标题姓名。',frames[:2])
        actual_title_prompt='成片标题文字复核：仅提供实际MP4的1.5秒标题首中末帧，不含片头或风格参考。'+presenter_rule+'批准文字='+json.dumps({k:target[k] for k in ('title','presenter')},ensure_ascii=False)+'；只检查必需code每个一次='+json.dumps(sorted(text_checks))+'。决定与理由必须一致，不存在问题则pass；只基于提供帧，不声称听过音频。'
        actual_local=self.reviewed(p,actual_title_prompt,frames[2:5],role='recognizer')
        actual_cloud=self.reviewed(p,actual_title_prompt,frames[2:5])
        head_parts=[intro_review,actual_cloud]
        head=Review(decision='pass' if review_passed(intro_review,{'intro_identity'}) and review_passed(actual_local,text_checks) and review_passed(actual_cloud,text_checks) and not any(x.issues for x in [intro_review,actual_local,actual_cloud]) else 'fail',checks=[c for x in head_parts for c in x.checks],issues=[i for x in [intro_review,actual_local,actual_cloud] for i in x.issues],limits='Intro and actual title checked separately; both models inspect actual title samples')
        old={**old,'export_title_local':actual_local.model_dump()}
        body_checks=export_checks-head_checks
        paired=[x for pair in zip(frames[5:],reference) for x in pair]
        body=self.reviewed(p,"成片授课复核：图片按首、中、尾三组排列，每组第一张为实际导出export，第二张为对应source-reference源画面。核对教学内容一致、末尾仍是授课且未留制作名单、无异常黑屏。"+('本片末尾按批准策略定格最后授课帧至收束语结束，尾组应与最后授课帧一致，定格本身不算异常。' if plan.ending else '')+"不要要求本组没有提供的片头/标题。必需code，每个一次="+json.dumps(sorted(body_checks))+"。只根据给定帧，不声称听过音频或看过全片。",paired)
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
