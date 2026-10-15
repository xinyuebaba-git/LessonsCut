"""Deterministic media operations. No content recognition happens here."""
import hashlib
import json
import os
import subprocess
from fractions import Fraction
from pathlib import Path
import numpy as np
from .domain import frame_count


def digest(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def authorized_path(root, relative):
    root=Path(root).resolve()
    candidate=(root / relative).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ValueError("file outside authorized root or not found")
    return candidate


def run(args, timeout=1800):
    p=subprocess.run(args, capture_output=True, timeout=timeout)
    if p.returncode:
        raise ValueError("media tool failed: "+p.stderr.decode(errors="replace")[-1200:])
    return p.stdout


def probe(path):
    return json.loads(run(["ffprobe","-v","error","-show_streams","-show_format","-of","json",str(path)],60))


def video_info(path):
    info=probe(path)
    v=next((s for s in info["streams"] if s["codec_type"]=="video"),None)
    if v is None:
        raise ValueError("no video stream")
    return info,v


def extract_frame(path, seconds, target):
    target=Path(target)
    target.parent.mkdir(parents=True,exist_ok=True)
    # These are derived review frames in a revision work directory; refresh on resume.
    run(["ffmpeg","-v","error","-i",str(path),"-ss",str(seconds),"-frames:v","1","-vf","scale=1280:-2","-y",str(target)],120)
    if not target.is_file():
        raise ValueError("frame not present at requested time")
    return target


def cfr_guard(path):
    info,v=video_info(path)
    if v.get("color_transfer") in ("smpte2084","arib-std-b67") or v.get("field_order","progressive") not in ("unknown","progressive"):
        raise ValueError("HDR/interlaced input requires manual preprocessing")
    # Inspect timestamps rather than trusting r_frame_rate alone.
    frames=json.loads(run(["ffprobe","-v","error","-select_streams","v:0","-show_frames","-show_entries","frame=best_effort_timestamp_time","-of","json",str(path)],300))["frames"]
    ts=[float(f["best_effort_timestamp_time"]) for f in frames if "best_effort_timestamp_time" in f]
    if len(ts)<2:
        raise ValueError("insufficient timestamp evidence")
    diffs=np.diff(ts)
    if np.max(np.abs(diffs-np.median(diffs)))>0.001:
        raise ValueError("VFR is not supported by the v0.1 renderer; manual review required")
    return info,v


def render(source,intro,title,plan,work,encoder="libx264",size=(1920,1080)):
    work=Path(work);work.mkdir(parents=True,exist_ok=True)
    info,v=cfr_guard(source)
    duration=float(info["format"]["duration"])
    if plan.lecture.end>duration+0.001:
        raise ValueError("lecture range exceeds source")
    introinfo,_=video_info(intro)
    if float(introinfo["format"]["duration"])<6:
        raise ValueError("intro shorter than six seconds")
    audio_stream=next((s for s in info["streams"] if s["codec_type"]=="audio"),None)
    if not audio_stream:
        raise ValueError("source audio missing")
    if audio_stream.get("channels",0) not in (1,2):
        raise ValueError("multichannel audio requires manual channel approval")
    fps=plan.fps
    source_fps=v["r_frame_rate"]
    source_start=frame_count(plan.lecture.start,source_fps)
    source_end=frame_count(plan.lecture.end,source_fps)
    lecture_seconds=float(Fraction(source_end-source_start,1)/Fraction(source_fps))
    counts=[frame_count(6,fps),frame_count(plan.title_seconds,fps),frame_count(lecture_seconds,fps)]
    width,height=size
    vf=f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p"
    parts=[]
    for i,(path,n) in enumerate(zip([intro,title,source],counts)):
        seconds=float(Fraction(n,1)/Fraction(fps))
        args=["ffmpeg","-v","error","-nostdin","-n"]
        if i==1:
            args += ["-loop","1"]
        if i==2:
            args += ["-copyts"]
        args += ["-i",str(path)]
        has_audio=i!=1 and any(s["codec_type"]=="audio" for s in (introinfo if i==0 else info)["streams"])
        if not has_audio:
            args += ["-f","lavfi","-i","anullsrc=r=48000:cl=stereo"]
        segment_vf=vf
        segment_af="asetpts=PTS-STARTPTS,apad"
        if i==2:
            segment_vf=f"trim=start_frame={source_start}:end_frame={source_end},setpts=PTS-STARTPTS,"+vf
            origin=float(v.get("start_time",0))
            audio_start=origin+float(Fraction(source_start,1)/Fraction(source_fps))
            audio_end=origin+float(Fraction(source_end,1)/Fraction(source_fps))
            segment_af=f"atrim=start={audio_start}:end={audio_end},asetpts=PTS-STARTPTS,apad"
        args += ["-map","0:v:0","-map","0:a:0" if has_audio else "1:a:0","-t",str(seconds),"-vf",segment_vf,"-af",segment_af,"-c:v",encoder]
        if encoder=="libx264":
            args += ["-preset","fast","-crf","20"]
        args += ["-c:a","aac","-ar","48000","-ac",str(audio_stream["channels"]),"-movflags","+faststart",str(work/f"part-{i}.mp4")]
        run(args)
        parts.append(work/f"part-{i}.mp4")
    listing=work/"concat.txt"
    listing.write_text("".join(f"file 'part-{i}.mp4'\n" for i in range(3)))
    output=work/"render.mp4"
    run(["ffmpeg","-v","error","-n","-f","concat","-safe","1","-i",str(listing),"-c","copy","-movflags","+faststart",str(output)])
    return output,{"segment_frames":counts,"expected_frames":sum(counts),"fps":fps,"size":list(size),"source_frame_range":[source_start,source_end],"source_fps":source_fps,"title_actual_seconds":float(Fraction(counts[1],1)/Fraction(fps)),"source_sha256":digest(source)}


def verify_output(path,expected):
    info,v=video_info(path)
    if (v["width"],v["height"])!=tuple(expected["size"]) or v["codec_name"]!="h264":
        raise ValueError("output video specification mismatch")
    if not any(s["codec_type"]=="audio" and s["codec_name"]=="aac" for s in info["streams"]):
        raise ValueError("AAC audio missing")
    if Fraction(v["r_frame_rate"])!=Fraction(expected["fps"]):
        raise ValueError("output fps mismatch")
    nominal=expected["expected_frames"]/float(Fraction(expected["fps"]))
    audio=next(s for s in info["streams"] if s["codec_type"]=="audio")
    if abs(float(audio["duration"])-nominal)>0.12 or abs(float(v["duration"])-nominal)>1/float(Fraction(expected["fps"]))+0.001:
        raise ValueError("audio/video duration outside allowed encoder padding")
    decoded=run(["ffmpeg","-v","error","-xerror","-i",str(path),"-map","0:v:0","-map","0:a:0","-fps_mode","passthrough","-progress","pipe:1","-f","null","-"])
    frames=[int(x.split("=",1)[1]) for x in decoded.decode().splitlines() if x.startswith("frame=")]
    if not frames or frames[-1]!=expected["expected_frames"]:
        raise ValueError(f"frame mismatch: {frames[-1:]}, expected {expected['expected_frames']}")
    return {"full_decode":True,"frames":frames[-1],"duration":float(info["format"]["duration"]),"sha256":digest(path)}


def verify_audio(source,output,plan):
    length=plan.lecture.end-plan.lecture.start
    win=min(8,length/4)
    if win<0.2:
        raise ValueError("audio too short to validate")
    offset=float(Fraction(frame_count(6,plan.fps)+frame_count(plan.title_seconds,plan.fps),1)/Fraction(plan.fps))
    rows=[]
    for point in [0,(length-win)/2,length-win]:
        samples=[]
        for path,start in [(source,plan.lecture.start+point),(output,offset+point)]:
            b=run(["ffmpeg","-v","error","-i",str(path),"-ss",str(start),"-t",str(win),"-vn","-ac","1","-ar","8000","-f","f32le","pipe:1"])
            a=np.frombuffer(b,np.float32).astype(float);samples.append(a-a.mean())
        a,b=samples
        if min(len(a),len(b))==0 or min(np.linalg.norm(a),np.linalg.norm(b))<0.001:
            raise ValueError("silent/unusable audio sample; human review required")
        size=1<<(len(a)+len(b)-2).bit_length()
        corr=np.fft.irfft(np.fft.rfft(b,size)*np.conj(np.fft.rfft(a,size)),size)
        lags=np.arange(-1600,1601);lag=int(lags[np.argmax(corr[lags%size])])
        if lag>=0: x=a[:min(len(a),len(b)-lag)];y=b[lag:lag+len(x)]
        else: y=b[:min(len(b),len(a)+lag)];x=a[-lag:-lag+len(y)]
        score=float(np.dot(x,y)/(np.linalg.norm(x)*np.linalg.norm(y)))
        rows.append({"source_seconds":plan.lecture.start+point,"lag_seconds":lag/8000,"correlation":score})
    if any(r["correlation"]<0.90 or abs(r["lag_seconds"])>0.08 for r in rows) or max(r["lag_seconds"] for r in rows)-min(r["lag_seconds"] for r in rows)>0.04:
        raise ValueError("audio alignment failed")
    return rows


def publish(path,root,name,expected_hash):
    if digest(path)!=expected_hash:
        raise ValueError("reviewed artifact hash changed")
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    # Hardlink is atomic and does not replace. Work/output are on same data filesystem.
    if Path(name).name!=name:
        raise ValueError("invalid output filename")
    with Path(path).open("rb") as source:
        os.fsync(source.fileno())
    for i in range(1000):
        dest=root/(name if i==0 else f"{Path(name).stem}-{i}{Path(name).suffix}")
        try:
            os.link(path,dest)
            descriptor=os.open(root,os.O_RDONLY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
            return dest
        except FileExistsError:
            continue
    raise ValueError("too many filename conflicts")
