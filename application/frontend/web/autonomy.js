'use strict';
(() => {
  const token = document.querySelector('meta[name="control-token"]').content;
  const state = document.getElementById('autonomy-state');
  const input = document.getElementById('command-text');
  const feedback = document.getElementById('command-receipt');
  let context = null, pending = false, sequence = 0;
  async function refresh() {
    try {
      const reply = await fetch('/api/autonomy/status', {cache: 'no-store'});
      if (!reply.ok) throw new Error('状态不可达');
      context = await reply.json();
      input.placeholder = context.simulated ? '离线秒数模拟：前进0.5秒 / 停止' : '小雅小雅，前进一米 / 左转九十度 / 停车';
      const control = context.control;
      state.textContent = (context.simulated ? '离线模拟 · 未连接硬件 · ' : '集成候选 · 尚未验收 · ') +
        (control ? control.mode + ' / ' + control.reason : context.mode);
      document.getElementById('start-cruise').disabled = pending || !control || !control.autonomy_enabled;
    } catch (error) {
      context = null; state.textContent = '状态不可达';
      document.getElementById('start-cruise').disabled = true;
    }
  }
  async function submit(text) {
    const stopping = /^(?:小雅小雅[，,\s]*)?(?:停止|停车|停止全部)$/.test(text.trim()) || ['手动接管', '切换手动'].includes(text.trim());
    if (pending && !stopping) return;
    if ((!context || !context.control) && !stopping) {feedback.textContent = '请先连接并读取控制状态。'; return;}
    const modelText = !stopping && !context.simulated;
    if (modelText && (!context.voice?.ready || context.voice.stopped || context.voice.mode !== 'text' || context.voice.pending || context.voice.active != null)) {
      feedback.textContent = '请先恢复交互、切换文字模式、加载模型并等待空闲。'; return;
    }
    const ticket = ++sequence;
    const captured = {instance_id: context?.instance_id ?? null, epoch: context?.epoch ?? null,
      voice_epoch: context?.voice?.epoch ?? null};
    pending = true;
    try {
      const body = {instance_id: captured.instance_id, epoch: captured.epoch,
        control_epoch: context?.control?.epoch ?? null, text: stopping && text.includes('小雅小雅') ? '停车' : text};
      if (modelText) body.voice_epoch = captured.voice_epoch;
      const reply = await fetch(modelText ? '/api/autonomy/model-command' : '/api/autonomy/command', {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Control-Token': token},
        body: JSON.stringify(body)});
      const result = await reply.json();
      if (ticket !== sequence) return;
      feedback.textContent = reply.ok ? JSON.stringify(result) : result.error;
      if (reply.ok && modelText && Number.isInteger(result.result?.id)) {
        const deadline = Date.now() + 120000;
        while (Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, 500));
          if (ticket !== sequence) return;
          if (!context || context.instance_id !== captured.instance_id || context.epoch !== captured.epoch ||
              context.voice?.epoch !== captured.voice_epoch) throw new Error('context changed');
          const poll = await fetch('/api/autonomy/model-job', {method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-Control-Token': token},
            body: JSON.stringify({...captured, id: result.result.id})});
          const latest = await poll.json();
          if (ticket !== sequence) return;
          if (!poll.ok) throw new Error('job unavailable');
          feedback.textContent = JSON.stringify(latest);
          if (['completed','cancelled','failed'].includes(latest.result?.state)) return;
        }
        throw new Error('receipt timeout');
      }
    } catch (error) {if (ticket === sequence) feedback.textContent = '回执未知或状态已变化，未自动重发；需要时请停车。';}
    finally {if (ticket === sequence) {pending = false; await refresh();}}
  }
  document.getElementById('command-form').addEventListener('submit', event => {
    event.preventDefault(); submit(input.value);
  });
  document.getElementById('start-cruise').addEventListener('click', () => submit('开始自主巡航'));
  document.getElementById('take-manual').addEventListener('click', () => submit('手动接管'));
  refresh(); setInterval(refresh, 1500);
})();
