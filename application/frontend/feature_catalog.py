"""Reviewed capability inventory: grammar recognition is not motion authority."""
def catalog():
    return dict(
        wake_phrase='小雅小雅',
        voice_commands=[
            dict(example='前进二厘米', form='前进/后退（或向前走/向后走）+ 距离 + 米/厘米', status='须控制会话授权与反馈就绪；实际距离范围查询 motion_capabilities，本地语音无需蓝牙'),
            dict(example='左转十度', form='左转/右转（或向左转/向右转）+ 角度 + 度', status='实际角度范围查询 motion_capabilities；编码器台架不代表地面标定'),
            dict(example='停车', form='停车', status='请求停车；必须看新鲜反馈确认静止'),
            dict(example='开始自动避障', form='开始自动避障', status='语义已定义；自动避障未开放'),
        ],
        rules=['小雅小雅是单独的唤醒词，回答我在；唤醒后的指令无需重复前缀；停止语音后结束会话', '文字模式直接输入指令，不需要声学唤醒词', '缺少距离、角度或单位时不运动', '不支持横移或否定指令；本地语音和蓝牙文字每次一条运动指令，大模型聊天支持 2–6 步明确顺序指令',
               'ASR 原文、BERT 结果、控制回执、播放完成分别记录'],
        modes=[
            dict(name='本地语音', description='板端麦克风→ASR→BERT→TTS→音箱；蓝牙页加载模型，语音页开始/停止监听；保留网线备用入口'),
            dict(name='蓝牙文字', description='通过蓝牙加载模型、输入文字指令或朗读文字；也支持通过蓝牙开启板端麦克风'),
            dict(name='大模型聊天', description='DeepSeek 网络 API；可拆解明确的顺序运动指令，通过蓝牙逐步执行，每步确认停车后继续；文字聊天独立于板卡连接'),
            dict(name='手动遥控', description='共用蓝牙连接；有效控制会话、反馈新鲜且静止后，按住方向申请控制，松手停止'),
        ],
        exclusivity='本地语音与蓝牙文字/蓝牙麦克风互斥；蓝牙连接可与本地语音共存；纯文字大模型聊天可独立使用；手动运动时不能开始或切换语音任务。',
        voiceprint=dict(available=False, reason='当前服务未接入声纹录入和验证；没有已验证阈值'),
        autonomous_motion=False, voice_motion='conditional_current_authorization',
        evidence='此清单说明已接入接口和指令规则；不代表每条语音识别或实车动作已经验收')


def register_workspace(registry, board, vehicle, local_voice):
    empty = dict(type='object', properties={}, required=[], additionalProperties=False)
    def receipt(message, **data):
        return dict(status='completed', message=message, hardware_executed=False, **data)
    registry.register('supported_commands', '查询小雅支持的语音指令、模式关系和功能边界；询问能做什么时使用。',
                      empty, lambda args, guard: receipt('已读取实际功能清单', catalog=catalog()))
    def status(args, guard):
        guard()
        b = board.status(); v = vehicle.snapshot()
        voice = (b.get('state') or {}).get('voice', {})
        local = local_voice.status() if local_voice else {}
        return receipt('已读取当前状态；蓝牙连接不代表运动就绪',
                       bluetooth=(b.get('state') or {}).get('connection'),
                       voice={k:voice.get(k) for k in ('ready','stopped','mode','mic','failed')},
                       local_voice={k:local.get(k) for k in ('interaction','can_start')} | dict(
                           voice={k:(local.get('voice') or {}).get(k) for k in ('ready','stopped','mode','awake','active','failed','mic')},
                           phase=(local.get('job') or {}).get('phase')),
                       vehicle={k:v.get(k) for k in ('ready','armed','reason','adapter')},
                       motion_interpretation={'armed':'手动遥控是否正在持有控制；定距离/定角度命令无需先手动使能',
                           'metric_preconditions':'查询实际范围；本轮明确指令由控制器检查会话、静止和板端新鲜反馈',
                           'feedback_age':'此处是PC网关收到的反馈快照；过期时不能据此确认当前车辆状态'})
    registry.register('device_status', '读取蓝牙、语音模型、麦克风与车辆当前状态；不连接或启动车辆。', empty, status)
    def stop(args, guard):
        from workspace_agent import explicit_request
        explicit_request(guard, [r'(?:让小车|让车|小车)?(?:停车|停止)', r'停下小车'])
        guard()
        state = vehicle.stop('model_explicit_stop')
        return receipt('已请求停车；请以新鲜测速反馈确认静止', stop_receipt=state.get('stop_receipt'))
    registry.register('stop_vehicle', '仅当用户明确要求停车时使用。请求停车，不启动、不恢复运动。', empty, stop, mutating=True)
