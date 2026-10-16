"""Decoded source PTS index and exact frame extraction."""
import json
from bisect import bisect_left
from fractions import Fraction
from pathlib import Path
import numpy as np
from .media import run, video_info


class FrameIndex:
    def __init__(self, source, cache):
        self.source = Path(source)
        info, stream = video_info(source)
        self.info, self.stream = info, stream
        self.time_base = Fraction(stream["time_base"])
        cache = Path(cache)
        if cache.exists():
            data = json.loads(cache.read_text())
        else:
            raw = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
                                 "-show_entries", "frame=best_effort_timestamp", "-of", "json", str(source)], 600))
            data = [int(f["best_effort_timestamp"]) for f in raw["frames"]]
            cache.write_text(json.dumps(data))
        if len(data) < 2 or any(b <= a for a,b in zip(data,data[1:])):
            raise ValueError("source frame timestamps invalid")
        self.pts = data
        self.seconds = [float((p-data[0])*self.time_base) for p in data]
        step = float(np.median(np.diff(self.seconds)))
        self.end_seconds = self.seconds[-1] + step
        rate=Fraction(stream["r_frame_rate"])
        self.fps = f"{rate.numerator}/{rate.denominator}"
        if np.max(np.abs(np.diff(self.seconds)-step)) > 0.001:
            raise ValueError("VFR requires manual output-rate handling")
        if abs(step - 1/float(Fraction(self.fps))) > 0.001:
            raise ValueError("nominal fps does not match decoded timestamps")

    def nearest(self, seconds):
        i = bisect_left(self.seconds, seconds)
        return min(range(max(0,i-1), min(len(self.seconds),i+1)), key=lambda k:abs(self.seconds[k]-seconds))

    def timestamp(self, frame):
        if frame == len(self.pts):
            return self.end_seconds
        return self.seconds[frame]

    def extract(self, frame, target):
        if not 0 <= frame < len(self.pts):
            raise ValueError("frame outside source")
        target = Path(target)
        if target.exists():
            return target
        target.parent.mkdir(parents=True,exist_ok=True)
        seek = max(0,self.seconds[frame]-2)
        run(["ffmpeg", "-v", "error", "-ss", str(seek), "-copyts", "-i", str(self.source),
             "-vf", f"select=eq(pts\\,{self.pts[frame]}),scale=1280:-2", "-frames:v", "1",
             "-fps_mode", "vfr", "-n", str(target)], 120)
        if not target.is_file():
            raise ValueError("exact source frame extraction failed")
        return target
