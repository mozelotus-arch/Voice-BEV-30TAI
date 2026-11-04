"""Small, inspectable Chinese front end. No neural weights or global pinyin mutations."""
from dataclasses import dataclass
from datetime import date
import re
import unicodedata

from pypinyin import Style, pinyin
from tts_engine import chinese_integer, DIGITS, INITIALS, MAX_TEXT

PUNCT = set("。？！，；：、—… ")
STRONG = set("。？！")
# Longest match first; these affect pronunciation only, never the displayed reply/command.
PHRASES = {
    "空调": "kong1 tiao2", "调到": "tiao2 dao4", "调高": "tiao2 gao1",
    "调低": "tiao2 di1", "调节": "tiao2 jie2", "调整": "tiao2 zheng3",
    "重新": "chong2 xin1", "重启": "chong2 qi3", "重复": "chong2 fu4",
    "重要": "zhong4 yao4", "重庆": "chong2 qing4", "音乐": "yin1 yue4",
    "快乐": "kuai4 le4", "银行": "yin2 hang2", "行程": "xing2 cheng2",
    "执行": "zhi2 xing2", "不行": "bu4 xing2", "长按": "chang2 an4",
    "长时间": "chang2 shi2 jian1", "长大": "zhang3 da4", "还有": "hai2 you3",
    "还没": "hai2 mei2", "还是": "hai2 shi4", "归还": "gui1 huan2",
    "好的": "hao3 de5", "什么": "shen2 me5", "哪个": "na3 ge4",
    "稍等": "shao1 deng3", "闹钟": "nao4 zhong1", "设备": "she4 bei4",
}
PROTECTED = set(PHRASES) | {
    "灵犀管家", "模拟器", "未指定", "开关状态", "状态记录", "客厅", "卧室",
    "电视", "风扇", "窗帘", "关闭", "打开", "确认", "取消", "摄氏度", "百分之",
}


def number_words(raw):
    sign = "负" if raw.startswith("-") else ""
    integer, dot, fraction = raw.lstrip("-").partition(".")
    return sign + chinese_integer(integer) + ("点" + "".join(DIGITS[int(c)] for c in fraction) if dot else "")


def normalize(text):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError("TTS needs 1..200 text characters")
    text = unicodedata.normalize("NFKC", text).replace("−", "-")
    text = re.sub(r"[\"'“”‘’]", "", text)
    text = re.sub(r"[()\[\]【】]", "，", text)
    text = re.sub(r"\s+", " ", text).strip()

    def calendar(m):
        year, month, day = map(int, m.groups())
        date(year, month, day)  # Invalid dates fail explicitly; don't silently invent a reading.
        return "".join(DIGITS[int(c)] for c in m[1]) + "年" + chinese_integer(str(month)) + "月" + chinese_integer(str(day)) + "日"

    text = re.sub(r"(?<![0-9])([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})(?![0-9])", calendar, text)

    def clock(m):
        h, minute = int(m[1]), int(m[2])
        if not (0 <= h < 24 and 0 <= minute < 60):
            raise ValueError("Invalid HH:MM time")
        return chinese_integer(str(h)) + "点" + ("整" if minute == 0 else ("零" if minute < 10 else "") + chinese_integer(str(minute)) + "分")

    text = re.sub(r"(?<![0-9])([0-9]{1,2}):([0-9]{2})(?![0-9])", clock, text)
    text = re.sub(r"(-?[0-9]+(?:\.[0-9]+)?)\s*°\s*[Cc]", lambda m: ("零下" if m[1].startswith("-") else "") + number_words(m[1].lstrip("-")) + "度", text)
    text = re.sub(r"(-?[0-9]+(?:\.[0-9]+)?)\s*%", lambda m: ("负" if m[1].startswith("-") else "") + "百分之" + number_words(m[1].lstrip("-")), text)
    text = re.sub(r"(?<![0-9])([0-9]{4})年", lambda m: "".join(DIGITS[int(c)] for c in m[1]) + "年", text)
    text = re.sub(r"-?[0-9]+(?:\.[0-9]+)?", lambda m: number_words(m[0]), text)
    text = text.translate(str.maketrans({",": "，", ".": "。", "?": "？", "!": "！", ";": "；", ":": "："}))
    # Keep question/exclamation semantics; collapse repeated punctuation rather than adding pauses per glyph.
    def punctuation_run(m):
        # One boundary per run. Prefer the last sentence mark over commas/spaces.
        strong = [c for c in m[0] if c in STRONG]
        return strong[-1] if strong else m[0][-1]
    text = re.sub(r"[。？！，；：、—… ]{2,}", punctuation_run, text)
    text = text.lstrip("".join(PUNCT))
    if re.search(r"[^\u4e00-\u9fff。？！，；：、—… ]", text):
        raise ValueError("Unsupported text: Chinese, numbers and common punctuation only")
    if not re.search(r"[\u4e00-\u9fff]", text) or len(text) > MAX_TEXT:
        raise ValueError("No Chinese text or normalized text exceeds 200 characters")
    return text


