"""Short, truthful failure announcements; successful commands remain silent."""

def rejection(reason):
    reason = str(reason)
    if 'invalid_quantity' in reason: return '数字或单位不明确，未执行。'
    if 'unit_mismatch' in reason: return '距离或角度单位不匹配。'
    if 'outside_confirmed_forms' in reason: return '未识别出支持的指令。'
    if 'sleeping' in reason: return '请先唤醒小雅。'
    if 'automatic_avoidance' in reason: return '自动避障尚未开放。'
    if 'calibration_and_acceptance' in reason: return '距离角度控制尚未配置。'
    if 'voice_result_timeout' in reason: return '识别结果已过期，未执行。'
    if 'nlu_' in reason: return '模型理解与指令不一致，未执行。'
    if 'local_voice_manual_only' in reason or 'manual_control_only' in reason: return '当前仅支持遥控运动。'
    if 'control_interaction_stopped' in reason: return '语音交互已停止，请重新开始。'
    if 'control_interaction_disconnected' in reason: return '控制交互未连接，请重新开始。'
    if 'target_outside' in reason: return '目标超出当前控制范围。'
    if 'no_feedback_progress' in reason: return '没有检测到运动进展，已请求停车。'
    if 'wrong_direction' in reason: return '反馈方向异常，已请求停车。'
    if 'feedback' in reason: return '底盘反馈异常，未继续运动。'
    if 'timeout' in reason: return '控制超时，已请求停车。'
    if 'standstill' in reason: return '尚未确认静止，未执行。'
    if 'authorization' in reason: return '运动控制未授权，未执行。'
    if 'epoch' in reason or 'stale' in reason: return '控制状态已改变，未执行旧指令。'
    return '控制未完成，请查看具体错误。'


def speech_for(receipt):
    if receipt.get('kind') == 'wake' or receipt.get('reason') == 'wake_only': return '我在'
    if receipt.get('accepted') is True: return None
    reason = receipt.get('reason', '')
    if reason: return rejection(reason)
    return receipt.get('message') or '指令未执行，请查看原因。'


def terminal_failure(report):
    reason = report.get('stop_reason', '')
    # Normal completion and deliberate cancellation do not need announcements.
    if reason == 'metric_target_reached_stop_requested': return None
    if reason.startswith('metric_motion_rejected:') or reason in ('metric_start_rejected', 'control_loop_failure'):
        return rejection(reason)
    return None
