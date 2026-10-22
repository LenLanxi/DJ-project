# 沙箱Python接口

这是4组给模型的代理协议，由宿主网关翻译为2组SDK；不是2组原生函数签名。
代理调用均同步等待结果，模型程序可以自行编排多步、循环和条件。

| 方法 | 返回/参数 |
|---|---|
| get_status() | battery,height_cm,connected,flight_state,control_owner,timestamp |
| get_frame() | image_url,frame_id,timestamp,source,simulated；无有效画面报错 |
| observe(target) | visible,center_x,width_fraction,description及帧元数据 |
| takeoff(),land(),stop_motion() | ok,status,reason（存在时） |
| move_forward/back/left/right/up/down(cm) | 20至500厘米整数，调用2组同名接口 |
| rotate(degrees) | 内部接受非零±360度整数；对接实机前须确认2组正负方向约定 |
| update_velocity(lr,fb,ud,yaw) | 四个-100至100整数百分比；accepted仅指收到请求 |
| start_follow(),stop_follow() | 通过2组统一入口启动/停止；恢复检查由3组提供 |
| is_following() | 布尔值 |

模型无需也不能任意指定owner。宿主统一使用model并完成控制权交接。
未公开的电脑文件读写、shell、任意模块属性等均不属于RPC能力。
完整Python运行在容器内部，可以导入容器已有标准库；不能通过容器网络调用外部服务。
视觉服务和SDK由宿主调用，API密钥不交给容器。

observe调用可在循环中反复使用。若视觉不可用、画面过期或没有新真实帧，抛出异常。
本地静态图片和语义夹具都有明确source，不作为实时导航证据。

单纯完成Python程序不代表任务物理目标已完成。journal保留每个接口的实际结果和错误。
容器隔离保护电脑执行端，不是物理避障或飞行控制算法。
