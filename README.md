# nova-resolution-unlocker

## 原理

将星塔旅人的分辨率子档设为未定义值 `3`，让游戏跳过预设降分辨率分支，并将 Unity 缓存宽高设为屏幕原生尺寸。不是隐藏的超高画质档；目前仅验证国服 `2.0.0`、画质配置版本 `9`。

## 要求

设备已 root，且 `adb shell su -c 'id -u'` 能返回 `0`。电脑需安装 Python 3.9+ 和 Android Platform Tools，设备已连接并授权 USB 或无线调试。先在游戏内将画质设为自定义。

## 使用

仅连接一台设备时运行 `python3 unlock.py`。多台设备时，用 `python3 unlock.py -s "设备标识"` 指定设备，或 `python3 unlock.py --all` 处理所有在线设备；设备标识通过 `adb devices` 查看。

恢复：`python3 unlock.py -s "设备标识" --restore "备份文件.json"`，仅允许恢复到原设备。

重新选择游戏画质可能覆盖解锁设置；原生分辨率会增加 GPU 负载。
