工程代码
语音交互、车辆遥控与四摄鸟瞰系统

一、目录和入口
application/launch.py：电脑网页、蓝牙网关、语音与遥控交互、鸟瞰结果显示。
application/board_entry.py：板端语音、CAN控制、四摄照片任务和模型调度入口。
application/fm_autonomy/：控制仲裁、会话管理、模型适配和常驻BEV处理。
application/vendor/：协议、驱动和中文语音前端。
application/camera_continuous/：四路libuvc连续取流，包括ARM64可执行文件、动态库、源码和许可。
fpga/uart/：UART RTL、约束、PS驱动和MMIO C源码。
bev_postprocess/：目标框解码与NMS源码。
scripts/：SDK隔离环境准备和原生部件构建脚本。
model_manifests/、模型依赖清单.json：五个模型的身份与恢复目录。

本工程包含当前应用及其UART、相机、BEV后处理源码。大型模型、平台SDK安装包、厂家Vivado基础工程和BOOT镜像需另备，具体文件身份和目录见运行环境恢复清单.json与模型依赖清单.json。

二、电脑安装与页面预览
进入application目录，使用Python 3.8或以上版本：
  python -m pip install -r requirements-pc.txt
  python launch.py --mock --port 18883 --gateway-port 18890 --bev-folder results/bev
打开http://127.0.0.1:18883/。该模式使用模拟传输，供查看页面和交互流程；按Ctrl+C退出。真实蓝牙启动命令见第六节。

三、板端环境与模型
板端使用Linux ARM64、Python 3.8和iCraft 3.39.0。Python扩展与动态库必须来自同一个隔离运行目录，使用runtime339_2g的分配器范围配置。
从工程代码根目录执行：
  python scripts/prepare_runtime.py --deb /PATH/Icraft_3.39.0_onchip.deb --wheel /PATH/icraft-3.39.0-cp38-none-manylinux2014_aarch64.whl --output application/runtime339_2g
安装包名称、大小和SHA见运行环境恢复清单.json。脚本只在新的目录中解包和准备库文件，不启动硬件，也不覆盖已有目录。
板端CPU依赖见application/requirements-board.txt；系统还需ALSA的arecord/aplay、libusb-1.0.so.0、Linux uvcvideo与SocketCAN。

将五个模型恢复到：
  application/assets/models/asr100_rebuilt/
  application/assets/models/vehicle_bert/
  application/assets/models/tts_decoder/
  application/assets/models/bev_extractor/
  application/assets/models/bev_decoder/
每个文件的来源定位、角色、大小、SHA和目标路径均列于模型依赖清单.json。将model_manifests中的语音三份BUNDLE.json原样复制到相应目录，并补齐其中列出的全部文件，包括CMVN、tokenizer、嵌入数组和TTS的CPU encoder；只有network.raw不能运行。
BEV实际加载extractor与decoder各自的network.json和network.raw；模型清单中的对应摘要为其运行身份。中文TTS前端已包含在application/vendor/voice_policy/。几何配置和LUT位于application/assets/geometry/。

四、构建原生部件
以下命令从工程代码根目录执行。在目标Linux或对应ARM64交叉编译环境中运行：
  sh scripts/build_native.sh
生成build/native/libfm_uart_mmio.so和build/native/postprocess。将前者放入application/config/private/baseline/，将后者的绝对路径和SHA填入resident_bev配置。

application/camera_continuous/已包含ring_stream、libuvc_probe.so及其release.json。需要重编译时运行：
  sh scripts/build_camera.sh
输出位于build/camera/。替换相机程序时同步更新release.json中的文件摘要。libuvc源码及BSD许可、libusb头文件许可均随工程提供；系统libusb动态库由板端环境提供。

UART模块接入厂家平台寄存器总线，使用100MHz时钟、9600baud、TX128/RX256及0x400F0000地址约定。uart_io.xdc针对当前集成层级；集成到不同层级时需调整端点约束。完整布局布线和BOOT生成依赖厂家基础工程及平台IP。