def pronounce(text):
    rows = pinyin(text, style=Style.TONE3, neutral_tone_with_five=True,
                  heteronym=False, errors=lambda chars: list(chars))
    syllables = [r[0].lower().replace("ü", "v").replace("u:", "v") for r in rows]
    if len(syllables) != len(text):
        raise ValueError("Pinyin character alignment lost")
    applied = []
    pos = 0
    ordered = sorted(PHRASES, key=len, reverse=True)
    while pos < len(text):
        phrase = next((p for p in ordered if text.startswith(p, pos)), None)
        if phrase:
            replacement = PHRASES[phrase].split()
            if len(replacement) != len(phrase):
                raise ValueError("Invalid phrase pronunciation entry")
            syllables[pos:pos + len(phrase)] = replacement
            applied.append(dict(start=pos, text=phrase, pinyin=replacement))
            pos += len(phrase)
        else:
            pos += 1
    if text.strip("".join(PUNCT)) == "行":
        syllables[text.index("行")] = "xing2"
    # No blanket tone-sandhi transformation: the training front end's convention is not established.
    return syllables, applied


def ids_from_syllables(syllables, id_map):
    ids = list(id_map["^"])
    for syl in syllables:
        if syl in PUNCT:
            group = [syl]
        else:
            match = re.fullmatch(r"([a-zv]+)([1-5])", syl)
            if not match:
                raise ValueError("Unsupported pinyin syllable: " + syl)
            stem, tone = match.groups()
            initial = next((i for i in INITIALS if stem.startswith(i)), "")
            group = [initial or "Ø", stem[len(initial):], tone]
        for symbol in group:
            if symbol not in id_map:
                raise ValueError("Missing phoneme: " + symbol)
            ids.extend(id_map[symbol])
        ids.extend(id_map["_"])
    return ids + list(id_map["$"])


@dataclass(frozen=True)
class Chunk:
    start: int
    end: int
    boundary: str


def plan_chunks(text, target=48, hard_limit=64):
    if not 8 <= target <= hard_limit <= MAX_TEXT:
        raise ValueError("Invalid chunk limits")
    spans = []
    for phrase in PROTECTED:
        spans += [(m.start(), m.end()) for m in re.finditer(re.escape(phrase), text)]
    spans += [(m.start(), m.end()) for m in re.finditer(r"[零一二三四五六七八九十百千万两点]+(?:度|秒|分|年|月|日)?", text)]
    chunks, start = [], 0
    while start < len(text):
        cap = min(start + hard_limit, len(text))
        strong = next((i + 1 for i in range(start, cap) if text[i] in STRONG), None)
        if strong:
            end, boundary = strong, "sentence"
        elif cap == len(text):
            end, boundary = cap, "end"
        else:
            cuts = [i + 1 for i in range(start + 7, cap) if text[i] in PUNCT]
            if cuts:
                end, boundary = min(cuts, key=lambda i: abs(i - start - target)), "punctuation"
            else:
                candidates = [i for i in range(start + 8, cap + 1)
                              if not any(a < i < b for a, b in spans)]
                # Bounded memory wins for an unreasonably long unbroken phrase, and the fallback is logged.
                end, boundary = (max(candidates) if candidates else cap), "length_fallback"
        # Leave room for a immediately following mark even at the hard cap.
        if end < len(text) and text[end] in PUNCT and end - start == hard_limit:
            safe = [i for i in range(start + 8, end) if not any(a < i < b for a, b in spans)]
            end = max(safe) if safe else end - 1
            boundary = "length_fallback"
        # Attach trailing punctuation to the previous spoken chunk, avoiding punctuation-only ONNX calls.
        while end < len(text) and text[end] in PUNCT and end - start < hard_limit:
            end += 1
        if re.search(r"[\u4e00-\u9fff]", text[start:end]):
            chunks.append(Chunk(start, end, boundary))
        elif chunks:
            last = chunks[-1]
            chunks[-1] = Chunk(last.start, end, last.boundary)
        else:
            # Leading punctuation is harmless, but keep offsets/content in the first spoken chunk.
            next_char = next((i for i in range(end, len(text)) if text[i] not in PUNCT), None)
            if next_char is None:
                raise ValueError("Punctuation-only text")
            end = next_char + 1
            chunks.append(Chunk(start, end, "leading_punctuation"))
        start = end
    return chunks
