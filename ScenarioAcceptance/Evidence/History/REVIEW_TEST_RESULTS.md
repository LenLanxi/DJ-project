# 修复候选验收记录 · 2026-10-24

基础提交：9aaec59；Python 3.11；离线假硬件/假模型环境。

| 测试组 | 结果 |
| --- | --- |
| RootIntegration | exit=0 |
| AIModule | exit=0 |
| FollowModule | exit=0 |
| GroundSelfCheck | exit=0 |
| ScenarioAcceptance | exit=0 |
| DocsBaseline | exit=0 |
| DocsAcceptance | exit=0 |
| R3Review | exit=0 |

T01–T43：43通过，0失败，0错误。R3补充：7通过。
原有DocsBaseline：54项，51通过，3项真实Docker测试跳过。
其余：RootIntegration 16项、AIModule 28项、FollowModule 323项通过；地面自检通过。

本次未改老师的验收断言；Cases.json把已复测通过的known_gap改为pass，保留baseline_expected。
两处旧距离断言按50厘米需求更新；兼容Runner默认继续拒绝多句，显式启用的完整程序只送Docker。

**accepted不等于执行成功**：无Docker时完整程序状态为unavailable、ok=false，宿主机没有执行模型代码。

未完成的外部验收与需求：真实容器隔离/清理、真实国内服务协议和时延、耳机/电机噪声打断、独立距离/障碍证据、完整找回用户、实际通信故障注入及受控真机任务。
严格用例通过不代表完整自主导航已经实现；詳见REVIEW_FIXES.md。