五、配置和板端启动
进入application目录。将config/*.example.json复制到config/private/，去掉文件名中的.example并填写真实路径与本次启动参数。各path/sha256对必须指向同一文件。修改resident_bev.json后更新voice_candidate.json中的resident_bev摘要，再更新joint_profile.json中的authorized_voice_validation摘要。
本工程使用all_icraft339_final语音配置、同一DeviceOwner中的resident BEV，以及photo.capture.strategy=fast。
photo.capture.expected_boot_id取自本次板端启动；photo和resident中的capture_root/output_root必须一致。
config/camera_mapping_frozen.json提供当前物理USB布局。配置绑定本次文件SHA，采集工作者同时核对槽位、方向标签及USB路由；重启后的boot/video节点可更新，更新后需同步摘要。改变相机安装或USB连接后重新确认画面方向。

板端baseline目录需准备candidate_identity.json、trial_boot.json、release_manifest.json和libfm_uart_mmio.so；结构样例位于config/baseline/。这些配置需对应本次启动和实际BOOT，不能将未填写的样例当作有效凭据。
link.key由操作人生成32字节随机值并保存为64位十六进制文本，PC与板端均放入application/config/private/baseline/link.key，内容相同。LAN语音令牌也由操作人生成，分别配置到PC和板端；云端聊天凭证由使用者本机设置或环境变量提供。
voice_candidate中的execution_evidence和模型验证文件为运行门控所需，需随实际板端环境配置。车辆与语音授权按本次boot和人工确认生成。PC的pc_motion_authorization.json使用同一次有效ground-user授权副本。

将runtime_root填写为隔离目录的绝对路径，并设置环境：
  export FM_RUNTIME=/ABSOLUTE/工程代码/application/runtime339_2g
  export PYTHONPATH="$FM_RUNTIME/python"
  export LD_LIBRARY_PATH="$FM_RUNTIME/usr/lib/aarch64-linux-gnu"
  export FM_PL_DRIVER="$LD_LIBRARY_PATH/libaxizg330aiu.so"
  export LD_PRELOAD="$FM_PL_DRIVER"
  export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1

先执行入口检查；相同参数检查通过后，将--check改为--start启动：
  python3 board_entry.py --mode ground-user --baseline config/private/baseline --authorization config/private/ground_user_authorization.json --metric-calibration config/private/metric.json --voice-candidate-config config/private/voice_candidate.json --accelerator-profile config/private/joint_profile.json --photo-config config/private/photo.json --voice-http-config config/private/board_lan.json --until-stopped --check
服务ready等待建议180秒，外层SSH调用等待240秒，模型加载时间另计。入口会核对BOOT、UART、当前授权和硬件状态；缺少相应配置时先补齐再启动。

六、电脑连接与使用
在application目录执行：
  python launch.py --bluetooth --port 8883 --gateway-port 8890 --settings-dir config/private/pc_settings --local-voice-config config/private/pc_lan.json --bev-folder results/bev --photo-control --motion-authorization config/private/pc_motion_authorization.json
打开http://127.0.0.1:8883/，在蓝牙页连接并加载模型。语音页单独开始或停止监听，唤醒词与运动指令分开说。遥控按住方向移动，松手停车；动作范围从motion.capabilities读取，示例上限为0.2m、90°和单动作5秒。
生成鸟瞰图前保持车辆和场景静止，再点击采集与生成。ASR、BERT、TTS decoder及两个BEV模型共同驻留，图调用串行；TTS encoder、LUT投影和目标框后处理在CPU执行。
四路连续取流不等于同步曝光。当前鸟瞰输出表达模型检测到的交通目标，零框或框外空白不表示可通行区域。检测结果用于展示，未接入自主避障；几何距离精度与最新重启映射补丁后的实机新采集仍需现场确认。

第三方来源与使用许可见许可证说明.txt。
