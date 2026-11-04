'use strict';
if (typeof window !== 'undefined' && document.getElementById('workspace-home')) (() => {
  const $=id=>document.getElementById(id), token=document.querySelector('meta[name="control-token"]').content;
  const pages={home:['功能主页',''],voice:['本地语音工作室','开始 / 停止监听，查看识别、理解与播报结果。'],bluetooth:['蓝牙加载与文字','连接、加载模型与文字交流；监听开关位于语音页面。'],chat:['大模型聊天','聊天独立可用；查询设备能力时读取实际清单与状态。'],vehicle:['小车遥控','先确认通路与反馈，再按住操纵。'],vision:['视觉与鸟瞰图','显示板端的真实任务与返回结果。'],settings:['连接设置','管理语言模型接口与当前会话密钥。']};
  const aliases={'vehicle-title':'vehicle','local-voice-title':'voice','chat-title':'chat','board-title':'bluetooth'};
  let voiceLog=[],carLog=[],lastVoice='',lastCar='',busy=false,cancelVersion=0,loadingSince=null,textJob=null,latestLocal=null,progressClock=null;
  const stamp=()=>new Date().toLocaleTimeString('zh-CN',{hour12:false});
  const text=(id,value)=>{if($(id))$(id).textContent=value;};
  const node=(tag,value)=>{const n=document.createElement(tag);n.textContent=value;return n;};
  function route(){
    const key=location.hash.slice(1),page=aliases[key]|| (pages[key]?key:'home');
    window.dispatchEvent(new CustomEvent('workspace-route',{detail:page}));
    document.querySelectorAll('main [data-page]').forEach(n=>n.hidden=!n.dataset.page.split(' ').includes(page));
    $('page-heading').hidden=page==='home';text('page-name',pages[page][0]);text('page-description',pages[page][1]);
    document.querySelectorAll('.workspace-nav a').forEach(n=>n.setAttribute('aria-current',n.hash==='#'+page?'page':'false'));
    document.body.dataset.page=page;window.scrollTo(0,0);
  }
  window.addEventListener('hashchange',route);route();
  async function api(path,body){
    const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),10000);
    try{
      const r=await fetch(path,{cache:'no-store',signal:controller.signal,...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':token},body:JSON.stringify(body)})});
      const d=await r.json();if(!r.ok)throw Error(d.error||'请求失败');return d;
    }finally{clearTimeout(timer);}
  }
  const ctx=s=>({instance_id:s.instance_id,interaction_epoch:s.interaction.epoch});
  function log(rows,id,message){rows.push({time:stamp(),message});if(rows.length>200)rows.shift();$(id).replaceChildren(...rows.slice(-40).reverse().map(r=>node('li',r.time+' · '+r.message)));}
  function progress(s){
    const v=s?.voice||{}, p=v.load_progress, running=['running','queued','sending','playing'].includes(s?.job?.state);
    const failure=v.failed||(s?.job?.state==='failed'?(s.job.error||'加载任务失败'):null);
    if(p){
      $('load-progress').max=p.total_stages;
      $('load-progress').value=p.completed_stages;
      text('load-label',failure?'加载未完成：'+failure:v.ready?'模型加载完成':p.label);
      if(!progressClock||progressClock.started!==p.started_at||progressClock.updated!==p.updated_at)progressClock={started:p.started_at,updated:p.updated_at,observed:Date.now()};
      const elapsed=Math.max(0,p.updated_at-p.started_at)+(p.complete||failure?0:(Date.now()-progressClock.observed)/1000);
      text('load-elapsed',`${p.completed_stages}/${p.total_stages} 阶段 · 已观察约 ${Math.round(elapsed)} 秒`);
      text('load-explanation',failure?'已保留最后阶段与错误回执。':'进度按板端已完成阶段计数；不同阶段耗时不同，不代表剩余时间比例。');
      $('load-stages').replaceChildren(...(p.records||[]).map(r=>node('li',`+${Math.max(0,Math.round(r.time-p.started_at))}秒 ${r.completed?'✓':'…'} ${r.label}`)));
    }else if(v.ready){
      $('load-progress').max=1;$('load-progress').value=1;text('load-label','模型已加载');text('load-elapsed','板端已确认');
      text('load-explanation','当前服务未提供此前各阶段的时间记录；模型就绪来自实时回执。');
    }else if(running){
      loadingSince??=Date.now();$('load-progress').removeAttribute('value');
      text('load-label',s.job.phase||'板端正在加载');text('load-elapsed',`本页已等待 ${Math.floor((Date.now()-loadingSince)/1000)} 秒`);
      text('load-explanation','等待板端阶段回执；此时不显示猜测的百分比。请勿重复加载。');
    }else{loadingSince=null;$('load-progress').value=0;text('load-label',failure||s?.job?.error||'等待开始');}
  }
  const examples=[['前进 / 后退','前进二厘米 / 向前走二厘米','必须说明距离与单位；范围取当前板端配置'],['左转 / 右转','左转十度 / 向左转十度','必须说明角度；不支持横移'],['停车','停车','请求停车，静止以回读为准'],['自动避障','开始自动避障','语义已定义，执行尚未开放']];
  $('voice-command-list').replaceChildren(...examples.map(([title,example,note])=>{const card=node('div','');card.append(node('strong',title),node('p',example),node('small',note));return card;}));
  $('export-voice-log').addEventListener('click',()=>{
    const a=document.createElement('a'),url=URL.createObjectURL(new Blob([JSON.stringify(voiceLog,null,2)],{type:'application/json'}));a.href=url;a.download='xiaoya-voice-events.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  async function refresh(){
    const results=await Promise.allSettled([api('/api/local-voice/status'),api('/api/board/status'),api('/api/vehicle/status')]);
    const local=results[0].status==='fulfilled'?results[0].value.state:null, b=results[1].status==='fulfilled'?results[1].value.state:null,v=results[2].status==='fulfilled'?results[2].value:null;
    latestLocal=local;
    text('home-bt','蓝牙：'+({connected:'已连接',connecting:'连接中',disconnected:'未连接',error:'连接异常'}[b?.connection?.state]||'状态不可用'));
    const voice=local?.voice?.observed?local.voice:b?.voice?.observed?b.voice:null;
    text('home-voice','语音：'+(!voice?'未连接，尚未读取模型状态':voice.mic?.open?'正在聆听':voice.ready?'模型已加载':'尚未加载'));
    text('home-car','小车：'+(v?.armed?'控制中':v?.ready?'可按住操纵':'运动未就绪'));
    // A configured but disconnected LAN entry must not mask Bluetooth loading.
    progress(local?.active_request?local:b?.active_request?b:local?.voice?.observed?local:b?.voice?.observed?b:local||b);
    const stateLine=[b?.connection?.state,voice?.ready,voice?.stopped,voice?.mode,voice?.mic?.phase,voice?.playing,voice?.load_progress?.label,(local?.voice_job||b?.voice_job)?.id,(local?.voice_job||b?.voice_job)?.state,b?.connection?.error].join('|');
    if(stateLine!==lastVoice){
      lastVoice=stateLine;const j=local?.voice_job||b?.voice_job;
      log(voiceLog,'voice-timeline',`蓝牙 ${b?.connection?.state||'未读取'}；模型${voice?.ready?'就绪':'未就绪'}；${voice?.stopped?'语音已停止':voice?.mode||'未确认'}；麦克风 ${voice?.mic?.phase||'未确认'}${j?.text?'；识别：'+j.text:''}${j?.message?'；回执：'+j.message:''}${j?.playback_completed?'；播放完成':''}${b?.connection?.error?'；'+b.connection.error:''}`);
    }
    text('bt-load-detail',!b?.voice?.observed?'蓝牙尚未读取模型状态；连接后确认，未连接不代表模型未加载。':b?.voice?.load_progress?`加载：${b.voice.load_progress.label}（${b.voice.load_progress.completed_stages}/${b.voice.load_progress.total_stages} 阶段）`:b?.voice?.ready?'模型已经驻留，不需要再次加载。':'等待加载请求与板端回执。');
    if(v){
      const a=v.adapter||{}, checks=[['蓝牙连接',a.connected],['PC 控制会话',a.transport_validated],['板端允许运动',a.gateway_ready],['测速反馈新鲜',a.feedback_fresh||a.mode==='mock'],['已确认静止',a.standstill_observed],['全局交互已启用',!v.global_stopped]];
      $('vehicle-checklist').replaceChildren(...checks.map(([name,ok])=>{const n=node('div',(ok?'✓ ':'○ ')+name+' · '+(ok?'通过':'未通过'));n.className=ok?'check-ok':'check-wait';return n;}));
      const blockers=checks.filter(([,ok])=>!ok).map(([name])=>name);
      text('vehicle-blockers',v.armed?'正在按住控制；松开请求停车':blockers.length?'当前不能运动：'+blockers.join('、')+'。':'已就绪：持续按住方向，松手停止。');
      const carLine=[a.connected,a.transport_validated,a.gateway_ready,v.armed,v.action,v.reason,v.stop_receipt?.message].join('|');
      if(lastCar!==carLine){lastCar=carLine;log(carLog,'vehicle-timeline',`蓝牙${a.connected?'已连接':'未连接'}；${v.armed?'已申请控制':'未申请控制'}；${v.action||'无方向输入'}；${v.stop_receipt?.message||v.reason||''}`);}
    }else{text('vehicle-blockers','无法读取车辆状态，不能确认运动条件。');}
  }
  async function waitFor(test,version,timeout=360000){
    const end=Date.now()+timeout;
    while(Date.now()<end){if(version!==cancelVersion)throw Error('切换已取消');const result=await test();if(result)return result;await new Promise(r=>setTimeout(r,500));}
    throw Error('等待板端超时，请查看加载和错误记录；不会自动重试。');
  }
  async function switchMode(mode){
    if(busy||window.voiceStopPending)return;busy=true;window.voiceActionPending=true;const version=++cancelVersion;
    window.dispatchEvent(new CustomEvent('voice-bluetooth-requested'));
    $('switch-text').disabled=$('load-bt-models').disabled=$('start-bt-voice').disabled=true;
    const report=t=>{text(mode==='load'?'load-model-status':'mode-switch-status',t);if(mode!=='load')text('voice-mode-switch-status',t);log(voiceLog,'voice-timeline',t);};
    async function control(action,extra={}){
      if(version!==cancelVersion)throw Error('切换已取消');
      const [c,b]=await Promise.all([api('/api/status'),api('/api/board/status')]);
      if(c.stopped)throw Error('全部交互已停止，请先在大模型聊天页恢复交互。');
      if(version!==cancelVersion)throw Error('切换已取消');
      await api('/api/board/control',{instance_id:c.instance_id,epoch:c.epoch,action,board_context:ctx(b.state),...extra});
    }
    try{
      await api('/api/vehicle/status?view=voice');
      let b=(await api('/api/board/status')).state;
      if(b.connection.state!=='connected'){report('1 · 连接蓝牙');await control('connect');await waitFor(async()=>{const s=(await api('/api/board/status')).state;if(s.connection.state==='error')throw Error(s.connection.error);return s.connection.state==='connected';},version,45000);}
      report('2 · 通过蓝牙读取当前语音状态');
      b=await waitFor(async()=>{const s=(await api('/api/board/status')).state;if(s.connection.error)throw Error(s.connection.error);return s.voice.observed?s:null;},version,15000);
      if(mode==='load'&&b.voice.ready){report('模型已加载，无需重复加载；监听状态保持不变。');return;}
      if(mode!=='load'&&!b.voice.ready)throw Error('模型尚未加载，请先到蓝牙页面点击“加载板端模型”。切换模式不会自动加载。');
      if(mode==='load'&&(b.active_request||b.voice.active||b.voice.pending||b.voice.local_active)){
        report('正在等待已有加载任务，不重复提交');
        await waitFor(async()=>{const s=(await api('/api/board/status')).state;if(s.voice.failed||s.job?.state==='failed')throw Error(s.voice.failed||s.job.error);return s.voice.observed&&s.voice.ready&&!s.active_request&&!s.voice.active;},version);
        report('模型加载完成；可前往语音页面点击“开始监听”。');return;
      }
      // The board permits stop_all over SPP even when LAN owns the microphone.
      // Never depend on the unavailable LAN route to hand ownership to Bluetooth.
      if(b.voice.local_active){
        report('2 · 通过蓝牙停止旧语音，等待麦克风与当前任务结束');
        await control('stop');
        b=await waitFor(async()=>{const s=(await api('/api/board/status')).state;if(s.connection.error)throw Error(s.connection.error);return s.voice.observed&&s.voice.stopped&&!s.voice.local_active&&!s.interaction.stop_pending&&!s.voice.pending&&!s.voice.active&&!s.voice.mic?.running?s:null;},version,30000);
      }
      if(b.interaction.stopped||b.voice.stopped){report('3 · 解除语音停止状态');await control('resume');await waitFor(async()=>{const s=(await api('/api/board/status')).state;if(s.connection.error)throw Error(s.connection.error);return !s.interaction.stopped&&!s.voice.stopped;},version,30000);}
      report('4 · 读取当前模型状态，等待已有任务结束');
      b=await waitFor(async()=>{
        await api('/api/vehicle/status?view=voice');
        const s=(await api('/api/board/status')).state;
        if(s.voice.failed||s.job?.state==='failed')throw Error(s.voice.failed||s.job.error);
        return s.voice.observed&&(s.can_load||s.can_mode)&&!s.active_request&&!s.voice.active&&!s.voice.pending?s:null;
      },version);
      if(mode==='load'){
        if(!b.voice.ready){report('4 · 加载板端模型，进度以板端回执为准');await control('load');await waitFor(async()=>{const s=(await api('/api/board/status')).state;if(s.voice.failed||s.job?.state==='failed')throw Error(s.voice.failed||s.job.error);return s.voice.observed&&s.voice.ready&&!s.active_request&&!s.voice.active;},version);}
        report('模型加载完成，尚未开启监听；请前往语音页面点击“开始监听”。');return;
      }
      report('5 · 切换'+(mode==='text'?'文字模式':'板端麦克风'));await control('mode',{mode});
      await waitFor(async()=>{const s=(await api('/api/board/status')).state;return s.voice.mode===mode&&!s.voice.stopped&&(mode!=='voice'||s.voice.mic.running);},version,15000);
      report(mode==='text'?'文字模式已启用，可以输入指令或朗读聊天回复。':'板端语音已开启；本次启动与状态查询通过蓝牙。');
    }catch(e){report('未完成：'+e.message);}
    finally{busy=false;window.voiceActionPending=false;$('switch-text').disabled=$('load-bt-models').disabled=$('start-bt-voice').disabled=Boolean(window.voiceStopPending);refresh();}
  }
  $('switch-text').addEventListener('click',()=>switchMode('text'));
  $('load-bt-models').addEventListener('click',()=>switchMode('load'));
  $('start-bt-voice').addEventListener('click',()=>switchMode('voice'));
  $('stop-all').addEventListener('click',()=>{cancelVersion++;textJob=null;});
  $('local-voice-stop').addEventListener('click',()=>cancelVersion++);
  window.addEventListener('voice-lan-start-requested',()=>cancelVersion++);
  window.addEventListener('voice-stop-requested',()=>{cancelVersion++;textJob=null;});
  window.addEventListener('voice-stop-settled',()=>{if(!busy)$('switch-text').disabled=$('load-bt-models').disabled=$('start-bt-voice').disabled=false;});
  document.querySelectorAll('[data-board="stop"], [data-board="disconnect"]').forEach(button=>button.addEventListener('click',()=>cancelVersion++));
  $('board-text-form').addEventListener('submit',async e=>{
    e.preventDefault();if(textJob)return;
    const content=$('board-text').value.trim();if(!content)return;
    textJob='pending';$('board-text-send').disabled=true;
    try{
      const s=await api('/api/autonomy/status');
      if(!s.connected||!s.voice?.ready||s.voice.stopped||s.voice.mode!=='text')throw Error('请先点击“启用蓝牙文字模式”。');
      const context={instance_id:s.instance_id,epoch:s.epoch,voice_epoch:s.voice.epoch};
      const reply=await api('/api/autonomy/model-command',{...context,control_epoch:s.control.epoch,text:content});
      const j=reply.result;
      if(!j.id){text('board-text-result',j.message||JSON.stringify(j));return;}
      textJob=j.id;text('board-text-result','文字已送达板端，等待 BERT 解析与播报…');
      const version=cancelVersion;
      const done=await waitFor(async()=>{const d=await api('/api/autonomy/model-job',{...context,id:j.id});const job=d.result;return ['completed','failed','cancelled'].includes(job.state)?job:null;},version,90000);
      text('board-text-result',[done.text,done.message,done.error,done.control_receipt?.reason,done.speech_policy==='wake_or_error_only'&&!done.spoken_message?'正常指令静默处理':done.playback_completed?'板端播放完成':'未确认播放完成'].filter(Boolean).join('\n'));
      log(voiceLog,'voice-timeline','文字输入：'+content+'；'+(done.message||done.error||done.state));
    }catch(e){text('board-text-result',e.message);}finally{textJob=null;$('board-text-send').disabled=false;}
  });
  refresh();setInterval(refresh,1200);
})();
