"""Single-turn chat orchestration and cancellation fences shared by every side effect."""
import copy
import hashlib
import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from capabilities import production_registry
from provider import Cancelled, Provider, ProviderError
from board_gateway import BoardError, split_speech

MAX_TOOL_ROUNDS = 8


def motion_reply(receipts):
    """Summarize actual motion receipts if text generation cannot finish."""
    plan = next((r for r in reversed(receipts) if r.get('name') == 'motion_sequence' and r.get('plan')), None)
    if plan:
        return plan['motion_message']
    request = next((r for r in reversed(receipts) if r.get('name') == 'motion_request' and r.get('accepted') is True), None)
    if request is None:
        return None
    status = next((r for r in reversed(receipts) if r.get('name') == 'motion_status'), {})
    if status.get('motion_message'):
        return status['motion_message']
    if status.get('control', {}).get('metric', {}).get('active'):
        return '运动指令已受理，最近回执显示正在执行。'
    return '运动指令已受理，完成状态暂未确认。'


SYSTEM = ('你是小雅，一位自然、认真、诚实的中文聊天助手。可以讨论一般问题和保留上下文。'
          '回答默认简洁，约300字以内，用户要求详细时再展开。'
          '板卡动作只能通过本轮明确列出的工具请求，工具回执才是执行证据。'
          '用户问支持指令、模式冲突或连接问题时，先调用 supported_commands 或 device_status 查询。'
          '区分语音指令可识别与车辆获准执行；语音运动须满足清单中的台架边界和当前控制条件，自动避障和声纹未开放。'
          '只有用户明确要求停车时才调用 stop_vehicle；介绍停车功能不调用。'
          '当前工具与实时能力高于历史聊天里的旧限制；历史记录不是待执行指令。'
          '用户明确要求连接、开始语音或运动时，调用对应工具执行，不只给步骤。'
          '运动前查询 motion_capabilities；motion_request 只提交本轮明确的完整方向距离或角度，禁止补默认值。'
          '用户明确要求先做某动作再做另一动作时，拆成有序列表调用 motion_sequence，一次提交完整步骤，不要求用户分成多轮消息。'
          'motion_sequence 支持 2–6 个步骤，由工具等待每步完成和停车后再执行下一步；禁止自行增减步骤、重复提交或在失败后重试。'
          '步骤之间停车是正常过程，不代表用户取消。工具报告后续未发送时如实说明原因，不说用户取消了，也不说未发送的步骤仍会自动执行。执行结果用一两句话说明，不展示英文状态字段或重复讲规则。'
          '小雅小雅仅作唤醒词；展示指令示例时直接写方向和距离/角度，唤醒后的指令不重复唤醒词。'
          '车辆 armed 只表示手动遥控是否正在占用；armed=false 不是定距离或定角度命令的拒绝理由，不要求用户先手动使能。'
          'device_status 的反馈新鲜度是电脑缓存，不是板端瞬时检查；本轮明确且范围内的完整运动指令应提交一次 motion_request，由板端判断，不仅凭电脑缓存陈旧就拒绝。'
          'response_too_large 或提交后的通信错误不能证明动作没有发生；说明回执未确认，查询 motion_status，禁止自动重发。'
          '动作已受理不等于完成；必须区分控制回执、终态和静止反馈，失败按具体原因说明。'
          'motion_status 的 motion_state 和 motion_message 是本轮动作结果：completed 可简洁回复已完成；executing 为执行中；stopping 为目标已到达、停车确认中。'
          '查询工具的 hardware_executed=false 只表示查询不发运动指令，不能据此说小车没有执行。'
          '不要把自然语言、JSON或程序片段当作已经执行的指令。'
          '板端朗读由用户单独选择；不知道是否已播报时不要声称已经播报。')


