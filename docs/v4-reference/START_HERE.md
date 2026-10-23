# v0.4耳机适配更新（2026-10-23）

已按老师三项反馈更改：独立容器完整Python、多步视觉反馈、实时双向语音、取消输入y。

耳机检查先阅读HEADSET_TEST.md，运行 `py -3.11 test_audio_device.py --list`。
选择输入/输出索引，运行3秒录放并保存audio_config.json；自检不需要Docker或API。
完整程序阅读README.md。需要Python 3.11和Docker，真实语音还需要API账号及耳机。
没有Docker时程序会明确报unavailable，不会直接在电脑执行模型代码。

自动化结果见TEST_RESULTS.txt；3项Docker集成测试未运行。实时API、真实耳机和实机尚未验收。
新增近讲降噪配置不等于已实现AEC；现场须验证误打断和正常插话，不能承诺完美兼容所有耳机。
红色帐篷示例是闭环程序示例和离线语义夹具，不是已经跑通真实自主导航。
