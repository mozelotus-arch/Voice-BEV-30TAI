"use strict";
(() => {
  const image=document.getElementById('bev-image');
  function sourceLabel(source){
    if(typeof source==='string')return source;
    if(!source || typeof source!=='object')return '来源信息不完整';
    return ['slot','device','captured_at'].map(key=>source[key])
      .filter(value=>typeof value==='string' || typeof value==='number').join(' / ') || '来源信息见运行记录';
  }
  let sequence=0,pollTimer=null,pollRunning=false;
  function schedulePoll(delay=0){
    clearTimeout(pollTimer);
    pollTimer=setTimeout(poll,delay);
  }
  async function poll(){
    if(pollRunning)return;
    pollRunning=true;
    try{await refresh();}
    finally{
      pollRunning=false;
      const active=posting||photoState?.busy||measurement&&!measurement.failed&&!measurement.reported;
      schedulePoll(!document.hidden&&active?200:2000);
    }
  }
  let photoState=null,posting=false,sourceKey='',stateFresh=false,measurement=null,measuring=false;
  const token=document.querySelector('meta[name="control-token"]').content;
  const $=id=>document.getElementById(id);
  function buttons(){
    const blocked=!stateFresh||posting||photoState?.busy||!$('photo-stationary').checked;
    $('photo-one-shot').disabled=blocked;
    $('photo-warmup').disabled=blocked||photoState?.camera?.ready||photoState?.camera?.persistent===false;
    $('photo-capture').disabled=blocked;
    $('photo-infer').disabled=blocked||!photoState?.capture_ready;
  }
  async function action(action,oneShot=false){
    if(!stateFresh||posting||photoState?.busy||!$('photo-stationary').checked)return;
    const id=crypto.randomUUID();
    if(action==='capture')measurement={capture_id:id,start:performance.now(),one_shot:oneShot,marks:{trigger_ms:0},hidden_during_wait:document.hidden};
    if(action==='infer'&&measurement){
      measurement={...measurement,failed:false,reported:false,infer_id:id,marks:{trigger_ms:0,
        capture_received_ms:measurement.marks.capture_received_ms,infer_trigger_ms:performance.now()-measurement.start}};
    }
    posting=true;buttons();
    try{
      const context=await (await fetch('/api/status',{cache:'no-store'})).json();
      const response=await fetch('/api/bev/control',{method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':token},
        body:JSON.stringify({instance_id:context.instance_id,epoch:context.epoch,action,id,stationary_confirmed:$('photo-stationary').checked})});
      const value=await response.json();if(!response.ok)throw Error(value.error||'任务未受理');
      photoState=value;await refresh();
    }catch(e){if(measurement)measurement.failed=true;$('photo-status').textContent='未完成：'+e.message;}
    finally{posting=false;buttons();schedulePoll();}
  }
  $('photo-stationary').addEventListener('change',buttons);
  $('photo-capture').addEventListener('click',()=>action('capture'));
  $('photo-one-shot').addEventListener('click',()=>action('capture',true));
  $('photo-warmup').addEventListener('click',()=>action('warmup'));
  $('photo-infer').addEventListener('click',()=>action('infer'));
  async function refresh(){
    const ticket=++sequence;
    try{
      const response=await fetch('/api/bev/status',{cache:'no-store'});
      if(!response.ok)throw new Error('返回服务暂不可用');
      const value=await response.json();if(ticket!==sequence)return;
      photoState=value.task||null;stateFresh=true;
      $('photo-controls').hidden=!photoState?.available;
      if(photoState){
        const camera=photoState.camera;
        $('photo-camera').textContent=camera?.strategy==='fast'?'四路并行持续取流：仅启动时预热，5 分钟无采集后释放摄像头。每次选取停车后的 24 张新图片。':
          camera?.ready?'摄像头持续取流已预热'+(typeof camera.warmup_seconds==='number'?` · 预热 ${camera.warmup_seconds.toFixed(2)} 秒（不计入点击后的采集耗时）`:''):
          camera?.persistent?'录制前先加载模型并预热摄像头；停车后再采集全新的画面。':'先加载模型，待确认板端采集方式；当前未确认摄像头就绪。';
        const job=photoState.job,capture=photoState.capture;
        if(measurement&&!measurement.failed){
          if(job?.state==='failed'||job?.state==='cancelled')measurement.failed=true;
          if(job?.id===measurement.capture_id&&job.state==='completed'&&capture){
            measurement.frame_id=capture.frame_id;
            measurement.marks.capture_received_ms??=performance.now()-measurement.start;
            if(measurement.one_shot&&!measurement.infer_id&&!posting){action('infer');return;}
          }
          if(job?.id===measurement.infer_id&&job.state==='completed'&&job.result?.frame_id===measurement.frame_id){
            measurement.marks.result_received_ms??=performance.now()-measurement.start;
          }
        }
        const count=Number.isInteger(job?.completed_images)?` · ${job.completed_images}/${job.total_images} 张`:'';
        const stages={allocate_decoder_input:'准备输入缓冲区',decoder_forward_begin:'鸟瞰解码计算',
          decoder_forward_complete:'鸟瞰解码完成',sequential_input_spool:'保存计算输入（回退路径）'};
        const stage=job?.model_stage?(stages[job.model_stage]||job.model_stage):'';
        $('photo-status').textContent=job?(job.error?'未完成：'+job.error:job.phase||job.state)+count+(stage?' · '+stage:''):'等待采集本次照片';
        if(capture){
          $('photo-source').textContent=(photoState.capture_ready?'本次照片':'上次成功采集的照片')+' · 采集用时 '+capture.capture_seconds.toFixed(2)+' 秒 · '+
            (capture.capture_method?.startsWith('persistent_')?'持续流中选取停车后的新窗口，非同步曝光。':'四路顺序采集，非同步曝光。');
          $('photo-source').title='原图帧：'+capture.frame_id;
          const slotDirections=['front','right','left','back'];
          const directionLabels={front:'前方',back:'后方',left:'左侧',right:'右侧'};
          // Bind previews to the exact JPEG used by this result, not its capture filename or device number.
          const sources=value.frame_id===capture.frame_id?value.input_sources||[]:[];
          const previews=capture.images.map(item=>{
            const source=sources.find(source=>source.sha256===item.sha256&&source.slot===item.slot);
            return {...item,direction:source?.physical_camera||item.physical_camera||slotDirections[item.slot]};
          });
          const displayOrder=['front','back','left','right'];
          previews.sort((a,b)=>displayOrder.indexOf(a.direction)-displayOrder.indexOf(b.direction));
          const key=JSON.stringify([capture.frame_id,photoState.capture_ready,previews.map(item=>[item.image_url,item.sha256,item.direction])]);
          if(sourceKey!==key){
            sourceKey=key;
            $('photo-images').dataset.frameId=capture.frame_id;
            $('photo-images').replaceChildren(...previews.map(item=>{
              const figure=document.createElement('figure'),img=document.createElement('img'),caption=document.createElement('figcaption');
              figure.dataset.direction=item.direction;figure.dataset.sha256=item.sha256;
              img.src=item.image_url;img.alt=(directionLabels[item.direction]||'方向未确认')+'原始照片';
              caption.textContent=(directionLabels[item.direction]||'方向未确认')+' · '+(photoState.capture_ready?'本次采集':'上次采集');
              caption.title=img.alt+' · '+item.device+' · '+item.captured_at+'（板端时间未校正）';
              figure.append(img,caption);return figure;
            }));
          }
        }
      }
      buttons();
      const activity=value.activity, activityText=document.getElementById('bev-activity');
      activityText.hidden=!activity;
      activityText.textContent=activity ? '最近任务状态：'+activity.message+
        (activity.updated_at?' · 更新于 '+activity.updated_at:'')+
        (activity.voice_paused?'\n当前语音与遥控服务已暂停，资源用于视觉验证。':'') : '';
      document.getElementById('bev-message').textContent=value.message;
      const stale=photoState?.capture?.frame_id!==undefined&&photoState.capture.frame_id!==value.frame_id||
        photoState?.busy||['failed','cancelled'].includes(photoState?.job?.state);
      document.getElementById('bev-badge').textContent=value.available?
        (stale||activity && activity.state!=='completed'?'上次结果':'板端结果'):'等待板端';
      image.hidden=!value.available;
      if(value.available){
        if(image.getAttribute('src')!==value.image_url)image.src=value.image_url;
        const mode=value.capture_mode==='replay'?'历史实拍照片回放 · 本轮重新计算':'已保存的板端结果';
        document.getElementById('bev-source').textContent=mode+'（非实时避障）\n帧 '+value.frame_id+' · 板端记录时间 '+value.generated_at+'（未校正板端时钟）'+
          '\n本轮运行标识：'+value.board_boot_id+(value.source_capture_boot_id?' · 原采集标识：'+value.source_capture_boot_id:'')+
          '\n来源：'+value.input_sources.map(sourceLabel).join('、');
        present(value);
      }else{image.removeAttribute('src');document.getElementById('bev-source').textContent='';}
    }catch(e){if(ticket===sequence){stateFresh=false;buttons();document.getElementById('bev-message').textContent=String(e.message);
      document.getElementById('bev-badge').textContent='状态不可用';document.getElementById('bev-activity').hidden=true;}}
  }
  async function present(value){
    const m=measurement,job=photoState?.job;
    if(measuring||!m||m.failed||m.reported||job?.id!==m.infer_id||job.state!=='completed'||
       value.frame_id!==m.frame_id||job.result?.frame_id!==m.frame_id||value.sha256!==job.result?.image?.sha256)return;
    measuring=true;
    const inferId=m.infer_id;
    function current(){const j=photoState?.job;return measurement===m&&m.infer_id===inferId&&j?.id===inferId&&
      j.state==='completed'&&j.result?.frame_id===value.frame_id&&j.result?.image?.sha256===value.sha256;}
    function visible(){const r=image.getBoundingClientRect(),style=getComputedStyle(image);
      return !document.hidden&&document.body.dataset.page==='vision'&&!image.hidden&&style.display!=='none'&&style.visibility!=='hidden'&&
        r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth;}
    try{
      await image.decode();
      if(!current()||image.getAttribute('src')!==value.image_url||!image.complete||!image.naturalWidth)return;
      m.marks.decoded_ms??=performance.now()-m.start;
      if(m.one_shot&&!m.scrolled&&!document.hidden&&document.body.dataset.page==='vision'){
        m.scrolled=true;image.scrollIntoView({behavior:'auto',block:'center'});
      }
      if(!visible()){m.hidden_during_wait=true;return;}
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      if(!current()||!visible()||!stateFresh)return;
      m.marks.presentation_ready_ms??=performance.now()-m.start;
      const report={capture_id:m.capture_id,infer_id:m.infer_id,frame_id:m.frame_id,sha256:value.sha256,
        marks:m.marks,hidden_during_wait:m.hidden_during_wait,visible_at_completion:true,
        clock:'browser.performance.now',boundary:'trigger_to_decoded_visible_two_animation_frames',one_shot:m.one_shot};
      $('photo-timing').textContent=`本次点击到图像呈现就绪 ${(m.marks.presentation_ready_ms/1000).toFixed(2)} 秒（含轮询${m.one_shot?'':'及两次点击间等待'}）；已解码并经历两次渲染机会，未测量屏幕实际扫描输出。`;
      const response=await fetch('/api/bev/timing',{method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':token},body:JSON.stringify({report})});
      if(!response.ok)throw Error('计时记录未保存');
      m.reported=true;
    }catch(e){$('photo-timing').textContent+=' · '+e.message;}
    finally{measuring=false;}
  }
  document.addEventListener('visibilitychange',()=>{if(measurement&&document.hidden)measurement.hidden_during_wait=true;schedulePoll();});
  image.addEventListener('error',()=>{image.hidden=true;document.getElementById('bev-message').textContent='板端返回图像未能显示，请刷新结果。';});
  document.getElementById('bev-refresh').addEventListener('click',()=>schedulePoll());
  schedulePoll();
})();