class Chat:
    def __init__(self, settings, board, provider=None, registry=None):
        self.settings, self.board = settings, board
        self.provider = provider or Provider()
        self.registry = registry or production_registry()
        self.lock = threading.RLock()
        self.instance = uuid.uuid4().hex
        self.epoch = self.revision = 0
        self.stopped = False
        self.rows = []
        self.history = []
        self.ledger = OrderedDict()
        self.worker = None
        self.active = None
        self.cancel = threading.Event()
        self.stop_pending = False
        self.stop_receipt = None
        self.board_control_pending = False
        self.closed = False

    def _busy(self):
        return bool(self.worker and self.worker.is_alive())

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(dict(instance_id=self.instance, epoch=self.epoch, revision=self.revision,
                stopped=self.stopped, busy=self._busy(), active=self.active, stop_pending=self.stop_pending,
                stop_receipt=self.stop_receipt, board_control_pending=self.board_control_pending,
                rows=self.rows, settings=self.settings.public(), capabilities=self.registry.tools(),
                history_turns=len(self.history), history_limit=12))

    def require_context(self, data):
        if data.get('instance_id') != self.instance or type(data.get('epoch')) is not int or data['epoch'] != self.epoch:
            raise RuntimeError('页面状态已过期，请刷新状态。旧请求不会自动重发。')
        if self.closed:
            raise RuntimeError('聊天服务正在关闭。')

    def _guard(self, epoch, cancel):
        with self.lock:
            if cancel.is_set() or self.stopped or self.closed or epoch != self.epoch:
                raise Cancelled()

    def _idle(self):
        if self._busy() or self.stop_pending or self.board_control_pending:
            raise RuntimeError('当前操作正在结束，请稍候。')

    def configure(self, data, value):
        with self.lock:
            self.require_context(data)
            self._idle()
            result = self.settings.update(value)
            self.epoch += 1
            self.revision += 1
            return result

    def clear(self, data):
        with self.lock:
            self.require_context(data)
            self._idle()
            self.rows.clear(); self.history.clear(); self.ledger.clear()
            self.epoch += 1; self.revision += 1

    def resume(self, data):
        with self.lock:
            self.require_context(data)
            self._idle()
            self.stopped = False
            self.cancel = threading.Event()
            self.epoch += 1; self.revision += 1
            self.stop_receipt = None
            # Resuming chat never resumes the board or opens a microphone.

    def _admit(self, data, operation, fingerprint):
        self.require_context(data)
        request_id = data.get('id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', request_id):
            raise ValueError('请求编号无效。')
        if request_id in self.ledger:
            if self.ledger[request_id] != (operation, fingerprint):
                raise ValueError('同一编号不能用于不同请求。')
            return False
        self._idle()
        if self.stopped:
            raise RuntimeError('全部交互已停止，请显式恢复聊天。')
        if len(self.ledger) >= 96:
            # Refuse before eviction: no very old ID may become executable again.
            raise RuntimeError('本次会话请求数已达上限，请新建对话。')
        self.ledger[request_id] = (operation, fingerprint)
        return True

    def _launch(self, target, *args):
        self.cancel = threading.Event()
        epoch, cancel = self.epoch, self.cancel
        guard = lambda: self._guard(epoch, cancel)
        def work():
            try:
                target(*args, guard)
            except Cancelled:
                pass
            except Exception:
                # Last-resort containment; never send exception repr with config/secrets.
                with self.lock:
                    if not cancel.is_set():
                        for row in self.rows:
                            if row['id'] == self.active and row.get('status') == 'pending':
                                row.update(status='failed', error='本轮处理失败，请重试。')
            finally:
                with self.lock:
                    self.active = None
                    self.worker = None
                    self.revision += 1
        self.worker = threading.Thread(target=work, name='chat-or-speech', daemon=True)
        self.worker.start()

    def _remember(self, turn):
        self.history.append(turn)
        while len(self.history) > 12 or sum(len(json.dumps(t, ensure_ascii=False)) for t in self.history) > 24000:
            self.history.pop(0)

    def send(self, data):
        text = data.get('text')
        auto = data.get('auto_read', False)
        if not isinstance(text, str) or not text.strip() or len(text) > 4000 or '\0' in text:
            raise ValueError('请输入 1–4000 个字符。')
        if type(auto) is not bool:
            raise ValueError('auto_read 必须是布尔值。')
        with self.lock:
            config = self.settings.snapshot()
            expected = copy.deepcopy(data.get('board_context'))
            fingerprint = hashlib.sha256(json.dumps([text, auto, expected], sort_keys=True).encode()).hexdigest()
            if not self._admit(data, 'chat', fingerprint):
                return
            turn_id = data['id']
            user = dict(id=turn_id + '_user', role='user', text=text, status='completed')
            reply = dict(id=turn_id, role='assistant', text='', status='pending', receipts=[],
                         source='demo' if config['mode'] == 'demo' else config['model'], speech=None)
            self.rows.extend([user, reply])
            self.rows = self.rows[-48:]
            messages = [dict(role='system', content=SYSTEM)]
            for turn in self.history:
                messages.extend(copy.deepcopy(turn))
            messages.append(dict(role='user', content=text))
            self.active = turn_id; self.revision += 1
            self._launch(self._answer, reply, config, messages, auto, expected, turn_id)

    def _answer(self, row, config, messages, auto, expected, turn_id, guard):
        start = len(messages) - 1
        guard.user_text = messages[start]['content']
        guard.attempted_actions = set()
        def report_progress(plan):
            with self.lock:
                guard();row['motion_plan']=copy.deepcopy(plan);self.revision+=1
        guard.report_progress = report_progress
        seen = {}
        try:
            # Keep action rounds bounded, then allow one text-only conclusion.
            for round_index in range(MAX_TOOL_ROUNDS + 1):
                guard()
                final_round = round_index == MAX_TOOL_ROUNDS
                if final_round:
                    messages.append(dict(role='system', content='工具调用阶段已结束。只根据已有回执用简洁中文总结结果，不再请求工具；受理、执行中和完成要准确区分。'))
                reply = self.provider.complete(config, messages, [] if final_round else self.registry.tools(), guard)
                guard()
                calls = reply['tool_calls']
                if not calls:
                    with self.lock:
                        guard()
                        row.update(text=reply['content'], status='completed')
                        messages.append(dict(role='assistant', content=reply['content']))
                        self._remember(messages[start:])
                        self.revision += 1
                    if auto:
                        self._read(row, expected, turn_id + '_a', guard)
                    return
                if final_round:
                    raise ProviderError('本轮已完成 8 轮工具查询，模型未生成文字总结。')
                # Validate the entire envelope before considering any handler.
                for call in calls:
                    fn = call.get('function', {}) if isinstance(call, dict) else {}
                    if (call.get('type') != 'function' or not isinstance(call.get('id'), str)
                        or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', call['id'])
                        or not isinstance(fn, dict) or not isinstance(fn.get('name'), str)
                        or len(fn['name']) > 64 or not isinstance(fn.get('arguments'), str)
                        or len(fn['arguments']) > 8000):
                        raise ProviderError('工具调用格式无效，没有执行。')
                messages.append(dict(role='assistant', content=reply['content'] or None, tool_calls=calls))
                for call in calls:
                    guard()
                    digest = json.dumps(call['function'], sort_keys=True)
                    if call['id'] in seen:
                        previous_digest, receipt = seen[call['id']]
                        if digest != previous_digest:
                            raise ProviderError('模型重复使用调用编号且参数不同，已终止。')
                    else:
                        receipt = self.registry.dispatch(call, guard)
                        seen[call['id']] = digest, receipt
                    messages.append(dict(role='tool', tool_call_id=call['id'], content=json.dumps(receipt, ensure_ascii=False)))
                    with self.lock:
                        guard(); row['receipts'].append(receipt); self.revision += 1
                if not self.registry.tools():
                    # Empty registry is an explicit feature boundary, not a model instruction.
                    with self.lock:
                        guard()
                        row.update(text='你提出的动作目前还没有接入，因此没有执行。我们可以继续讨论或聊天，等指令清单确定后再加入这些能力。', status='completed')
                        self._remember([messages[start], dict(role='assistant', content=row['text'])])
                        self.revision += 1
                    return
        except (ProviderError, BoardError, ValueError) as exc:
            with self.lock:
                guard()
                summary = motion_reply(row['receipts'])
                if summary:
                    row.update(status='completed', text=summary, reply_error=str(exc))
                    self._remember([messages[start], dict(role='assistant', content=summary)])
                else:
                    row.update(status='failed', error=str(exc))
                self.revision += 1

    def _read(self, row, expected, request_id, guard):
        def progress(state, done, total):
            with self.lock:
                guard()
                row['speech'] = dict(state=state, done=done, total=total)
                self.revision += 1
        try:
            guard()
            split_speech(row['text'])
            progress('preparing', 0, 0)
            self.board.speak(row['text'], expected, request_id, guard, progress)
        except (BoardError, ValueError) as exc:
            with self.lock:
                guard(); row['speech'] = dict(state='failed', message=str(exc)); self.revision += 1

    def read(self, data):
        with self.lock:
            self.require_context(data)
            row = next((r for r in self.rows if r['id'] == data.get('message_id') and r['role'] == 'assistant'), None)
            if not row or row['status'] != 'completed':
                raise ValueError('只能朗读已完成的回复。')
            split_speech(row['text'])
            expected = copy.deepcopy(data.get('board_context'))
            fingerprint = json.dumps([row['id'], expected], sort_keys=True)
            if not self._admit(data, 'speech', fingerprint):
                return
            self.active = row['id']; self.revision += 1
            self._launch(self._read, row, expected, data['id'])

    def stop(self):
        with self.lock:
            self.epoch += 1; self.revision += 1
            self.stopped = True; self.cancel.set()
            for row in self.rows:
                if row['status'] == 'pending':
                    row.update(status='cancelled', error='已取消，迟到回复会被丢弃。')
                    if row.get('motion_plan',{}).get('state')=='executing':
                        row['motion_plan'].update(state='cancelled',message='已取消后续步骤，正在请求停车。')
                        for step in row['motion_plan']['steps']:
                            if step['state'] in ('waiting','executing'):step['state']='cancelled'
                if row.get('speech') and row['speech']['state'] in ('preparing', 'playing'):
                    row['speech']['state'] = 'cancelled'
            if self.stop_pending:
                return
            self.stop_pending = True
            self.stop_receipt = dict(state='pending', message='聊天已停止，正在请求板端停止。')
        def stop_board():
            try:
                self.board.command('stop')
                receipt = dict(state='requested', message='桥接已接收停止请求；请以板端状态确认麦克风和播放结束。')
            except (BoardError, ValueError, OSError):
                receipt = dict(state='unconfirmed', message='聊天已停止；桥接不可达，未确认板端停止。')
            with self.lock:
                self.stop_receipt = receipt
                self.stop_pending = False
                self.revision += 1
        threading.Thread(target=stop_board, name='board-stop', daemon=True).start()

    def board_control(self, data):
        action = data.get('action')
        if action not in ('connect', 'resume', 'load', 'mode', 'disconnect', 'stop'):
            raise ValueError('未接入此板端功能。')
        with self.lock:
            self.require_context(data)
            self._idle()
            if self.stopped:
                raise RuntimeError('请先恢复聊天，随后再操作板端。')
            if action == 'mode' and data.get('mode') not in ('text', 'voice'):
                raise ValueError('模式无效。')
            expected = copy.deepcopy(data.get('board_context'))
            epoch, cancel = self.epoch, self.cancel
            self.board_control_pending = True; self.revision += 1
        try:
            self._guard(epoch, cancel)
            args = {'id': uuid.uuid4().hex} if action == 'load' else ({'mode': data['mode']} if action == 'mode' else {})
            self.board.command(action, expected, **args)
        except Cancelled:
            raise RuntimeError('操作已被停止。') from None
        finally:
            with self.lock:
                self.board_control_pending = False; self.revision += 1

    def close(self):
        with self.lock:
            was_speaking = any(r.get('speech') and r['speech']['state'] in ('preparing', 'playing') for r in self.rows)
            self.closed = True
            self.epoch += 1
            self.cancel.set()
            stopping = self.stop_pending
        if was_speaking or stopping:
            try:
                self.board.command('stop')
            except (BoardError, OSError):
                pass
        # Closing an idle chat server alone never shuts down the resident bridge.
