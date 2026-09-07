# PM2 Animation Lab

独立的 Princess Maker 2 课程与打工动画研究工具。依据固定版本源码和明确条件重放活动，合成原生图像，并与实录逐帧比较画面、顺序及时间。

[English](README.en.md) · [完整工作流程](docs/workflow.md) · [验证范围](docs/validation.md)

## 能做什么

- 支持10门课程、15项打工的源码档案；一次初始化、完整日程、随机输入和跨日状态连续执行。
- 解码外部LBX/PT1，保留mask、图层顺序、作者坐标和前景遮挡。
- 发布合成、实录、并排差异GIF/MP4，核验原生像素、帧顺序、累计时间及输出文件哈希。
- 播放前重新核验本次请求、完整源码状态、图像和回执；旧片不能只凭文件名被当成新结果。
- 对固定观察搜索兼容的源码条件；研究输出与动画发布分开。

这是命令行研究工具。原作源码、游戏文件、素材、录像、存档、FFmpeg和模拟器不随项目提供，也不会自动下载。请使用自己的授权本地输入。详见[来源及许可](NOTICE.md)。

## 安装

需要Python 3.11或更新版本。重建需要Git；生成/解码视频还需要单独安装FFmpeg。

```sh
git clone https://github.com/lishuoipad/pm2-animation-lab.git
cd pm2-animation-lab
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install .
pm2-animation-lab doctor
```

macOS/Linux：

```sh
. .venv/bin/activate
python -m pip install .
pm2-animation-lab doctor
```

也可用`python -m pm2_animation_lab`代替`pm2-animation-lab`。基础检查不需要原作文件。可选回放压缩解析依赖通过`python -m pip install '.[replay]'`安装。

## 数据放在哪里

数据目录必须显式指定，并位于工具仓库之外。无需使用作者机器的盘符或目录名。例如Windows可使用`D:/pm2-data`；macOS/Linux可使用`~/pm2-data`。

```text
pm2-data/                 # 外部目录，不提交到这个仓库
  inputs/                # 自备游戏归档
  requests/              # 明确日程、条件、输入绑定
  observations/          # 独立冻结的录像区间和帧索引
  recordings/            # 自备原生录像
  deliveries/            # 统一入口的新输出
  palette.json           # 明确且已核验的16色调色板
```

自备源码检出可以放在另一个外部目录。当前只接受提交`ec7bdef58357185fe5344973c156b857a5de2c1f`，并逐项检查源码文件哈希；缺少这个版本会明确拒绝，不推测其他版本的语义。

## 基本命令

查看场景、为输入建立哈希绑定：

```sh
pm2-animation-lab scenes
pm2-animation-lab bind /absolute/path/to/an/input.json
```

发布和播放核验始终使用同一个`pipeline`入口：

```sh
pm2-animation-lab pipeline --request /absolute/pm2-data/requests/activity.json --output /absolute/pm2-data/deliveries/activity-v1 --quarantine-root /absolute/pm2-data --source-root /absolute/pm2-source
pm2-animation-lab pipeline --request /absolute/pm2-data/requests/activity.json --playback-directory /absolute/pm2-data/deliveries/activity-v1 --quarantine-root /absolute/pm2-data --source-root /absolute/pm2-source
```

把示例绝对路径替换为自己的路径；Windows参数可使用`D:/...`。发布拒绝覆盖现有目录。播放复核成功后，只打开返回JSON的`media`路径。

首次准备需要独立录像索引、冻结区间和条件，不能仅给一个场景名就承诺生成已核验动画。[工作流程](docs/workflow.md)解释每份输入及其先后关系。`examples/`提供不包含原作数据的结构模板。

## 验证结论

提取前的25项固定录像实验覆盖15,122原生帧、1,242源码时间步，原生像素全部一致；其中544帧通过相邻源码姿势的显存复制模型重建。此结论限定于已验证区间及其兼容条件，不能扩展为所有年龄、等级、随机事件或硬件时序的无条件保证。

时间和复制进度受实录约束，不是独立硬件预测。GIF/MP4观看副本的累计边界舍入不超过5ms；MP4是有损观看副本，像素验收使用原生RGB/PNG和GIF回读。更多说明见[验证范围](docs/validation.md)。

## 开发与测试

```sh
python -m pip install -e .
python -m unittest discover -s tests -p 'test_*.py'
python tools/check_public_tree.py
```

无原作数据时，可选固定源码测试明确跳过。设置`PM2_SOURCE_ROOT`为授权本地源码目录后可启用；完整原生媒体验证仍需要私有的请求和录像。CI不下载原作文件。修改后须保持唯一发布入口、所有哈希和像素门槛，新增场景或语义必须附独立证据。

MIT许可证仅覆盖此库的自编工具、文档及原创测试。
