"""Small explicit command language. Unknown/negated/compound text never moves."""
import re

MOVES = {'前进': [100, 0, 0], '后退': [-100, 0, 0], '左转': [0, 0, 400], '右转': [0, 0, -400]}
STOPS = {'停止', '停车', '停下', '停止全部', '紧急停止'}


def parse(text):
    if not isinstance(text, str) or len(text) > 120:
        raise ValueError('invalid_command_text')
    text = text.strip().rstrip('。！!')
    if text in STOPS:
        return dict(kind='stop')
    if text == '开始自主巡航':
        return dict(kind='auto', seconds=30)
    if text in ('切换手动', '手动接管'):
        return dict(kind='manual')
    match = re.fullmatch(r'(前进|后退|左转|右转)\s*(0\.[1-9]|1(?:\.0)?)\s*秒', text)
    if match:
        return dict(kind='pulse', velocity=MOVES[match[1]], seconds=float(match[2]))
    return dict(kind='unrecognized', reason='需要明确短时指令，例如“前进0.5秒”；未执行车辆动作')
