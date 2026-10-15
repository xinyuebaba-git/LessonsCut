"""Evidence-driven automatic recognition and adjacent-frame cut-point review."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from opencc import OpenCC
from pathlib import Path
from typing import Literal
from pydantic import Field, ValidationError
from sqlalchemy import select
from .domain import StrictModel, ConfirmedPlan, Candidate, Review, EndingHold, review_passed
from .frame_index import FrameIndex
from .asr import LocalASR
from .media import digest
from .store import Asset, Job, Revision, Audit, encode
from .providers import ProviderOutputError


class NeedsReview(ValueError):
    pass


class Observation(StrictModel):
    evidence_id: str
    category: Literal["intro_guangzhou", "intro_other", "lesson_title", "series_title", "presenter", "teaching_slide", "teaching_demo", "credits", "outro", "transition", "other", "uncertain"]
    visible_text: str
    confidence: float = Field(ge=0, le=1)
    reason: str


class Proposal(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    presenter: str | None
    origin: Literal["visible_text", "explicit_speech", "inferred"]
    title_evidence_ids: list[str] = Field(min_length=1)
    presenter_evidence_ids: list[str]
    presenter_status: Literal["verified", "absent", "uncertain"]
    style_evidence_ids: list[str] = Field(min_length=1)
    style_description: str = Field(min_length=1, max_length=1000)
    conflicts: list[str]


class BoundaryRow(StrictModel):
    frame: int
    belongs: bool
    confidence: float = Field(ge=0, le=1)
    reason: str


class ObservationResponse(StrictModel):
    samples: list[Observation]


class BoundaryResponse(StrictModel):
    frames: list[BoundaryRow]


class IndependentFacts(StrictModel):
    title: str
    presenter: str | None
    issues: list[str]


class TitleFact(StrictModel):
    title: str
    issues: list[str]


class PresenterFact(StrictModel):
    presenter: str | None
    issues: list[str]


class PresenterResolution(StrictModel):
    presenter: str | None
    decision: Literal['visible_signatures_agree','review']
    evidence_ids: list[str]
    reason: str = Field(min_length=1,max_length=1000)


class TitleStyle(StrictModel):
    style_evidence_ids: list[str] = Field(min_length=1,max_length=2)
    style_description: str = Field(min_length=1,max_length=1000)


class ClosingChoice(StrictModel):
    decision: Literal['hold','none','review']
    closing_segment_ids: list[str]
    excluded_segment_ids: list[str]
    reason: str = Field(min_length=1,max_length=1000)


def validated_ending(choice,segments,visual_end,source_end,max_hold):
    ids=[s['id'] for s in segments]
    kept=choice.closing_segment_ids
    if choice.decision=='none':
        if kept or choice.excluded_segment_ids!=ids or any(s['start']<visual_end for s in segments):
            raise NeedsReview('outro-only classification crosses visual cut or has invalid coverage')
        return None
    if choice.decision!='hold' or not kept or kept!=ids[:len(kept)] or choice.excluded_segment_ids!=ids[len(kept):]:
        raise NeedsReview('closing speech evidence is uncertain or non-contiguous')
    chosen=segments[:len(kept)]
    if any(s['avg_logprob']< -1 or s['no_speech_prob']>=0.6 or not s['words'] for s in chosen):
        raise NeedsReview('closing speech ASR evidence unreliable')
    if chosen[0]['start']>visual_end+1 or any(b['start']-a['end']>1 for a,b in zip(chosen,chosen[1:])):
        raise NeedsReview('closing speech is not adjacent to lecture')
    audio_end=max(max(s['end'],max(w['end'] for w in s['words'])) for s in chosen)
    if not visual_end<audio_end<=min(source_end,visual_end+max_hold):
        raise NeedsReview('closing speech exceeds authorized hold bounds')
    if choice.excluded_segment_ids and segments[len(kept)]['start']<audio_end-0.001:
        raise NeedsReview('closing and excluded speech overlap')
    return EndingHold(audio_end=audio_end,evidence_ids=kept,reason=choice.reason)


class PreludeCheck(StrictModel):
    prelude_is_teaching: bool
    confidence: float = Field(ge=0,le=1)
    reason: str


def compact(text):
    return re.sub(r"[\s\W_]+", "", OpenCC("t2s").convert(text), flags=re.UNICODE)


def evidence_supports(value, ids, texts):
    return bool(ids) and all(i in texts for i in ids) and any(compact(value) in compact(texts[i]) for i in ids)


def single_transition(frames, states, before, after):
    if len(frames) != len(states) or len(frames) < 2 or frames != sorted(set(frames)):
        raise NeedsReview("boundary response coverage invalid")
    if states[0] != before or states[-1] != after:
        raise NeedsReview("boundary bracket revised; additional evidence required")
    changes = [i for i in range(1,len(states)) if states[i]!=states[i-1]]
    if len(changes)!=1:
        raise NeedsReview("non-monotonic boundary; do not guess a cut")
    i=changes[0]
    return frames[i-1],frames[i]


def end_bracket(rows, teaching):
    """Identity is not temporal role: an intro-like brand AFTER teaching may be an outro.

    This only proposes a bracket; it never approves the final cut.
    """
    last=max((i for i,r in enumerate(rows) if r.category in teaching),default=-1)
    ending={"credits","outro","intro_guangzhou","intro_other"}
    outside=next((i for i in range(last+1,len(rows)) if rows[i].category in ending),None)
    return last,outside


def speech_cut_conflicts(segments,cuts):
    return [{"boundary":name,"cut_seconds":cut,"segment_id":segment["id"],"text":segment["text"],"word":word}
            for segment in segments for word in segment["words"] for name,cut in cuts.items()
            if word["probability"]>=0.5 and word["start"]+0.15<cut<word["end"]-0.15]


class AutomaticAnalysis:
    def __init__(self, settings, store, jid, rev, token, source, provider, work, asr_factory=LocalASR):
        self.cfg,self.store,self.jid,self.rev,self.token=settings,store,jid,rev,token
        self.source,self.p,self.work=Path(source),provider,Path(work)
        self.asr_factory=asr_factory
        self.index=FrameIndex(source,self.work/"frame-pts.json")
        self.evidence={}
        with store.session() as s:
            initial=json.loads(s.get(Job,jid).result)
        self.result={"automation_version":"0.2", "intro_asset_id":initial.get("intro_asset_id"), "approval_policy":initial.get("approval_policy","exceptions_only"), "evidence":[], "observations":[]}
        self.transcript=None

    def save(self, phase):
        self.result["phase"]=phase
        self.result["evidence"]=list(self.evidence.values())
        (self.work/"analysis.json").write_text(json.dumps(self.result,ensure_ascii=False,indent=2))
        self.store.transition(self.jid,self.rev,self.token,"RUNNING",self.result,phase)

    def sample(self, frame):
        eid=f"frame-{frame}"
        if eid not in self.evidence:
            path=self.index.extract(frame,self.work/"frames"/f"{frame:08d}.jpg")
            self.evidence[eid]={"id":eid,"frame":frame,"source_pts":str(self.index.pts[frame]),
                "time_base":str(self.index.time_base),"source_seconds":self.index.timestamp(frame),
                "path":str(path.relative_to(self.cfg.data_dir)),"sha256":digest(path)}
        return self.evidence[eid]

    def paths(self, evidence):
        return [self.cfg.data_dir/e["path"] for e in evidence]

    def typed(self, role, prompt, model, images=()):
        prompt+="\n只输出满足此JSON schema的对象（confidence为数字，不是high等文字）："+encode(model.model_json_schema())
        for attempt in range(3):
            try:
                raw=self.p.chat(role,prompt,images)
                return model.model_validate(raw)
            except (ValidationError,ProviderOutputError) as exc:
                if attempt==2:raise NeedsReview("model schema invalid after bounded repairs") from exc
                prompt+="\n上次结构错误如下，请仅修复格式，不编造证据，字符串中的引号必须转义或改用中文单引号："+str(exc)[:1000]

    def speech_near(self, start, end):
        return [s.model_dump() for s in self.transcript.segments if s.end>=start and s.start<=end]

    def confirm_first_presenter(self, ordered):
        candidates=[r for r in ordered if r.category=="presenter"]
        changed={}
        for offset in range(0,min(len(candidates),32),4):
            rows=candidates[offset:offset+4]
            batch=[self.evidence[r.evidence_id] for r in rows]
            frames=[e["frame"] for e in batch]
            checked=self.typed("recognizer",
                "真人讲解核验：只有画面中确实出现真人身体/脸部、处于本课真实讲解镜头才为true。只有“主讲XXX”文字名单、演员署名卡、绘画/动画/静态照片，都不是首次真人开讲。"
                "逐张核验，不因先前分类叫presenter就预设true。图片帧号="+encode(frames)+
                "；相关ASR="+encode(self.speech_near(max(0,batch[0]["source_seconds"]-2),batch[-1]["source_seconds"]+2))+
                "。返回 {frames:[{frame,belongs:true或false,confidence:数值,reason}]}。",BoundaryResponse,self.paths(batch)).frames
            checked=sorted(checked,key=lambda x:x.frame)
            if [r.frame for r in checked]!=frames or any(r.confidence<0.8 for r in checked):
                raise NeedsReview("first presenter presence evidence uncertain")
            self.result.setdefault("presenter_presence_checks",[]).extend([r.model_dump() for r in checked])
            for row,check in zip(rows,checked):
                if not check.belongs:changed[row.evidence_id]=row.model_copy(update={"category":"other","reason":check.reason})
                else:
                    revised=[changed.get(r.evidence_id,r) for r in ordered]
                    return revised,next(i for i,r in enumerate(revised) if r.evidence_id==row.evidence_id)
        raise NeedsReview("no verified real presenter after contextual checks")

    def closing_ending(self,end):
        cut=end['seconds']
        candidates=[s.model_dump() for s in self.transcript.segments if s.end>cut and s.start>=cut-10]
        if not candidates or candidates[0]['start']>cut+1:return None
        if self.cfg.ending_policy=='review':raise NeedsReview('closing speech requires configured manual review')
        context=[s.model_dump(exclude={'words'}) for s in self.transcript.segments if s.end>=cut-15]
        batch=[self.evidence[i] for i in end['evidence_ids']]
        prompt=('收束语定格判断：用户已批准，片尾画面开始但本课收束语未完时，定格最后授课帧，保留完整收束原声后结束。'
                '逐条区分本课教学/收束（包括道别感谢）与下一课预告、制作宣传等片尾语音；后者删除。不能因为同一讲师继续说就把预告算本课收束。'
                '只选择候选ID，不改写文字或猜时间。closing_segment_ids必须为从第一条开始的连续前缀，其余逐条列入excluded_segment_ids；混合语义无法分开、声音类别无法确认或需要跳段则review。'
                '若候选全部是片尾/预告且没有本课收束语，decision用none，保留列表为空，排除列表覆盖全部候选；不需要定格。'
                '这是独立读取，尚未给另一模型结论。返回 {decision:hold|none|review,closing_segment_ids:[],excluded_segment_ids:[],reason}。'
                '视觉教学终点='+str(cut)+'；候选='+encode(candidates)+'；上下文='+encode(context)+'；相邻原帧='+encode([{k:e[k] for k in ('id','source_seconds')} for e in batch]))
        local=self.typed('recognizer',prompt,ClosingChoice,self.paths(batch))
        cloud=self.typed('supervisor',prompt,ClosingChoice,self.paths(batch))
        self.result['ending_recognition']={'local':local.model_dump(),'independent_cloud':cloud.model_dump(),'candidates':candidates}
        if local.decision!=cloud.decision or local.closing_segment_ids!=cloud.closing_segment_ids or local.excluded_segment_ids!=cloud.excluded_segment_ids:
            raise NeedsReview('independent closing speech classification disagrees')
        ending=validated_ending(local,candidates,cut,min(self.index.end_seconds,self.transcript.duration),self.cfg.max_ending_hold_seconds)
        if not ending:return None
        self.result['ending_proposal']=ending.model_dump()
        self.result['ending_policy']='freeze_for_closing_speech'
        return ending

    def classify(self, frame_numbers):
        batch=[self.sample(f) for f in frame_numbers]
        prompt=("逐张识别本课源画面，不把系列名当具体课名，不把课件/演示当片尾。人物必须是真实主讲人进入讲解镜头；片头预告不算。"
                "按列表每张返回一次，不漏不重。输出 {samples:[{evidence_id,category,visible_text,confidence,reason}]}。"
                "category只能为intro_guangzhou,intro_other,lesson_title,series_title,presenter,teaching_slide,teaching_demo,credits,outro,transition,other,uncertain。"
                "confidence必须是0到1的JSON数字，例如0.95，不能用high/medium/low等字符串。"
                "visible_text按实际可见文字记录，含署名；无法辨认不要猜。列表="+encode([{k:e[k] for k in ("id","frame","source_seconds")} for e in batch]))
        rows=self.typed("recognizer",prompt,ObservationResponse,self.paths(batch)).samples
        if sorted(r.evidence_id for r in rows)!=sorted(e["id"] for e in batch):
            raise NeedsReview("coarse classification sample coverage mismatch")
        return rows

    def refine(self, name, lo, hi, reference):
        before,after=(False,True) if name=="start" else (True,False)
        predicate=("是否已经进入真实主讲人首次讲解镜头，淡入一开始有主讲人即为true；之前片头/标题/制作名单为false。" if name=="start" else
                   "是否仍属于本次连续授课（人物或教学课件/演示，含收束语），尚未进入致谢卡/制作名单/片尾；片尾动画第一帧即false。不要把授课课件判为片尾。")
        reference_e=self.sample(reference)
        for iteration in range(10):
            if hi-lo<=1:break
            frames=sorted(set([lo,hi]+[round(lo+(hi-lo)*i/7) for i in range(1,7)]))
            batch=[self.sample(f) for f in frames]
            context=self.speech_near(max(0,self.index.timestamp(lo)-3),self.index.timestamp(hi)+3)
            prompt=("这是逐帧切点精查。"+predicate+"最后一张是明确属于目标授课的参考图，不另返回它。前面图片顺序="+encode(frames)+
                    "。只输出 {frames:[{frame:整数,belongs:true或false,confidence:0到1,reason:字符串}]}，每帧一次。"
                    "本地ASR语音上下文仅辅助，不能用它编造图像中没有的内容："+encode(context))
            rows=sorted(self.typed("recognizer",prompt,BoundaryResponse,self.paths(batch)+self.paths([reference_e])).frames,key=lambda r:r.frame)
            if [r.frame for r in rows]!=frames or any(r.confidence<0.8 for r in rows):
                raise NeedsReview("uncertain or incomplete fine boundary labels")
            lo,hi=single_transition(frames,[r.belongs for r in rows],before,after)
            self.result.setdefault("refinement",[]).append({"boundary":name,"iteration":iteration,"samples":[r.model_dump() for r in rows]})
            self.save("refining_"+name)
        if hi-lo!=1:raise NeedsReview("fine boundary did not converge")
        # Independently check both adjacent frames again; never just trust a binary-search result.
        frames=list(range(max(0,hi-2),min(len(self.index.pts),hi+2)))
        batch=[self.sample(f) for f in frames]
        rows=sorted(self.typed("recognizer",
             "独立复核以下相邻原片帧。"+predicate+"图片帧号="+encode(frames)+
             "。返回 {frames:[{frame,belongs,confidence,reason}]}，不要因排序预设结论。",BoundaryResponse,self.paths(batch)).frames,key=lambda r:r.frame)
        if [r.frame for r in rows]!=frames or any(r.confidence<0.8 for r in rows):
            raise NeedsReview("adjacent-frame confirmation uncertain")
        pair=single_transition(frames,[r.belongs for r in rows],before,after)
        if pair!=(hi-1,hi):raise NeedsReview("fine and adjacent-frame boundary disagree")
        return {"first_after_frame":hi,"last_before_frame":hi-1,"seconds":self.index.timestamp(hi),
                "source_pts":str(self.index.pts[hi]),"time_base":str(self.index.time_base),
                "evidence_ids":[e["id"] for e in batch]}

    def run(self, source_asset_id):
        pool=None
        try:
            if self.cfg.asr_backend=="disabled":raise NeedsReview("local ASR not configured")
            self.save("recognition_and_local_asr")
            pool=ThreadPoolExecutor(max_workers=1)
            speech_future=pool.submit(self.asr_factory(self.cfg,self.store,self.jid,self.rev).transcribe,self.source,self.work)
            duration=self.index.end_seconds
            seconds=sorted(set(list(range(0,min(62,int(duration)),2))+list(range(max(0,int(duration)-60),int(duration),2))+list(range(60,max(60,int(duration)-60),30))+[duration-0.1]))
            frames=sorted({self.index.nearest(t) for t in seconds})
            rows=[]
            for i in range(0,len(frames),4):
                rows.extend(self.classify(frames[i:i+4]))
                self.result["observations"]=[r.model_dump() for r in rows]
                self.save("coarse_recognition")
            self.save("waiting_local_asr")
            self.transcript=speech_future.result()
            self.result["transcript"]=self.transcript.model_dump()
            teaching={"presenter","teaching_slide","teaching_demo"}
            ordered=sorted(rows,key=lambda r:self.evidence[r.evidence_id]["frame"])
            self.result["coarse_observations"]=[r.model_dump() for r in ordered]
            ordered,first=self.confirm_first_presenter(ordered)
            if first==0:raise NeedsReview("source begins with presenter; boundary exception review required")
            early=[r for r in ordered[:first] if r.category in ("teaching_slide","teaching_demo")]
            if early:
                context=early[:7]+[ordered[first]]
                batch=[self.evidence[r.evidence_id] for r in context]
                check=self.typed("recognizer",
                    "这些图在正式真人开场之前，最后一张为首次真人讲解镜头。判断之前看起来像教学的画面实际是正式教学，还是栏目包装/预告/标题动画。不能只因出现电商/绘画等元素就当正式教学，也不能只因没有声音就删除真实无声示范。结合实际图像、顺序和ASR上下文判断。"
                    "返回 {prelude_is_teaching:true或false,confidence:0到1数字,reason:解释}。图片="+encode([{k:e[k] for k in ('id','source_seconds')} for e in batch])+
                    "；ASR="+encode(self.speech_near(0,self.evidence[ordered[first].evidence_id]["source_seconds"]+5)),PreludeCheck,self.paths(batch))
                self.result["prelude_check"]=check.model_dump()
                if check.prelude_is_teaching or check.confidence<0.8:raise NeedsReview("teaching before presenter requires start exception review")
                ids={r.evidence_id for r in early}
                ordered=[r.model_copy(update={"category":"other"}) if r.evidence_id in ids else r for r in ordered]
            self.result["observations"]=[r.model_dump() for r in ordered]
            self.save("contextual_sequence_confirmed")
            last,outside=end_bracket(ordered,teaching)
            if last<first or outside is None:raise NeedsReview("no confirmed teaching-to-outro end bracket")
            start=self.refine("start",self.evidence[ordered[first-1].evidence_id]["frame"],self.evidence[ordered[first].evidence_id]["frame"],self.evidence[ordered[first].evidence_id]["frame"])
            end=self.refine("end",self.evidence[ordered[last].evidence_id]["frame"],self.evidence[ordered[outside].evidence_id]["frame"],self.evidence[ordered[last].evidence_id]["frame"])
            if start["first_after_frame"]>=end["first_after_frame"]:raise NeedsReview("invalid refined lecture interval")
            self.result["boundaries"]={"start":start,"end":end}
            self.result['candidate_source_frame_range']=[start['first_after_frame'],end['first_after_frame']]
            observations=[r.model_dump() for r in ordered]
            speech=[s.model_dump(exclude={"words"}) for s in self.transcript.segments]
            proposal=self.typed("recognizer",
                "从实际OCR和本地语音转写选择本节课具体标题和讲师，不留系列、课程编号或制作单位。优先明确本节课名，讲师明确说“本节课讲/今天说”可作为直接标题，不能用更宽泛的PPT分类名取代。ASR可能错字，姓名优先本节首次真实讲解镜头中的清晰讲师署名；系列主讲名单不是本节课署名，后续采访嘉宾不能覆盖本节署名。角色不清必须uncertain。"
                "只返回 {title,presenter:null或姓名,origin:visible_text|explicit_speech|inferred,title_evidence_ids:[],presenter_evidence_ids:[],presenter_status:verified|absent|uncertain,style_evidence_ids:[],style_description,conflicts:[]}。"
                "证据ID必须来自输入。style_evidence_ids选原片标题动画/标题页样式，可是含系列名的标题页，但不能把其中系列文字复制成课名。不能编造姓名。截图观察="+encode(observations)+"；ASR="+encode(speech),Proposal)
            texts={r.evidence_id:r.visible_text for r in ordered}
            texts.update({s.id:s.text for s in self.transcript.segments if s.avg_logprob>=-1 and s.no_speech_prob<0.6})
            self.result["proposal"]=proposal.model_dump()
            if proposal.origin=="inferred" or proposal.conflicts or proposal.presenter_status=="uncertain":
                raise NeedsReview("title/presenter requires inference or has conflicting evidence")
            if not evidence_supports(proposal.title,proposal.title_evidence_ids,texts):
                raise NeedsReview("title is not a direct substring of its evidence")
            if proposal.origin=="explicit_speech" and not any(i.startswith("asr-") for i in proposal.title_evidence_ids):
                raise NeedsReview("spoken title lacks ASR evidence")
            if proposal.origin=="visible_text" and not evidence_supports(proposal.title,[i for i in proposal.title_evidence_ids if i.startswith("frame-")],texts):
                raise NeedsReview("visible title lacks matching OCR evidence")
            if proposal.presenter and (proposal.presenter_status!="verified" or not evidence_supports(proposal.presenter,proposal.presenter_evidence_ids,texts)):
                raise NeedsReview("presenter not directly supported")
            if proposal.presenter_status=="verified" and not proposal.presenter:
                raise NeedsReview("verified presenter missing name")
            if not all(i in self.evidence for i in proposal.style_evidence_ids):
                raise NeedsReview("style source evidence missing")
            # Course-content slides do not replace the original opening title design.
            opening_titles=[r for r in ordered[:first] if r.category in {"lesson_title","series_title"}]
            opening_ids={r.evidence_id for r in opening_titles}
            if opening_titles and not set(proposal.style_evidence_ids)<=opening_ids:
                style_batch=[self.evidence[r.evidence_id] for r in opening_titles[:6]]
                chosen=self.typed("recognizer","标题风格独立选择：以下是正式真人授课之前的原片标题候选。选文字/设计已完整出现、适合模仿构图配色字体的一张，不用授课PPT替代原片标题页。只描述视觉风格；生成时全部原文字会由批准课名/姓名替换。返回 {style_evidence_ids:[],style_description}。候选="+encode([{k:e[k] for k in ('id','source_seconds')} for e in style_batch]),TitleStyle,self.paths(style_batch))
                if not set(chosen.style_evidence_ids)<={e['id'] for e in style_batch}:raise NeedsReview('opening title style evidence invalid')
                proposal=proposal.model_copy(update=chosen.model_dump())
                self.result['proposal']=proposal.model_dump()
            self.result["candidate"]=Candidate(title=proposal.title,presenter=proposal.presenter,origin=proposal.origin,
                lecture={"start":start["seconds"],"end":end["seconds"]},style_description=proposal.style_description,
                evidence_ids=proposal.title_evidence_ids,uncertainties=[]).model_dump()
            self.save("independent_supervision")
            relevant=[s.model_dump(exclude={"words"}) for s in self.transcript.segments if s.start<90 or s.end>duration-40]
            title_ids=list(dict.fromkeys(proposal.style_evidence_ids+proposal.title_evidence_ids))
            batch=[self.evidence[i] for i in title_ids if i in self.evidence][:4]
            title_fact=self.typed("supervisor",
                "独立课名读取：尚未提供本地结论。从原片证据提取本节具体课名，删除课号/系列/制作单位。明确‘今天讲/本节课说’的具体课名优先于更宽泛PPT分类。系列、分类与本课具体课名是不同层级，不同本身不算冲突；同层级的两个明确本课标题互斥才是冲突。若需要概括或无法直接找到才写issues。只返回 {title,issues:[]}，不要判断本组没有提供的姓名或切点。图片="+encode([{k:e[k] for k in ('id','source_seconds')} for e in batch])+"；ASR="+encode(relevant),TitleFact,self.paths(batch))
            batch=[self.evidence[i] for i in proposal.presenter_evidence_ids if i in self.evidence][:3]
            name_fact=self.typed("supervisor",
                "独立姓名读取：尚未提供本地姓名结论。只读取本课首次真实讲解镜头中的清晰讲师署名，按实际中文字形，不猜姓氏，不用语音近音改写画面署名。系列主讲名单/后续嘉宾不代替本课署名。无可读署名且语音不能确定时写issues。讲师任职单位可与片头学校不同，无需验证所属机构。只返回 {presenter:姓名或null,issues:[]}，不判断课名或切点。图片="+encode([{k:e[k] for k in ('id','source_seconds')} for e in batch])+"；开场语音="+encode([s for s in relevant if s['start']<60]),PresenterFact,self.paths(batch))
            blind={"title":title_fact.title,"presenter":name_fact.presenter,"issues":title_fact.issues+name_fact.issues}
            self.result["blind_facts"]=blind
            if blind.get("issues") or compact(blind.get("title", ""))!=compact(proposal.title) or compact(blind.get("presenter") or "")!=compact(proposal.presenter or ""):
                raise NeedsReview("independent cloud facts disagree with local recognition")
            asr_name_ids=[i for i in proposal.presenter_evidence_ids if i.startswith('asr-')]
            if proposal.presenter and asr_name_ids and not evidence_supports(proposal.presenter,asr_name_ids,texts):
                # Add another actual source signature before resolving a transcription spelling difference.
                signature_ids=[r.evidence_id for r in ordered if r.category=='presenter' and self.evidence[r.evidence_id]['source_seconds']<start['seconds']+30 and compact(proposal.presenter) in compact(r.visible_text)][:3]
                if len(signature_ids)<2:raise NeedsReview('ASR/name spelling discrepancy needs additional visible signatures')
                signatures=[self.evidence[i] for i in signature_ids]
                prompt=('姓名证据补充核验：这些原片画面为同一本课开场的多个署名时间位置。独立逐张读取清晰署名，尚未给目标姓名。ASR是模型转写，不是原音频中文字形的证明；转写用字可能错误，不能反向改写清晰画面署名。'
                        '如果各画面署名清晰一致，可依据画面记录姓名及采纳理由，保留原ASR不改写；画面之间冲突、角色不明或无法辨认必须review。'
                        '返回 {presenter:null或姓名,decision:visible_signatures_agree|review,evidence_ids:实际清晰署名图ID列表,reason:逐项说明画面与ASR差异及采纳依据}。'
                        '图片列表='+encode([{k:e[k] for k in ('id','source_seconds')} for e in signatures])+'；相关原ASR='+encode({i:texts[i] for i in asr_name_ids}))
                local=self.typed('recognizer',prompt,PresenterResolution,self.paths(signatures))
                cloud=self.typed('supervisor',prompt,PresenterResolution,self.paths(signatures))
                self.result['presenter_resolution']={'local':local.model_dump(),'independent_cloud':cloud.model_dump(),'asr_original_ids':asr_name_ids,'asr_original_text':{i:texts[i] for i in asr_name_ids}}
                for resolution in (local,cloud):
                    if resolution.decision!='visible_signatures_agree' or compact(resolution.presenter or '')!=compact(proposal.presenter) or len(set(resolution.evidence_ids))<2 or not set(resolution.evidence_ids)<=set(signature_ids):
                        raise NeedsReview('additional signature evidence did not resolve name discrepancy')
                proposal=proposal.model_copy(update={'presenter_evidence_ids':signature_ids})
                self.result['proposal']=proposal.model_dump()
            with self.store.session() as s:
                j=s.get(Job,self.jid);intro_id=json.loads(j.result).get("intro_asset_id") or self.cfg.default_intro_asset_id
                intro=s.get(Asset,intro_id) if intro_id else None
                if not intro or intro.kind!="video":raise NeedsReview("approved reference intro not configured")
                intro_path,intro_hash=intro.path,intro.sha256
            if digest(intro_path)!=intro_hash:raise NeedsReview("registered intro changed")
            intro_index=FrameIndex(intro_path,self.work/"intro-pts.json")
            ref_intro=intro_index.extract(intro_index.nearest(2),self.work/"intro-reference.jpg")
            required={"intro_identity","title_evidence","presenter_evidence","start_boundary","end_boundary","continuous_teaching","speech_context"}
            boundary_speech=self.speech_near(max(0,start["seconds"]-3),start["seconds"]+5)+self.speech_near(max(0,end["seconds"]-5),end["seconds"]+3)
            conflicts=speech_cut_conflicts(boundary_speech,{"start":start["seconds"],"end":end["seconds"]})
            if conflicts:
                self.result['audio_boundary_conflicts']=conflicts
                if any(c['boundary']=='start' for c in conflicts):raise NeedsReview("high-confidence ASR word crosses proposed start boundary")
            ending=self.closing_ending(end)
            if conflicts and not ending:raise NeedsReview("high-confidence ASR word crosses proposed boundary")
            if ending:required.add('closing_speech')
            parts=[]
            for name,boundary in [("start",start),("end",end)]:
                boundary_batch=[self.evidence[i] for i in boundary["evidence_ids"]]
                code=name+"_boundary"
                criterion=("前两张应尚未进入真人授课，后两张已进入真人授课；从淡入开始出现真人的第一帧计入。只有主讲文字卡不算真人。" if name=="start" else
                           "前两张仍是有效授课/教学课件，后两张开始片尾/署名/品牌卡。片尾品牌卡可能与片头相同，不因写学校名就认作仍在授课。")
                part=self.typed("supervisor","切点独立复核，仅检查这一处相邻帧，不检查标题、姓名或其他未提供画面。"+criterion+
                    "每张图的文件名即源帧编号，图片顺序列表="+encode([{k:e[k] for k in ('id','frame','source_seconds')} for e in boundary_batch])+
                    "；候选第一变化帧="+str(boundary["first_after_frame"])+"；相关语音="+encode(self.speech_near(max(0,boundary["seconds"]-5),boundary["seconds"]+5))+
                    "。只返回这一个code的检查："+code+"；若看不到图片或无法判断，必须uncertain，不能编造未提供帧。reason用简短中文，不含未转义双引号。",Review,self.paths(boundary_batch))
                if not review_passed(part,{code}) or part.issues:
                    self.result["boundary_cloud_reviews"]=[x.model_dump() for x in parts]+[part.model_dump()]
                    raise NeedsReview("independent "+name+" boundary review failed")
                parts.append(part)
            fact_ids=list(dict.fromkeys(proposal.style_evidence_ids+proposal.presenter_evidence_ids+proposal.title_evidence_ids))
            fact_batch=[self.evidence[i] for i in fact_ids if i in self.evidence][:5]
            fact_required=required-{"start_boundary","end_boundary"}
            part=self.typed("supervisor",
                "这是已独立读取事实、且起止相邻帧已单独审查后的计划核对。最后一张是已登记的广州开放大学片头参考，之前是原片标题/讲师署名证据。图号="+encode([{k:e[k] for k in ('id','frame','source_seconds')} for e in fact_batch])+
                "。批准规则：清晰字幕/画面署名就是直接姓名证据，不要求另一独立来源；讲师任职单位可与片头学校不同，单位不同不构成姓名冲突。课名来自本课明确文字或口头说明，不能只因与系列主题相符就认为有依据。"
                +('ASR姓名用字差异已补充多个实际署名帧并独立核验，采纳依据='+encode(self.result['presenter_resolution'])+'。保留原转写但不把识别器选字当作原音频自报另一名字的证明。请检查补充依据是否成立；若实际画面仍冲突则fail。' if self.result.get('presenter_resolution') else '')+
                "计划="+encode({"title":proposal.title,"presenter":proposal.presenter,"origin":proposal.origin,"source_continuous_range":[start['seconds'],end['seconds']]})+
                ('；已批准定格策略='+encode(ending.model_dump())+'，画面在视觉终点切除片尾，最后授课帧延续至audio_end；语音从授课起点连续保留至该时间。closing_speech检查须保留本课完整收束、排除下一课预告。独立分类结果='+encode(self.result['ending_recognition']) if ending else '')+
                "；对应课名/姓名直接证据="+encode({i:texts[i] for i in proposal.title_evidence_ids+proposal.presenter_evidence_ids if i in texts})+
                "；语音上下文="+encode(boundary_speech)+"。连续授课检查指计划保持单一源区间、未删中间内容，不声称逐帧识别整片。"
                "只检查以下必需code，每个一次="+encode(sorted(fact_required))+"。返回严格JSON的Review对象，reason不要用未转义双引号。",Review,self.paths(fact_batch)+[ref_intro])
            parts.append(part)
            review=Review(decision="pass" if all(review_passed(x,{c.code for c in x.checks}) and not x.issues for x in parts) else "fail",
                          checks=[c for x in parts for c in x.checks],issues=[i for x in parts for i in x.issues],limits="Separate start/end/fact samples; local ASR text evidence only, no cloud audio listening or all-frame semantic review")
            self.result["boundary_cloud_reviews"]=[x.model_dump() for x in parts[:2]]
            self.result["plan_review"]=review.model_dump()
            if not review_passed(review,required) or review.issues:
                raise NeedsReview("refined plan did not pass every supervision check")
            plan=ConfirmedPlan(title=proposal.title,presenter=proposal.presenter,lecture={"start":start["seconds"],"end":end["seconds"]},
                ending=ending,intro_asset_id=intro_id,fps=self.index.fps,reason="Automatic: direct title/name evidence, local word-timestamp ASR, adjacent-frame cuts and independent supervision passed")
            self.result["style_reference_paths"]=[self.evidence[i]["path"] for i in proposal.style_evidence_ids[:1]]
            self.result["automatic_plan"]=plan.model_dump()
            self.result["source_frame_range"]=[start["first_after_frame"],end["first_after_frame"]]
            self.save("plan_validated")
            if not self.cfg.automatic_plan_approval or self.result["approval_policy"]=="always":raise NeedsReview("batch requires explicit plan approval")
            with self.store.session() as s:
                j=s.scalar(select(Job).where(Job.id==self.jid).with_for_update())
                if j.token!=self.token or j.revision!=self.rev:raise NeedsReview("stale automatic plan revision")
                s.add(Revision(job_id=self.jid,number=self.rev,plan=encode(plan.model_dump())))
                s.add(Audit(job_id=self.jid,revision=self.rev,event="Automatic plan accepted from independent evidence checks"))
                j.stage="render"
            return plan,self.result
        except Exception as exc:
            self.result["exception"]={"type":type(exc).__name__,"message":str(exc)[:1200]}
            self.save("exception_requires_review")
            raise
        finally:
            if pool:pool.shutdown(wait=True)
