# AI随行小伙伴 · 4组 v0.4 耳机适配更新（2026-10-23）

本版本按老师反馈重构。**v0.3仍保留为旧版；本包为新的执行和实时语音架构。**

本次补充：显式耳机设备配置与身份校验、3秒录放自检、近讲麦克风云端降噪、可调VAD、
双声道输出与音频流故障处理。**现场先读 [HEADSET_TEST.md](HEADSET_TEST.md)。**
不再自动使用系统默认音频设备；首次运行语音前需完成耳机选择。

## 三项改变

1. 完整Python程序在独立Docker容器中执行。取消AST单调用解析，不再限制每轮只能一个动作。可使用循环、条件、变量、标准库及多次视觉/设备接口调用。
2. 实时语音直接使用Realtime API。麦克风持续上传，返回音频增量持续播放；检测到用户说话后清空待播放音频、截断未听部分，并取消旧程序。
3. 使用耳机完成提问、回复和语义澄清。不再录固定5秒，不再转写后输入y，不假设Tello有显示屏。终端仅保留开发日志和结束进程操作。

## 实现与验证状态

- 结构化视觉结果可被程序读取，程序可“观察→判断→移动→再次观察”。
- 提供红色帐篷闭环示例；内置的语义夹具仅用于验证分支和调用顺序，不是识别结果。
- 可显式接当前2组模拟SDK，不导入DJITelloPy，不另建飞机连接。
- 实时语音采用PCM16/24kHz双向流，服务端VAD检测发言和打断，支持模型函数调用。
- 宿主机仅暴露设备/视觉RPC能力；模型不能借RPC读取电脑文件、执行电脑命令或获取密钥。
- 容器禁网、根文件系统只读、无宿主目录挂载、无Docker socket、无API密钥、非root，限制内存/CPU/进程和任务时限。
- 没有Docker或镜像时返回unavailable，绝不退回电脑直接exec。

**最新自动化结果见 TEST_RESULTS.txt；3项真实容器测试因环境无Docker而跳过。**
未验证：容器镜像实跑/隔离效果、真实实时API、耳机音频、实际延迟、费用、真实摄像头、真实飞行及自主导航。
本包不是“红色帐篷自主导航已经完成”的证明。接口闭环与导航算法的实机效果是不同验收内容。

## 最小运行环境

按项目统一要求使用Python 3.11；也兼容3.12。本次开发测试为3.12。
Windows/macOS需要可运行Linux容器的Docker Desktop；Linux需要Docker Engine。

```bash
python -m pip install -r requirements.txt
docker build -t companion-sandbox:0.4 sandbox
python main.py --check
python main.py --program examples/red_tent.py --fixture-vision
```

最后一条执行的是离线红色帐篷语义夹具。它会在容器中运行完整循环程序，实际设备是Mock。
首次构建可能需要下载Python基础镜像；正式任务执行使用--pull=never，不临时拉镜像。
Docker基础镜像当前以python:3.11-slim为构建入口；验收时记录构建后的镜像ID，可按部署环境进一步固定基础镜像digest。

## 实时耳机对话

先选择并自检耳机（不需要Docker或API）。下面4、8必须替换为本机实际索引：

```powershell
py -3.11 test_audio_device.py --list
py -3.11 test_audio_device.py --input-device 4 --output-device 8 --save-config audio_config.json
```

配置自己的可用OpenAI API账号和实时模型标识。不要把密钥发群或放进代码。
聊天订阅不能自动代替API账号配置；本包不会读取ChatGPT登录凭据。

PowerShell配置形式：

```powershell
$env:OPENAI_API_KEY="自己的API密钥"
$env:REALTIME_MODEL="账号支持的Realtime模型标识"
$env:REALTIME_VOICE="marin"
python main.py --voice --fixture-vision --audio-config audio_config.json
```

Linux/macOS使用export设置相同环境变量。
也可用 `python main.py --list-audio-devices` 查看编号，通过命令行显式指定：

```bash
python main.py --voice --input-device 1 --output-device 2
```

启动后直接说话，不需逐轮按键。要求停止或降落时，模型调用独立interrupt通道。
用户任何插话都会取消当前程序并请求停止，不自动续飞；重新理解用户下一句需求。
实时语音显式请求近讲麦克风降噪，VAD默认0.7，可在audio_config.json调整。
服务端VAD检测、音频回声、耳机驱动缓冲、网络延迟需要实际测量；此实现没有声学回声消除算法，使用耳机减少扬声器回录。
关闭程序后请求停止并释放控制权；这不等价于完成实机降落。真实设备需保留项目已有独立人工接管渠道。

## 实际图片/摄像头视觉

内置Mock没有摄像头，不会伪造实时画面。
需要结构化视觉观察时，设置独立视觉模型参数：

