'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const token = document.querySelector('meta[name="control-token"]').content;
  const buttons = Array.from(document.querySelectorAll('[data-drive]'));
  const names = {forward:'前进',backward:'后退',rotate_left:'原地左转',rotate_right:'原地右转',front_left:'前左弧线',front_right:'前右弧线',back_left:'后左弧线',back_right:'后右弧线'};
  let state = null, observed = 0, generation = 0, stopped = true, lease = null;
  let held = null, seq = 0, inFlight = false, stopping = false, arming = false, connecting = false;
  const setText = (id,text) => { $(id).textContent = text; };
  const context = () => ({instance_id:state?.instance_id,epoch:state?.epoch});
  const ownsControl = () => Boolean(held || arming || lease);
  const usable = () => state?.ready && state.armed && !state.global_stopped && !stopped && lease && performance.now()-observed < 700;
  const canStart = () => state?.ready && state.adapter?.standstill_observed && !state.armed && !state.global_stopped && !document.hidden && !held && !arming && !stopping && !connecting && performance.now()-observed < 700;
  function error(message) { $('vehicle-error').hidden=!message;setText('vehicle-error',message||''); }
  function render() {
    const s=state, a=s?.adapter;
    if(a?.preset && !held && !arming){
      const preset=a.preset,limits=a.limits;
      if(limits){$('vehicle-speed').replaceChildren(new Option(`${limits.linear_mm_s} mm/s · ${limits.angular_mrad_s} mrad/s`,preset));}
    }
    const mode=a?.mode==='mock'?'离线演示 · 非真车':s?.ready?'通路已就绪':'等待控制网关';
    $('vehicle-panel').classList.toggle('is-mock',a?.mode==='mock');
    setText('vehicle-mode',mode);
    setText('vehicle-message',a?.message||'车辆通路尚未接入。聊天功能可独立使用。');
    setText('drive-action',held ? names[held.action]+(arming?' · 正在申请':' · 按住中') : stopping?'正在请求停车':canStart()?'按住方向操纵':'等待就绪');
    setText('vehicle-connection',a?.connected?'已连接':'未连接');
    setText('vehicle-can',({simulated:'模拟',active:'正常',feedback_fresh:'已收到新鲜反馈',down:'关闭',error:'异常',unobserved:'未确认',unavailable:'未确认'})[a?.can_state]||a?.can_state||'—');
    const age=typeof a?.feedback_age_ms==='number'?Math.max(0,Math.round(a.feedback_age_ms+performance.now()-observed)):null;
    setText('vehicle-freshness',age===null?'反馈未确认':`反馈年龄上界 ${age} ms`);
    setText('vehicle-vx',typeof a?.vx_mm_s==='number'?a.vx_mm_s:'—');
    setText('vehicle-wz',typeof a?.wz_mrad_s==='number'?a.wz_mrad_s:'—');
    setText('vehicle-battery',typeof a?.battery_mv==='number'?(a.battery_mv/1000).toFixed(2):'—');
    const feedbackFresh=a?.mode==='gateway'?a.feedback_fresh&&a.gateway_status_age_ms+performance.now()-observed<=250:age!==null&&age<=200;
    setText('vehicle-standstill',a?.standstill_observed&&feedbackFresh?'已观察静止':'未确认静止');
    setText('vehicle-stop-status',s?.stop_receipt?.message||'停止请求与静止反馈分别显示。');
    $('vehicle-connect').disabled=!a?.gateway_available||a?.connected||s?.global_stopped||stopping||arming||connecting;
    $('vehicle-disconnect').disabled=!a?.gateway_available||!a?.connected||stopping||arming||connecting;
    $('vehicle-speed').disabled=Boolean(held)||arming||stopping;
    buttons.forEach(button=>{button.disabled=!(canStart()||(held?.button===button&&(arming||usable())));button.classList.toggle('is-held',held?.button===button);});
  }
  async function request(path, data, timeout=1200, keepalive=false) {
    const controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),timeout);
    const options={signal:controller.signal,cache:'no-store',keepalive};
    if(data!==undefined)Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':token},body:JSON.stringify(data)});
    try {
      const response=await fetch(path,options);const value=await response.json();
      if(!response.ok)throw new Error(value.error||'车辆请求被拒绝。');
      return value;
    } finally {clearTimeout(timer);}
  }
  function apply(next, stamp) {
    if(stamp!==generation)return;
    if(state?.instance_id===next.instance_id&&next.revision<state.revision)return;
    const contextChanged=state&&(state.instance_id!==next.instance_id||state.epoch!==next.epoch);
    state=next;observed=performance.now();
    if(contextChanged&&ownsControl()) {stop('gateway_changed');return;}
    if(ownsControl()&&(!next.ready||next.global_stopped||((lease||held&&!arming)&&!next.armed))) {stop('readiness_lost');return;}
    render();
  }
  async function stop(reason='operator', keepalive=false) {
    const stamp=++generation;
    const pointer=held;held=null;lease=null;stopped=true;arming=false;connecting=false;inFlight=false;stopping=true;
    if(pointer&&pointer.button.hasPointerCapture?.(pointer.id))pointer.button.releasePointerCapture(pointer.id);
    render();
    try {
      const next=await request('/api/vehicle/stop',{reason},1500,keepalive);
      if(stamp!==generation)return;
      state=next;observed=performance.now();
      if(!next.stop_receipt?.acknowledged&&!next.stop_receipt?.requested)error(next.stop_receipt?.message||'停止请求尚未确认。');
    } catch(unused) {
      if(stamp===generation)error('停止请求未收到回执；本页已停止续发，禁止自动恢复。');
    } finally {if(stamp===generation){stopping=false;render();}}
  }
  async function begin(button,event) {
    if(event.button!==0||!event.isPrimary||!canStart())return;
    held={id:event.pointerId,action:button.dataset.drive,button};
    button.setPointerCapture(event.pointerId);
    const stamp=++generation;arming=true;error('');render();
    try {
      const next=await request('/api/vehicle/arm',context(),2000);
      if(stamp!==generation||!held||document.hidden)return;
      if(!next.armed||!next.lease)throw new Error('没有收到本页控制许可。');
      state=next;lease=next.lease;seq=0;stopped=false;observed=performance.now();
      arming=false;render();pulse();
    } catch(e) {if(stamp===generation){error(e.message);stop('arm_failed');}}
    finally {if(stamp===generation){arming=false;render();}}
  }
  async function pulse() {
    if(!held||arming)return;
    if(!usable()){stop('stale_state');return;}
    if(inFlight)return; // No queue and no parallel hold requests.
    const stamp=generation, action=held.action;
    inFlight=true;
    try {
      const next=await request('/api/vehicle/hold',{...context(),lease,seq:++seq,action,preset:$('vehicle-speed').value,sent_at_ms:Date.now()},280);
      apply(next,stamp);
    } catch(unused) {if(stamp===generation){error('控制回执超时或失败，已停止续发。');stop('hold_failed');}}
    finally {if(stamp===generation)inFlight=false;}
  }
  buttons.forEach(button=>{
    button.addEventListener('pointerdown',event=>{
      event.preventDefault();
      begin(button,event);
    });
    const release=event=>{if(held?.id===event.pointerId)stop(event.type||'release');};
    button.addEventListener('pointerup',release);
    button.addEventListener('pointercancel',release);
    button.addEventListener('lostpointercapture',release);
    button.addEventListener('contextmenu',event=>event.preventDefault());
    button.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();error('使用鼠标或触摸按住方向按钮，松开即停止。');}});
  });
  window.addEventListener('pointerup',event=>{if(held?.id===event.pointerId)stop('pointerup');});
  window.addEventListener('pointercancel',event=>{if(held?.id===event.pointerId)stop('pointercancel');});
  window.addEventListener('workspace-route',event=>{
    if(event.detail!=='vehicle'&&ownsControl())stop('route_changed');
    request('/api/vehicle/status?view='+(event.detail==='vehicle'&&!document.hidden?'manual':'other')).catch(()=>{});
  });
  window.addEventListener('blur',()=>{if(ownsControl())stop('blur');});
  window.addEventListener('pagehide',()=>{
    if(ownsControl())stop('pagehide',true);
    if(document.body?.dataset?.page==='vehicle')request('/api/vehicle/status?view=other',undefined,1200,true).catch(()=>{});
  });
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden&&ownsControl())stop('hidden',true);
    if(document.hidden&&document.body?.dataset?.page==='vehicle')request('/api/vehicle/status?view=other').catch(()=>{});
  });
  window.addEventListener('keydown',event=>{if(event.key==='Escape'&&ownsControl())stop('escape');});
  $('vehicle-stop').addEventListener('click',()=>stop());
  $('vehicle-connect').addEventListener('click',async()=>{
    if(!state||arming||stopping||connecting)return;
    const stamp=++generation;connecting=true;error('');render();
    try{const next=await request('/api/vehicle/connect',context(),38000);if(stamp===generation){state=next;observed=performance.now();}}
    catch(e){if(stamp===generation)error(e.message);}
    finally{if(stamp===generation){connecting=false;render();}}
  });
  $('vehicle-disconnect').addEventListener('click',async()=>{
    await stop('disconnect');const stamp=++generation;
    try{apply(await request('/api/vehicle/disconnect',{},8000),stamp);}
    catch(e){if(stamp===generation)error(e.message);}
  });
  $('vehicle-speed').addEventListener('change',()=>{if(ownsControl())stop('speed_changed');});
  $('stop-all').addEventListener('click',()=>stop('stop_all'));
  async function poll() {
    const stamp=generation;
    const manualView=!document.hidden&&document.body?.dataset?.page==='vehicle';
    try{apply(await request('/api/vehicle/status'+(manualView?'?view=manual':'')),stamp);}
    catch(unused){if(stamp===generation&&ownsControl())stop('status_failed');else if(stamp===generation){stopped=true;if(state)state={...state,ready:false};render();}}
  }
  poll();setInterval(poll,250);setInterval(pulse,100);
})();
