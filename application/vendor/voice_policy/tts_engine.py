"""Local XiaoYa ONNX synthesis with a small pypinyin frontend (not upstream g2pW)."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
import wave

import numpy as np
import onnxruntime as ort
from pypinyin import Style, pinyin

ROOT=Path(__file__).resolve().parent
MAX_TEXT=200
MAX_AUDIO_SECONDS=45
PUNCT=set("。？！；：，、,.!?;:—… ")
INITIALS=("zh","ch","sh",*"bpmfdtnlgkhjqxrzcsyw")
DIGITS="零一二三四五六七八九"


def chinese_integer(digits):
    if len(digits)>4 or (len(digits)>1 and digits.startswith("0")):
        return "".join(DIGITS[int(c)] for c in digits)
    n=int(digits)
    if n==0: return "零"
    out=""
    pending_zero=False
    for power,unit in ((3,"千"),(2,"百"),(1,"十"),(0,"")):
        d,n=divmod(n,10**power)
        if d:
            if pending_zero: out+="零"
            out+=DIGITS[d]+unit
            pending_zero=False
        elif out and n: pending_zero=True
    return out[1:] if out.startswith("一十") else out


def normalize_text(text):
    if not isinstance(text,str) or not text.strip() or len(text)>MAX_TEXT:
        raise ValueError("TTS needs 1..200 text characters")
    text=unicodedata.normalize("NFKC",text)
    text=re.sub(r"[\"'“”‘’]","",text)
    text=re.sub(r"[()（）\[\]【】]","，",text)
    text=re.sub(r"\s+"," ",text).strip()
    def number(m):
        raw=m.group()
        sign="负" if raw.startswith("-") else ""
        integer,sep,fraction=raw.lstrip("-").partition(".")
        return sign+chinese_integer(integer)+( "点"+"".join(DIGITS[int(c)] for c in fraction) if sep else "")
    text=re.sub(r"-?[0-9]+(?:\.[0-9]+)?",number,text)
    if re.search(r"[^\u4e00-\u9fff。？！；：，、,.!?;:—… ]",text):
        raise ValueError("This lightweight frontend supports Chinese, numbers and punctuation only")
    if not re.search(r"[\u4e00-\u9fff]",text): raise ValueError("No pronounceable Chinese text")
    if len(text)>MAX_TEXT: raise ValueError("Normalized text too long")
    return text


def split_text(text,limit=32):
    chunks=[]
    current=""
    for char in text:
        current+=char
        if char in "。？！.!?；;" or len(current)>=limit:
            if current.strip(): chunks.append(current)
            current=""
    if current.strip(): chunks.append(current)
    return chunks


def text_to_ids(text,id_map):
    ids=list(id_map["^"])
    for row in pinyin(text,style=Style.TONE3,neutral_tone_with_five=True,heteronym=False,errors=lambda chars:list(chars)):
        syllable=row[0].lower().replace("ü","v").replace("u:","v")
        if syllable in PUNCT:
            group=[syllable]
        else:
            match=re.fullmatch(r"([a-zv]+)([1-5])",syllable)
            if not match: raise ValueError("Unsupported pinyin syllable: "+syllable)
            stem,tone=match.groups()
            initial=next((x for x in INITIALS if stem.startswith(x)),"")
            group=[initial or "Ø",stem[len(initial):],tone]
        for symbol in group:
            if symbol not in id_map: raise ValueError("Missing phoneme: "+symbol)
            ids.extend(id_map[symbol])
        ids.extend(id_map["_"])
    ids.extend(id_map["$"])
    return ids


@dataclass
class Speech:
    pcm: np.ndarray
    sample_rate: int
    metrics: dict

    def write_wav(self,path):
        # 'xb' prevents overwriting earlier research samples.
        with Path(path).open("xb") as target, wave.open(target,"wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate)
            wav.writeframes(self.pcm.astype("<i2",copy=False).tobytes())


class LocalTTS:
    def __init__(self,folder=None,threads=2,memory_arena=False):
        started=time.perf_counter()
        folder=Path(folder) if folder else ROOT/"models/xiao_ya"
        manifest=json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
        for name,info in manifest["files"].items():
            if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=info["sha256"]:
                raise ValueError("TTS asset hash mismatch: "+name)
        model=folder/"zh_CN-xiao_ya-medium.onnx"
        self.config=json.loads((folder/(model.name+".json")).read_text(encoding="utf-8"))
        if self.config["phoneme_type"]!="pinyin": raise ValueError("Expected pinyin voice")
        options=ort.SessionOptions()
        options.intra_op_num_threads=threads
        options.inter_op_num_threads=1
        options.enable_mem_pattern=False
        options.enable_cpu_mem_arena=memory_arena
        self.memory_arena=memory_arena
        self.session=ort.InferenceSession(str(model),sess_options=options,providers=["CPUExecutionProvider"])
        self.sample_rate=int(self.config["audio"]["sample_rate"])
        self.load_seconds=time.perf_counter()-started
        self.model_bytes=model.stat().st_size

    def synthesize(self,text):
        started=time.perf_counter()
        normalized=normalize_text(text)
        pieces=[]
        total=0
        ids_count=0
        for chunk in split_text(normalized):
            ids=text_to_ids(chunk,self.config["phoneme_id_map"])
            ids_count+=len(ids)
            feed={"input":np.array([ids],dtype=np.int64),"input_lengths":np.array([len(ids)],dtype=np.int64),
                  "scales":np.array([.667,1.0,.8],dtype=np.float32)}
            if "sid" in {x.name for x in self.session.get_inputs()}: feed["sid"]=np.array([0],dtype=np.int64)
            audio=np.asarray(self.session.run(None,feed)[0],dtype=np.float32).reshape(-1)
            if not len(audio) or not np.isfinite(audio).all(): raise ValueError("Invalid synthesized audio")
            total+=len(audio)
            if total>MAX_AUDIO_SECONDS*self.sample_rate: raise ValueError("Speech exceeds 45 seconds")
            pieces.append(audio)
        wave_float=np.concatenate(pieces)
        peak=float(np.max(np.abs(wave_float)))
        if peak<1e-5: raise ValueError("Synthesized speech is silent")
        wave_float=wave_float/max(1.,peak)*.75
        pcm=np.round(np.clip(wave_float,-1.,1.)*32767).astype(np.int16)
        elapsed=time.perf_counter()-started
        duration=len(pcm)/self.sample_rate
        return Speech(pcm,self.sample_rate,dict(normalized_text=normalized,seconds=duration,
            synth_seconds=elapsed,rtf=elapsed/duration,phoneme_ids=ids_count,peak_before_gain=peak,
            frontend="pypinyin_lite",model="xiao_ya_medium",speaker_count=1))
