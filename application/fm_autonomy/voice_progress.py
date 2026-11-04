"""Small, atomic, read-only UI receipts for actual model initialization stages."""
import json
import os
from pathlib import Path
import time

STAGES = ('校验模型与配置', '检查运行环境', '加载 ASR 语音识别', '加载 BERT 指令理解', '加载 TTS 语音合成', '预热固定短回复', '模型就绪')


class LoadProgress:
    def __init__(self, root):
        self.path = Path(root) / 'voice_load_progress.json'
        self.started = time.time()
        self.records = []
        self.stages = STAGES

    def stage(self, index, completed=False):
        now = time.time()
        self.records.append(dict(stage=index, label=self.stages[index], completed=completed, time=now))
        value = dict(schema='FM-VOICE-LOAD/1', started_at=self.started, updated_at=now,
                     stage=index, label=self.stages[index], completed_stages=index + int(completed),
                     total_stages=len(self.stages), records=self.records[-16:],
                     progress_kind='completed_stages_not_time', complete=index == len(self.stages)-1 and completed)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        os.replace(temp, self.path)


def read_progress(root):
    try:
        path = Path(root) / 'voice_load_progress.json'
        if path.stat().st_size > 12000:
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if value.get('schema') == 'FM-VOICE-LOAD/1' else None
    except (OSError, ValueError, AttributeError):
        return None
