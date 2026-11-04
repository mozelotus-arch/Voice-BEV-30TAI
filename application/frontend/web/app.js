'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const token = document.querySelector('meta[name="control-token"]').content;
  let state = null, board = null, generation = 0, localStop = false, sending = false, settingsLoaded = false;
  let lastRows = '', connectionError = false;
  const labels = {pending:'正在思考…', completed:'', cancelled:'已取消', failed:'回复未完成'};
  const toolLabels={motion_capabilities:'检查控制范围',motion_request:'发送运动指令',motion_sequence:'执行顺序动作',motion_status:'确认运动状态',
    supported_commands:'查看支持指令',device_status:'读取设备状态',connect_bluetooth:'连接蓝牙',disconnect_bluetooth:'断开蓝牙',
    start_local_voice:'开启语音监听',stop_local_voice:'停止语音监听',load_voice_models:'加载模型',enable_text_mode:'切换文字模式',
    diagnose_workspace:'检查运行记录',stop_vehicle:'停止小车'};
  function motionView(row){
    const receipts=row.receipts||[],requests=receipts.filter(r=>r.name==='motion_request');
    const sequence=receipts.filter(r=>r.name==='motion_sequence').at(-1);
    const plan=row.motion_plan||sequence?.plan;
    if(plan)return {state:plan.state,title:{executing:'执行中',completed:'全部完成',interrupted:'已结束',cancelled:'已取消'}[plan.state]||'等待确认',
      message:plan.message||`正在执行第 ${Math.min(plan.completed+1,plan.total)}/${plan.total} 步。`,plan};
    if(sequence&&!requests.length)return {state:'rejected',title:'步骤未受理',message:sequence.message||'请查看执行详情。'};
    if(!requests.length)return null;
    const accepted=requests.find(r=>r.accepted===true),request=accepted||requests.at(-1);
    if(!accepted)return {state:'rejected',title:'指令未受理',message:request.message||'请查看执行详情。',command:request.command};
    const status=receipts.filter(r=>r.name==='motion_status').at(-1);
    const phase=status?.motion_state||(status?.control?.metric?.active?'executing':'accepted');
    const titles={accepted:'已受理',executing:'执行中',stopping:'正在停车',completed:'已完成',interrupted:'动作已中止',unconfirmed:'等待确认'};
    return {state:phase,title:titles[phase]||'等待确认',command:accepted.command,
      message:status?.motion_message||(phase==='executing'?'最近回执：运动正在执行。':'运动指令已受理，正在确认执行结果。')};
  }
  const uid = () => crypto.randomUUID().replaceAll('-', '');
  const setText = (id, text) => { $(id).textContent = text; };
  function error(message) { $('error').hidden = !message; setText('error', message || ''); }
  async function request(path, data) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const options = {signal:controller.signal, cache:'no-store'};
      if (data !== undefined) Object.assign(options, {method:'POST', headers:{'Content-Type':'application/json','X-Control-Token':token}, body:JSON.stringify(data)});
      const response = await fetch(path, options);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '请求失败。');
      return result;
    } catch (e) {
      if (e.name === 'AbortError') throw new Error('本机请求超时，结果尚未确认；没有自动重发。');
      throw e;
    } finally { clearTimeout(timer); }
  }
  const boardContext = () => board?.available ? {instance_id:board.state.instance_id, interaction_epoch:board.state.interaction.epoch} : null;
  const blocked = () => !state || state.busy || state.stop_pending || state.board_control_pending || state.stopped || localStop || sending || connectionError;
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function renderRows() {
    const rowsKey = JSON.stringify(state.rows);
    if (rowsKey !== lastRows) {
      lastRows = rowsKey;
      if (!state.rows.length) {
        if (!$('messages').querySelector('.welcome')) {
          const welcome = element('div', 'welcome');
          welcome.append(element('span','welcome-mark','雅'), element('h2','','开始一段新对话'), element('p','','可以聊天、提问，或一起梳理想法。可查询支持指令和设备状态。'));
          $('messages').replaceChildren(welcome);
        }
      } else {
        const container = $('messages');
        const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 100;
        const nodes = state.rows.map(row => {
          const node = element('article', 'message ' + row.role);
          const motion=motionView(row);
          node.append(element('div','message-label',row.role === 'user' ? '你' : '小雅 · ' + row.source));
          node.append(element('div','bubble',row.text || motion?.message || labels[row.status] || ''));
          if(motion){
            const card=element('div','motion-result '+motion.state);
            card.append(element('span','motion-badge',motion.title));
            if(motion.command)card.append(element('span','motion-command',motion.command));
            node.append(card);
            if(motion.plan){
              const steps=element('ol','motion-steps');
              const stepLabels={waiting:'等待执行',executing:'执行中',completed:'已完成',stopping:'正在停车',
                unconfirmed:'结果待确认',interrupted:'已中止',rejected:'未受理',cancelled:'已取消',not_sent:'未发送'};
              for(const step of motion.plan.steps){
                const item=element('li','motion-step '+step.state);
                item.append(element('span','step-command',step.command),element('span','step-state',stepLabels[step.state]||'等待确认'));
                if(step.message&&['unconfirmed','interrupted','rejected'].includes(step.state))item.append(element('small','',step.message));
                steps.append(item);
              }
              node.append(steps);
            }
          }
          if (row.error&&!motion) node.append(element('div','message-error',row.error));
          if(row.receipts?.length){
            const details=element('details','execution-details');
            details.append(element('summary','',`执行详情 · ${row.receipts.length} 条记录`));
            for (const receipt of row.receipts) {
              const execution=['motion_request','motion_sequence'].includes(receipt.name)?(receipt.accepted?'已受理':'未受理'):
                receipt.name==='motion_status'?'状态已返回':receipt.status==='rejected'?'未受理':
                receipt.hardware_executed===true?'已执行':'已读取回执';
              details.append(element('div','receipt',`${toolLabels[receipt.name]||'工具记录'} · ${execution}\n${receipt.message || receipt.reason || receipt.status}`));
            }
            if(row.reply_error||row.error)details.append(element('div','message-error','文字回复记录：'+(row.reply_error||row.error)));
            node.append(details);
          }
          if (row.role === 'assistant' && row.status === 'completed') {
            const actions = element('div','message-actions');
            const read = element('button','','由板卡朗读');
            read.dataset.read = row.id;
            read.addEventListener('click', () => mutate('/api/chat/read',{id:uid(),message_id:row.id,board_context:boardContext()}));
            actions.append(read);
            if (row.speech) {
              const s = row.speech;
              const text = {preparing:'准备朗读…',playing:`已完成 ${s.done}/${s.total} 段`,completed:`板端回执：${s.total} 段播放完成`,cancelled:'后续朗读已取消',failed:s.message}[s.state];
              actions.append(element('span','',text || s.state));
            }
            node.append(actions);
          }
          return node;
        });
        container.replaceChildren(...nodes);
        if (nearBottom || state.rows.at(-1)?.status === 'pending') container.scrollTop = container.scrollHeight;
      }
    }
    document.querySelectorAll('[data-read]').forEach(button => {
      button.disabled = blocked() || !board?.available || !board.state.can_speak || board.state.voice.mode !== 'text';
    });
  }
  function render() {
    if (!state) return;
    const config = state.settings;
    const stopped = localStop || state.stopped;
    $('demo-banner').hidden = config.mode !== 'demo';
    setText('model-badge',config.mode === 'demo' ? '离线演示' : config.configured ? config.model : '模型未配置');
    setText('model-description',config.mode === 'demo' ? '当前没有调用语言模型。' : config.configured ? '已配置：' + config.model : '填写服务地址、模型名和密钥后开始。');
    setText('key-state',config.key_present ? (config.key_source === 'environment' ? '已从本机环境变量读取密钥。' : '本次服务会话已设置密钥；未写入磁盘。') : '当前没有密钥。本机免密服务可留空。');
    if (!settingsLoaded) {
      $('provider-mode').value = config.mode; $('base-url').value = config.base_url; $('model').value = config.model;
      $('settings-details').open = !config.configured; settingsLoaded = true;
    }
    let status = config.configured ? '聊天就绪 · 不需要板卡连接' : '先配置模型，或选择离线演示。';
    if (state.busy) status = '正在处理当前对话或朗读…';
    if (stopped) status = state.busy ? '已停止；正在收尾，迟到结果不会继续显示或播报。' : '交互已停止。';
    if (state.stop_receipt) status += ' ' + state.stop_receipt.message;
    if (connectionError) status = '本机服务连接中断，请检查启动状态。';
    setText('chat-state',status);
    $('stopped-banner').hidden = !stopped;
    $('send').disabled = blocked() || !config.configured || Array.from($('message').value).length > 4000;
    $('new-chat').disabled = state.busy || sending || state.stop_pending || state.board_control_pending;
    $('resume-chat').disabled = state.busy || sending || state.stop_pending || state.board_control_pending;
    $('save-settings').disabled = state.busy || sending || state.stop_pending || state.board_control_pending;
    $('auto-read').disabled = blocked();
    setText('capability-count',state.capabilities.length + ' 项' + (state.capabilities.length ? ' · 已注册能力' : ' · 等待确定指令清单'));
    $('agent-tool-list').replaceChildren(...state.capabilities.map(tool=>element('li','',tool.function.name+'：'+tool.function.description)));
    renderBoard(); renderRows();
  }
  function apply(next, stamp) {
    if (stamp !== generation) return;
    if (state && state.instance_id !== next.instance_id) {
      connectionError = true;
      error('本机服务已重启，请手动刷新页面以更新连接。旧操作不会自动重发。');
      render(); return;
    }
    if (state?.instance_id === next.instance_id && next.revision < state.revision) return;
    state = next; connectionError = false; render();
  }
  function renderBoard() {
    const available = board?.available;
    const s = available ? board.state : null;
    const links = {connected:'已连接',connecting:'连接中',disconnected:'未连接',disconnecting:'断开中',error:'连接异常'};
    setText('board-badge',available ? '桥接在线' : '桥接离线');
    setText('board-description',available ? '蓝牙可加载板端模型、开启麦克风、传递文字与状态；识别和播报均在板端完成。' : (board?.message || '正在读取板端状态…'));
    setText('bt-state',s ? links[s.connection.state] || s.connection.state : '不可达');
    setText('shared-bt-state','共享蓝牙：'+(s ? links[s.connection.state] || s.connection.state : '未接入'));
    const loadJob = s?.job?.op === 'load' ? s.job : null;
    const loading = loadJob && ['queued','sending','running','playing'].includes(loadJob.state);
    setText('voice-state',s ? !s.voice.observed ? '未读取' : s.voice.ready ? '已加载' : loading ? '加载中…' : s.voice.failed ? '加载或运行失败' : '未加载' : '—');
    setText('mic-state',s?.voice.observed ? s.voice.mic?.open ? '正在监听' : '关闭' : '未确认');
    setText('player-state',s?.voice.observed ? s.voice.playing ? '正在播放' : '空闲' : '未确认');
    let feedback = '尚无板端反馈。';
    if (s?.voice.observed && s?.interaction.stopped) feedback = '板端交互已停止，蓝牙可保持连接。';
    if (loading) feedback = '正在加载语音模型，时间取决于板端初始化，请查看实际阶段。请等待完成；无需重复点击。';
    if (loadJob?.state === 'failed') feedback = '语音模型加载失败：' + (loadJob.error || loadJob.board_receipt?.error || '请查看板端反馈。');
    if (s?.voice_job) feedback += '\n' + (s.voice_job.text || '') + (s.voice_job.message ? '\n' + s.voice_job.message : '');
    if (s?.voice_job?.error) feedback += '\n' + s.voice_job.error;
    if (s?.voice.mic?.error) feedback += '\n麦克风：' + s.voice.mic.error;
    if (s?.connection.error || s?.voice.failed) feedback += '\n' + (s.connection.error || s.voice.failed);
    setText('board-feedback',feedback);
    document.querySelectorAll('[data-board]').forEach(button => {
      const action = button.dataset.board;
      button.disabled = blocked() || !available;
      if (action === 'connect') button.disabled ||= s?.connection.state === 'connected';
      if (action === 'disconnect') button.disabled ||= s?.connection.state === 'disconnected';
      if (action === 'load') button.disabled ||= !s?.can_load;
      if (action === 'mode') {
        button.disabled ||= !s?.can_mode;
        button.setAttribute('aria-pressed',String(s?.voice.mode === button.dataset.mode));
      }
    });
  }
  async function refresh() {
    const stamp = generation;
    try { apply(await request('/api/status'), stamp); }
    catch (e) { if (stamp === generation) { connectionError = true; render(); } }
  }
  async function refreshBoard() {
    const stamp = generation;
    try { const next = await request('/api/board/status'); if (stamp === generation) { board = next; render(); } }
    catch (e) { if (stamp === generation) { board = {available:false,message:'桥接状态读取失败。聊天可独立使用。'}; render(); } }
  }
  async function mutate(path, extra={}, resume=false) {
    if (!state || sending) return false;
    const stamp = ++generation;
    sending = true; error(''); render();
    try {
      const next = await request(path,{instance_id:state.instance_id,epoch:state.epoch,...extra});
      if (stamp !== generation) return false;
      if (resume) localStop = false;
      apply(next, stamp);
      return true;
    } catch (e) { if (stamp === generation) error(e.message); return false; }
    finally { if (stamp === generation) { sending = false; render(); refresh(); refreshBoard(); } }
  }
  $('composer').addEventListener('submit', async event => {
    event.preventDefault();
    if (blocked() || !state.settings.configured) return;
    const text = $('message').value.trim();
    if (!text) return;
    if (Array.from(text).length > 4000) { error('每条消息最多 4000 个字符。'); return; }
    if (await mutate('/api/chat/send',{id:uid(),text,auto_read:$('auto-read').checked,board_context:boardContext()})) {
      $('message').value = ''; setText('char-count','0 / 4000');
    }
  });
  $('message').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault(); $('composer').requestSubmit();
    }
  });
  $('message').addEventListener('input', () => { setText('char-count',`${Array.from($('message').value).length} / 4000`); render(); });
  $('settings-form').addEventListener('submit', async event => {
    event.preventDefault();
    const settings = {mode:$('provider-mode').value,base_url:$('base-url').value.trim(),model:$('model').value.trim(),api_key:$('api-key').value.trim(),clear_key:$('clear-key').checked};
    $('api-key').value = ''; $('clear-key').checked = false;
    if (await mutate('/api/settings',{settings})) { $('settings-details').open = !state.settings.configured; }
  });
  $('stop-all').addEventListener('click', async () => {
    const stamp = ++generation;
    localStop = true; sending = false; error(''); render();
    try { apply(await request('/api/interaction/stop',{}),stamp); }
    catch (e) { if (stamp === generation) error('本页已阻止新操作，但未确认服务器停止：' + e.message); }
    finally { refresh(); refreshBoard(); }
  });
  $('resume-chat').addEventListener('click', () => mutate('/api/interaction/resume',{},true));
  $('new-chat').addEventListener('click', () => mutate('/api/chat/clear'));
  document.querySelectorAll('[data-board]').forEach(button => button.addEventListener('click', () => {
    const extra = {action:button.dataset.board,board_context:boardContext()};
    if (button.dataset.mode) extra.mode = button.dataset.mode;
    mutate('/api/board/control',extra);
  }));
  document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => {
    $('message').value = button.dataset.prompt; $('message').dispatchEvent(new Event('input')); $('message').focus();
  }));
  refresh(); refreshBoard();
  setInterval(refresh,700); setInterval(refreshBoard,1500);
})();

// WORKSPACE_UI_START
