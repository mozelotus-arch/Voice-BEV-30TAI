"""Display only board-returned PNG artifacts; never constructs a BEV on the PC."""
import hashlib,json,re
from pathlib import Path

class BoardBevResult:
    def __init__(self,folder=None):self.folder=Path(folder).resolve() if folder else None
    def read(self):
        if self.folder is None:raise ValueError('尚未接入板端鸟瞰图返回目录')
        p=self.folder/'latest.json'
        if not p.is_file():raise ValueError('等待板端生成并返回鸟瞰图')
        if p.stat().st_size>65536:raise ValueError('返回记录过大')
        d=json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(d,dict):raise ValueError('返回记录必须是对象')
        if d.get('schema')!='FM-BOARD-BEV/1' or d.get('generated_on')!='board':raise ValueError('缺少板端生成记录')
        name=d.get('image','')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+\.png',name):raise ValueError('返回图片名称无效')
        image=self.folder/name
        if image.is_symlink() or image.resolve().parent!=self.folder or not 8<=image.stat().st_size<=16*1024*1024:raise ValueError('返回图片路径或大小无效')
        raw=image.read_bytes();digest=hashlib.sha256(raw).hexdigest()
        if not raw.startswith(b'\x89PNG\r\n\x1a\n') or digest!=d.get('sha256'):raise ValueError('返回图片校验失败')
        if not isinstance(d.get('frame_id'),str) or not d['frame_id'] or not isinstance(d.get('input_sources'),list) or not d['input_sources']:raise ValueError('缺少相机来源或帧号')
        if not isinstance(d.get('board_boot_id'),str) or not d['board_boot_id'] or not isinstance(d.get('generated_at'),str):raise ValueError('缺少板端运行来源')
        capture_mode=d.get('capture_mode','unspecified')
        source_boot=d.get('source_capture_boot_id')
        if capture_mode not in ('unspecified','current_boot_capture','replay'):
            raise ValueError('采集来源类型无效')
        if capture_mode=='replay' and (not isinstance(source_boot,str) or not source_boot):
            raise ValueError('历史回放缺少原采集启动标识')
        message=d.get('message','板端返回的鸟瞰图')
        display=self.folder/'display_details.json'
        if display.is_file() and not display.is_symlink() and display.stat().st_size<=4096:
            details=json.loads(display.read_text(encoding='utf-8'))
            if details.get('image_sha256')==digest and details.get('detection_count')==0:
                message+=' 本次输出0个检测框；不代表环境没有障碍物。'
        return dict(available=True,frame_id=d['frame_id'],sha256=digest,input_sources=d['input_sources'],
                    generated_at=d['generated_at'],board_boot_id=d['board_boot_id'],
                    capture_mode=capture_mode,source_capture_boot_id=source_boot,
                    message=message,generated_on='board',
                    image_url='/api/bev/image?sha256='+digest),raw
    def activity(self):
        if self.folder is None or not (self.folder/'activity.json').exists():return None
        try:
            p=self.folder/'activity.json'
            if p.is_symlink() or p.stat().st_size>16384:raise ValueError('任务状态文件无效')
            d=json.loads(p.read_text(encoding='utf-8'))
            if (not isinstance(d,dict) or d.get('schema')!='FM-BOARD-ACTIVITY/1'
                    or d.get('state') not in ('preparing','processing','failed','completed')
                    or type(d.get('voice_paused')) is not bool
                    or any(not isinstance(d.get(k),str) or not d[k] or len(d[k])>1000
                           for k in ('run_id','updated_at','message'))):
                raise ValueError('任务状态记录不完整')
            return {k:d[k] for k in ('state','run_id','updated_at','message','voice_paused')}
        except (OSError,ValueError,TypeError,KeyError) as exc:
            return dict(state='unknown',message='无法读取最近任务状态：'+str(exc)[:100])

    def status(self):
        try:value=self.read()[0]
        except (OSError,ValueError,TypeError,KeyError) as exc:value=dict(available=False,message=str(exc)[:160])
        activity=self.activity()
        if activity is not None:value['activity']=activity
        return value
