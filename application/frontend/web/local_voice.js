'use strict';
(() => {
  const get=id=>document.getElementById(id);
  const token=document.querySelector('meta[name="control-token"]').content;
  let state=null, boardState=null, lanAvailable=false, pending=false, sequence=0, refreshSequence=0, operationMessage='';
  function observedState(){
    if(boardState?.voice?.observed&&!boardState.voice.local_active)return boardState;
    if(state?.voice?.observed)return state;
    return boardState?.voice?.observed?boardState:null;
  }
  function settled(s){
    const v=s?.voice;
    return Boolean(v?.observed&&v.stopped&&!s.interaction?.stop_pending&&!v.pending&&v.active==null&&!v.playing&&!v.mic?.running&&!v.mic?.open);
  }
  function render(){
    const current=observedState(),v=current?.voice,job=current?.job;
    get('local-voice-start').disabled=pending||window.voiceStopPending||window.voiceActionPending||!lanAvailable||!state?.can_start;
    get('local-voice-stop').disabled=Boolean(window.voiceStopPending);
    get('local-voice-state').textContent=!v?'等待板端状态':
      settled(current)?'监听已停止 · 模型'+(v.ready?'保持加载':'尚未加载'):
      v.stopped?'正在结束监听与播报':v.mic?.phase==='input_quiet'?'录音过长，等待停顿后继续监听':v.mic?.open?(v.awake?'持续对话中，请直接说指令':'正在听，请先说小雅小雅')+' · '+(v.local_active?'网线控制':'蓝牙控制'):
      v.mic?.running?'麦克风启动中':v.playing?'正在播报':job?.state==='running'?(job.phase||'正在处理'):
      v.ready?'模型已加载 · 监听未开启':'模型尚未加载，请先前往蓝牙页面加载';
    get('voice-operation-result').textContent=operationMessage;
    const receipt=current?.voice_job;
    const timing=receipt?.timing;
    const duration=timing?Object.entries(timing).filter(([k,v])=>typeof v==='number'&&k.endsWith('seconds')).map(([k,v])=>`${({asr_seconds:'ASR',nlu_seconds:'BERT',tts_seconds:'TTS',capture_to_play_request_seconds:'收音结束到请求播放',playback_and_tail_seconds:'播放及尾部保护',total_processing_seconds:'整轮处理',audio_seconds:'音频时长'})[k]||k} ${v.toFixed(3)} 秒`).join(' · '):null;
    const reasons={control_interaction_stopped:'控制交互已停止',control_interaction_disconnected:'控制交互未连接',stale_control_epoch:'识别期间控制状态改变',voice_result_timeout:'识别结果超过执行时限',motion_authorization_unavailable:'运动控制会话未授权',motion_feedback_unavailable:'底盘反馈不新鲜',unsupported_command:'识别或输入的文字不在支持范围',voice_result_stale_or_stopped:'旧服务返回：控制已停止或结果过期'};
    const reason=receipt?.control_receipt?.reason;
    const terminal=v?.motion?.last_metric_report;
    get('local-voice-result').textContent=[receipt?.text?'识别/输入原文：'+receipt.text:null,receipt?.message?'处理结果：'+receipt.message:null,reason?'拒绝原因：'+(reasons[reason]||reason):null,
      v?.motion?.mode==='metric'?'运动目标正在执行':null,
      terminal?'最近动作终态：'+terminal.stop_reason+'；编码器进度 '+terminal.progress:null,
      receipt?.speech_policy==='wake_or_error_only'&&!receipt?.spoken_message?'正常指令静默处理':null,
      receipt?.error,job?.error,current?.connection?.error,v?.mic?.error,duration,
      timing?.cache_hit===true?'固定或重复音频缓存命中':null].filter(Boolean).join('\n');
  }
  async function api(path,body){
    const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),6000);
    try{
      const r=await fetch(path,{cache:'no-store',signal:abort.signal,...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':token},body:JSON.stringify(body)})});
      const d=await r.json();if(!r.ok)throw Error(d.error||'请求失败');return d;
    }finally{clearTimeout(timer);}
  }
  async function refresh(){
    const ticket=++refreshSequence;
    const results=await Promise.allSettled([api('/api/local-voice/status'),api('/api/board/status')]);
    if(ticket!==refreshSequence)return;
    lanAvailable=results[0].status==='fulfilled'&&Boolean(results[0].value.available);
    state=lanAvailable?results[0].value.state:null;
    boardState=results[1].status==='fulfilled'?results[1].value.state:null;
    render();
  }
  async function stopListening(){
    if(window.voiceStopPending)return;
    const version=++sequence;
    window.voiceStopPending=true;
    window.dispatchEvent(new CustomEvent('voice-stop-requested'));
    operationMessage='正在请求停止监听…';render();
    try{
      await refresh();
      if(version!==sequence)return;
      const useBluetooth=boardState?.connection?.state==='connected';
      const localPending=lanAvailable&&(state?.active_request||state?.voice?.local_active||state?.interaction?.stop_pending);
      let route=null,minimumEpoch=null;
      // Cancel a pending LAN startup in the PC bridge before stopping the board.
      // This endpoint queues the stop; it does not wait for a disconnected cable.
      if(localPending){
        try{await api('/api/local-voice/control',{action:'stop'});route='网线';}
        catch(e){if(!useBluetooth)throw e;}
      }
      if(useBluetooth){
        // The existing stop-only integration route bypasses chat/motion admission.
        // Its stop branch ignores context and never arms or resumes the vehicle.
        const reply=await api('/api/autonomy/command',{instance_id:null,epoch:null,control_epoch:null,text:'停止'});
        minimumEpoch=reply.result?.epoch??null;route='蓝牙';
      }else if(!route&&lanAvailable&&state?.voice?.observed&&state.voice.local_active){
        await api('/api/local-voice/control',{action:'stop'});route='网线';
      }else if(!route&&settled(observedState())){
        operationMessage='板端已确认监听停止，模型保持加载。';return;
      }
      if(!route)throw Error('尚无可用的语音控制通路，请连接蓝牙或恢复原网线连接后再停止。');
      operationMessage='停止请求已通过'+route+'送达，等待麦克风和播报结束…';render();
      const until=Date.now()+15000;
      while(Date.now()<until){
        if(version!==sequence)return;
        const d=await api(route==='蓝牙'?'/api/board/status':'/api/local-voice/status');
        const s=d.state;
        if(route==='蓝牙')boardState=s;else state=s;
        if(settled(s)&&(minimumEpoch===null||s.voice.epoch>=minimumEpoch)){
          operationMessage='板端已确认监听与播报停止 · 模型保持加载。';render();return;
        }
        await new Promise(resolve=>setTimeout(resolve,300));
      }
      throw Error('停止请求已送达，但尚未确认麦克风关闭，请查看板端状态。');
    }catch(e){operationMessage='未确认停止：'+e.message;}
    finally{window.voiceStopPending=false;pending=false;window.dispatchEvent(new CustomEvent('voice-stop-settled'));render();await refresh();}
  }
  async function control(action){
    if(window.voiceStopPending||window.voiceActionPending)return;
    const version=++sequence;
    window.dispatchEvent(new CustomEvent('voice-lan-start-requested'));
    pending=true;operationMessage='';render();
    try{
      if(action==='start'){
        const board=await (await fetch('/api/board/status',{cache:'no-store'})).json();
        if(board.state?.connection?.state==='connected'&&!board.state.voice?.stopped&&!state?.voice?.local_active){
          get('local-voice-state').textContent='停止蓝牙语音任务，准备切换本地语音…';
          const c=await (await fetch('/api/status',{cache:'no-store'})).json();
          const reply=await fetch('/api/board/control',{method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':token},body:JSON.stringify({instance_id:c.instance_id,epoch:c.epoch,action:'stop',board_context:{instance_id:board.state.instance_id,interaction_epoch:board.state.interaction.epoch}})});
          if(!reply.ok)throw Error('无法停止当前蓝牙语音，请先停止全部交互，再恢复聊天。');
          const until=Date.now()+30000;
          let settled=false;
          while(Date.now()<until){
            if(version!==sequence)throw Error('切换已取消');
            const b=await (await fetch('/api/board/status',{cache:'no-store'})).json();
            if(b.state.voice.stopped&&!b.state.voice.pending&&!b.state.voice.active&&!b.state.voice.mic?.running){settled=true;break;}
            await new Promise(resolve=>setTimeout(resolve,500));
          }
          if(!settled)throw Error('旧语音任务尚未结束，未开启新模式。');
        }
      }
      if(version!==sequence)throw Error('切换已取消');
      const r=await fetch('/api/local-voice/control',{method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':token},
        body:JSON.stringify({action,instance_id:state?.instance_id,interaction_epoch:state?.interaction?.epoch})});
      const d=await r.json();if(!r.ok)throw new Error(d.error||'请求失败');state=d;
    }catch(e){if(version===sequence)operationMessage=e.message;}
    finally{if(version===sequence)pending=false;await refresh();}
  }
  get('local-voice-start').addEventListener('click',()=>control('start'));
  get('local-voice-stop').addEventListener('click',stopListening);
  get('stop-all').addEventListener('click',()=>sequence++);
  window.addEventListener('voice-bluetooth-requested',()=>{sequence++;pending=false;operationMessage='';});
  refresh();setInterval(refresh,700);
})();
