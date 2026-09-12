# 窗口移动鼠标锁定（WindowLockMaster）

Windows 多显示器窗口与鼠标管理工具，当前版本 **1.0.20**。

## 功能

- 枚举普通窗口、无边框窗口和最小化窗口
- 扁平化彩色操作台界面，采用深蓝导航、浅色内容区和多色动作分组
- 点击窗口列表表头可按标题、程序、显示器、状态或尺寸排序
- 移动后会验证实际所在显示器，失败自动重试，成功后不重复移动
- 窗口列表支持一键全选（自动排除黑名单）和清除勾选
- 对多个已勾选窗口批量移动到指定显示器
- 右键菜单支持批量锁定、解锁和移动
- 黑名单窗口禁止锁定和移动
- 将鼠标限制在完整显示器区域，包含任务栏
- 锁定窗口在恢复显示后自动回到绑定显示器
- 托盘运行与配置持久化
- 支持全局快捷键

## 快捷键

```text
Ctrl + Alt + Left / Right       移动当前窗口
Ctrl + Alt + L                  锁定当前窗口
Ctrl + Alt + U                  解锁当前窗口
Ctrl + Alt + M                  切换鼠标锁定/解锁
Ctrl + Alt + Shift + M          强制解除鼠标锁定
```

`Ctrl + Alt + L` 如果被其他软件占用，程序会在日志中记录注册失败；窗口列表中的锁定按钮仍可使用。

## 使用方式

启动 `WindowLockMaster.exe` 后，在窗口列表最左侧点击复选框，或点击“全选（排除黑名单）”一次性勾选所有可操作窗口；再使用工具栏或右键菜单批量移动。黑名单窗口不会被勾选，也不会被移动。

## 构建

需要 Python 3.12、PyInstaller、pystray 和 Pillow：

```bash
python -m PyInstaller --noconfirm --clean --onefile --windowed --name WindowLockMaster window_lock_master.py
```

## 数据位置

运行配置和日志保存在：

```text
%APPDATA%\\WindowLockMaster\\
```

本仓库不包含用户配置、日志、虚拟环境或本机生成的运行数据。

## License

MIT