- AI_CHAT_URL：完整HTTPS Chat Completions地址。
- AI_API_KEY：视觉服务密钥。
- AI_MODEL：支持图片的模型标识。

本地测试图片：

```bash
python main.py --voice --image test.jpg
```

本地图片明确标记static_test_image；不会因为重新调用就变成新画面。
若显式选择 `--fixture-vision` 则使用语义夹具，不能同时把其当作真实图片识别。

2组当前模拟SDK：

```bash
python -m pip install -r requirements-vision.txt
python main.py --program examples/red_tent.py --sdk-stub-root /path/to/DJ-project
```

路径需含drone_sdk/controller.py。当前仓库SDK get_frame没有图像，因此红色帐篷示例会明确失败。
可用一个只查状态/模拟起降的程序先核对dataclass返回值；不要把模拟completed当成实机完成。
当前适配入口名为sdk-stub，所有结果仍标记simulated=True；不提供宣称已经验证的实机启动开关。
接真实SDK前需确认初始化、线程安全、控制权交接、阻塞动作中断、rotate正负方向及跟随恢复检查。

真实SDK画面要求：frame为OpenCV BGR、timestamp是采集Unix时间、frame_id保持同帧一致。
画面为空、stale为真或采集时间超过0.5秒时拒绝观察；同一真实帧不作为新观察反复执行运动。
视觉返回visible、center_x、width_fraction、description等，可直接用于Python循环/条件。
center_x与width_fraction是图像相对坐标，并非米/厘米距离；看见目标不等价于知道安全路径或已经到达。

## 沙箱内可调用接口

```python
state = drone.get_status()
frame = drone.get_frame()  # 包含image_url及采集元数据；不含宿主机路径
observation = drone.observe('红色帐篷')

if observation['visible']:
    drone.move_forward(20)
    observation = drone.observe('红色帐篷')
```

完整方法见API.md。模型无需传owner，宿主网关处理暂停跟随/申请/释放控制权。
取消的是“Python程序形态限制”，保留的是宿主暴露能力和SDK参数范围；容器不能任意调用电脑函数。
程序可能执行多步，调用日志逐步返回实际接口结果，最终回复必须检查日志，不仅看程序退出状态。
程序最多90秒、2000次RPC，可通过--timeout调整为1至600秒；这些是计算资源预算，不是单步动作限制。
stop_motion、land或程序失败后不自动恢复跟随。临时移动完成后，恢复前检查由3组实现的跟随入口负责。

## 测试和验收

```bash
python -m unittest discover -s tests -v
```

要在已构建镜像的电脑上验证真实容器（Linux/macOS）：

```bash
RUN_DOCKER_TESTS=1 python -m unittest discover -s tests -v
```

PowerShell：

```powershell
$env:RUN_DOCKER_TESTS="1"
python -m unittest discover -s tests -v
```

3项容器集成测试覆盖：完整闭环Python程序；容器内无法取得API环境变量/Docker socket且无法联网；无限循环超时。
其余测试使用模拟设备、模拟通信管道和Realtime事件回放；没有在宿主机执行模型生成程序。
验收时还应测试实际宿主文件不可见、写权限、资源限制和停止时容器清理，不能只凭启动参数认定隔离已验证。

可选 `--log logs/session.jsonl` 记录程序、工具结果、语音事件耗时和供应商usage；默认不存日志，不存原始音频。
当前延迟指标为“服务端检测用户说完→首个音频包到达客户端”，不是人耳听见的端到端延迟。
还应测“用户实际结束说话→首个可听声音”、打断到静音、取消到设备停止，记录中位数和P95。
没有真实测试前不填写毫秒数或宣称达到ChatGPT体验；价格未配置时不虚构费用。

## 参考资料

2026-10-21核对的官方文档：
https://developers.openai.com/api/docs/guides/realtime-conversations
https://developers.openai.com/api/docs/guides/voice-websockets?voice-api=realtime
https://docs.docker.com/engine/containers/run/

## 文件

- main.py：实时语音或容器程序入口，无逐轮键盘确认。
- realtime.py / duplex_audio.py：实时模型事件、双向音频、打断、工具调用。
- audio_config.py / audio_devices.py：设备锁定、配置校验、WASAPI格式转换。
- test_audio_device.py / HEADSET_TEST.md：独立3秒录放自检及现场验收流程。
- sandbox_runner.py / sandbox/：容器任务监督、RPC通道、Python执行。
- gateway.py / backends.py：控制权、SDK与模拟设备适配。
- vision.py：结构化视觉观察和帧检查。
- examples/red_tent.py：可循环观察/判断/执行的示例。
- tests/：离线与需Docker的集成测试；TEST_RESULTS.txt为本次真实测试输出。
