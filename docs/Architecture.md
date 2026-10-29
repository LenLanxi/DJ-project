# 架构与维护入口

应用只有一条动作链：豆包 RTC 混合编排中的方舟模型生成完整 Python，在 Docker 内执行，经 Host 调用同一个设备 SDK。跟随独立持续运行，临时动作通过票据暂停并恢复原参考。模型接口见 [API](API.md)，验证入口见 [Testing](Testing.md)。

语音任务的默认循环是“模型看前摄像头→生成一个动作→执行→新画面→下一轮决策”。单动作仅由提示词约定，Host 不限制每段程序的动作数量。提示词不再使用内层 observe/describe；navigation 的旧观察与跟踪接口仅保留兼容手工脚本，不能把它们误认为当前语音路线的必经步骤。

## 职责

| 位置 | 负责什么 |
| --- | --- |
| app/Application.py、DeviceSession.py | plan/run/voice 组装、设备与会话关闭 |
| app/Runtime.py | 程序准入、撤权、ProgramRun、持续 RC 任务与清理 |
| app/Tools.py、Results.py | 模型提示、工具契约、结果摘要 |
| app/SessionLog.py | 脱敏运行日志与滚动保留 |
| providers/DoubaoConfig.py | 唯一豆包凭据加载与服务端配置 |
| providers/RTCBridge.py | 本机工具事件、程序调度与无人机帧推送 |
| providers/RTCRuntime.py | 隐藏 Node 服务的启动、结果回填与关闭 |
| providers/DoubaoVision.py | 官方 Ark SDK：文字计划、图片问答和结构化观察 |
| web/RTCService.js、RTCToken.js | 官方 OpenAPI 签名、RTC 房间授权和云任务生命周期 |
| web/RTCClient.js | 官方 RTC SDK、选定耳机设备、麦克风开关与外部无人机视频流 |
| app/ReplayCapture.py、web/ReplayRecorder.js、ReplayStore.js | 可选的独立复盘存档：源码、日志、音频、图传与时间线 |
| execution/Host.py | 程序动作授权、跟随交接、导航证据及 RPC 分发 |
| execution/sandbox_runner.py、sandbox/worker.py | 会话容器、每程序独立进程、RPC 与回收 |
| navigation | 观察解析、图像稳定与自然目标跟踪 |
| follow/r3_follow | 标记检测、固定控制算法、跟随服务和数值配置 |
| drone_sdk | 设备协议、遥测、最终发送、急停与 FrameResult |

## 调用与生命周期

浏览器使用官方 @volcengine/rtc SDK 发布耳机音频与无人机视频。Node 使用官方 OpenAPI 签名调用 StartVoiceChat，配置 volcano_duplex 原生语音与 ArkV3 视觉／工具模型；云端选择回答来源。工具程序由外挂方舟模型生成，不能称为语音模型直接生成代码。普通对话不需要画面或工具时使用混合编排要求的 `<NOVISION>` 标记；需要画面或工具时不得使用该标记。

工具到达即可处理，不以最终 ASR 字幕为执行前提；字幕用于显示与日志，普通插话和字幕变化不关闭 RTC 会话。云端自身编排时序不等于本地设置的 ASR 门禁。

1. RTCClient 转发云端工具消息给本机 RTCBridge，app/Tools 校验 run_program；按工具 ID 去重并检查当前执行授权。
2. UnifiedRuntime 创建本次 ProgramRun(host, outcome, task)，Runner 在会话容器中启动新 worker 进程。
3. worker 执行完整 Python，通过 RPC 调用 Host；模型进程没有设备、密钥、网络或宿主挂载。
4. Host 暂停已有跟随，取得 ControlGrant，调用 SDK；SDK 在实际发送前复查授权。
5. 程序成功后清理残留子进程和临时文件，保留容器供下一段程序复用；每段程序使用新 Python 作用域。控制权释放后，按票据恢复原跟随参考。结果按原工具 ID，由 Python 经 Node 的 UpdateVoiceChat(function) 回传云端；网页只显示结果，不承担云结果回填。

运动程序收尾后，RTCBridge 先请求一张更新帧；RTCClient 在解码时间晚于请求时间的图像绘制并提交视频轨后确认帧号。宿主再回填结果，最多等待 2 秒，失败只附带 camera_observation=unavailable，不篡改动作成功与否，也不关闭会话。该确认是本地发布证据，不能证明云端具体采用了哪张帧；需在真实 RTC 联调中核对模型的动作后观察。日志事件为 post_action_frame。

取消、超时或协议故障会销毁本次会话容器，下一段程序重新建立。应用退出调用 close_sandbox 回收常驻容器。创建／删除未确认仍保留 pending_container，先重试清理再接受新程序。容器复用减少启动开销，不复用 Host、取消事件或模型变量。

Runtime 统一准入和控制收尾；Runner 负责容器与 RPC。结果和回收证据保存在 SandboxOutcome，不挂到 CancelledError，也不使用全局结果槽。成功的临时动作不会因为普通自动跟随恢复失败而改判失败；恢复结果单独记录。

用户明确要求持续运动时，`start_velocity` 把任务交给 Runtime 单独持有的 Host，每 100 ms 续发 RC；正常程序结束与只读查询不会释放这个任务。新运动、跟随切换、停止、发起程序失败或会话结束会收尾，原有 RC 看门狗不变。持续任务不会在结束后恢复旧跟随，详情与状态字段见 [API](API.md)。

## 跟随与导航

算法固定为 adaptive 横移／转向、relative_size 前后跟随、responsive_v1 参数；默认配置开启上下跟随。RC 上限与参考大小等数值仍可调整，增益常量在 openpose_control.py。其他候选模式、相机／米制标定和离线穷举重复标记扫描已移除。

默认四轴上限为横向共享 50、前后 40、上下 20 RC。高度范围 50–200 cm，距边界 20 cm 限制继续外向升降，高度未知／过期时升降归零。四轴顺序固定为 lr/fb/ud/yaw，SDK 发送前取整。RC 和标记图像占比不代表物理速度或米制距离。

目标丢失、画面短时过期或帧间隔异常时保持启用意图并输出零；稳定的新鲜目标回来后恢复。显式停止、急停或关闭会作废恢复意图，不会自行重启。临时接管使用 pause_if_active / finish_action，保留原距离与构图；显式参考调整仍要求匹配动作、动作后新帧及整段程序成功。

常规识别已发现多个同号目标时拒绝跟随；不再穷举增强区域，因此可能漏掉低对比度的第二份同号标记。转身与遮挡相关常规识别仍保留。

自然目标导航保留：先观察图像稳定，用云语义种子经本地跟踪追到新鲜帧；语义窗口最多 6 秒，追赶最多 1 秒，运动前再次检查时效。bbox 使用归一化 xyxy。歧义、低纹理、跟踪失效和超窗不授权运动；解码时间不证明相机曝光时间。

## 运行边界

动作须传不可变 ControlGrant(owner, control_epoch, command_epoch)。同名 owner 不代表同次授权；停止／降落返回更新后的 grant。取消先同步撤销旧 generation，再异步停止；迟到线程不能借新授权执行。已发命令回复归属不明时，记录 command_channel_ambiguous 和 command_uncertainty 并继续允许后续动作；后来收到的 ok 不能证明回复属于新动作，模型需结合新画面和遥测判断，不能盲目重复前一动作。

控制清理未确认时保留待清理 Host，仅允许状态查询、停止和降落。日志／通知线程未排空不阻止设备清理，实际视觉和控制线程仍须退出。accepted 只表示接受，不代表动作已完成。

SDK 动作失败保留具体原因和 details，经 Host、沙箱结果与模型摘要传递。发送后响应超时属于完成情况未知，不能表述为“飞机没有动作”或自动重发；本地记录异常堆栈。

本地连接不依赖短时心跳超时退出；实际关闭连接仍停止并清理。媒体暂缺只暂停对应媒体；不自动更换设备或重放旧音频。操作员停止／降落先执行，网页通知不阻塞飞机操作。

运行日志位于 logs/Runtime，约 16 MB 分段，共享十进制 1 GB 保留预算。只回收本日志器已关闭分段；活动／历史格式文件不自动删除。容量每 5 秒、轮转或空间不足时核算，并发写入可短暂超额。写入失败记录 error/dropped_records，不中断任务。

## 修改入口

- 模型提示和公开工具：app/Tools.py；验证 tests/integration。
- 云配置与协议：providers；验证 tests/ai，真实云另行显式诊断。
- 音频和设备恢复：web；验证 npm --prefix web test，再做实际耳机检查。
- 控制交接和容器：app/Runtime.py、execution；验证 Check.ps1 -Docker。
- 跟随数值：follow/configs；算法：follow/r3_follow；验证 tests/follow，再现场核对方向。
- 自然目标导航：navigation；验证 VisionTests、TargetTrackerTests、NavigationTests。
