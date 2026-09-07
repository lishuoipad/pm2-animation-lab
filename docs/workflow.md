# 从本地输入到已核验动画

## 1. 准备外部输入

需要：固定源码Git检出、包含LBX资产的游戏归档、已确认的16色RGB调色板，以及用明确录制配置获得的原生录像。源提交为`ec7bdef58357185fe5344973c156b857a5de2c1f`；工具不会提供或下载这些原作文件。

归档和调色板必须位于显式`--quarantine-root`目录内。源码和FFmpeg可以位于其他外部目录，但都按请求/档案核验哈希。原生精确比较要求录制配置包含`vcodec="libx264rgb"`、`video_qp="0"`和`frame_drop_ratio="1"`。有损录制不能被标为原生像素基准。

安装检查与场景档案：

```sh
pm2-animation-lab doctor
pm2-animation-lab scenes
pm2-animation-lab index-video --help
```

例如，默认640×480视频中的活动区域是`32 240 320 128`：

```sh
pm2-animation-lab index-video --video /data/recordings/native.mkv --video-sha256 REPLACE_WITH_VIDEO_SHA256 --ffmpeg /tools/ffmpeg --ffmpeg-sha256 REPLACE_WITH_FFMPEG_SHA256 --output-directory /data/observations/index-v1 --quarantine-root /data --scene MULTI --crop 32 240 320 128 --crop-stream --capture-encoding lossless_rgb
```

以`index-video --help`所列参数为准。全视频/裁剪RGB帧哈希、PTS、原始视频绑定都需保留；索引中的lossless标签不代替录制来源核验。

## 2. 在生成候选前冻结区间

固定选择文件包含`selection_independent_of_candidate: true`及`selections`数组。每项指定`scene`、`first_frame`、`end_frame_exclusive`和选择依据。末端必须有下一帧边界，不能从候选结果反向缩短区间来隐藏差异。

录制来源支持两类互斥证明：当前格式的`capture_session`；或原始`capture_spec`和`capture_command`。后者必须绑定启动时spec哈希、`-r`录像路径和`--recordconfig`配置路径。不要为旧录像伪造新来源记录。

## 3. 明确连续条件和可见分组

请求包括一次初始化随机带、入口状态及连续日程。每一天都必须给出`branch`、`expected_state`、`random_values`；星期日为`branch: "sunday"`、`expected_state: null`。`start_weekday`以星期日为0。

源码时间步不可删除。相邻时间步画面完全相同时，可以共用一个观测曝光段；边界不可见则保持未知。自然科学每步消耗随机输入；狩猎需要单独12步`hunting_intro`，之后每日8步；普通活动每日5步。

`pm2_animation_lab.pm2_activity_ordered_alignment.align(images, native_runs, first_frame, end_frame, include_opening=...)`提供前向合组：输入是连续源码RGB图和完整原生游程，每个游程含`rgb_sha256/first_frame/end_frame_exclusive`。只折叠相邻相同源码图，再逐序核对稳定段；不是无序检索。合组返回`groups/opening_end_frame/opening_merged_with_first_sunday`，不签发像素相等结论。

`examples/ordered-observation.template.json`说明观察结构。每组的`ticks`必须依次完整覆盖所有源码步；`first_frame/end_frame_exclusive`连续覆盖选区，`stable_first_frame`落在该组内。开场、首次星期日合并与片段尾端范围须写入`opening_note`。所有输入绑定使用`pm2-animation-lab bind <文件>`产生的真实哈希。

没有实际调用记录时，可搜索一组兼容输入：

```sh
pm2-animation-lab infer --request /data/requests/search.json --output /data/conditions/search-v1 --quarantine-root /data --source-root /sources/pm2 --beam-width 256
```

搜索目前接受普通5步结构；狩猎条件须显式提供。搜索输出不是动画。结果为有界搜索的一组兼容证据，不代表真实RNG、唯一解或全局最优；非零残差必须保留。

## 4. 发布

将完整请求送入唯一动画入口：

```sh
pm2-animation-lab pipeline --request /data/requests/activity.json --output /data/deliveries/activity-v1 --quarantine-root /data --source-root /sources/pm2
```

比较请求为`pm2_activity_comparison_request/v1`，示意结构见`examples/comparison-request.template.json`。请求中观察表和每份资产都是`{path,sha256}`绑定；模板中的占位值不能直接运行。

选择`refresh_reconstruction: "capture_conditioned_planar_copy"`后，孤立显存复制过渡只能由前后相邻源码姿势与固定源码模型计算；实录约束其复制进度，不能直接将实录图贴进合成结果。

发布成功产生三组GIF/MP4、`comparison.json`、`comparison_check.json`、`video_check.json`、源码序列及时间清单。`status: passed`仅代表比较证据有效；必须同时检查`equality_status: equal`。失败保留`.pending-pm2-*`目录，目标目录不发布；不覆盖旧交付。

严格action/activity/refresh请求仍使用同一入口。`observe`子命令服务于该严格oracle流程；它与有序比较观察结构是不同契约，不能直接互换JSON。

## 5. 播放前复核

```sh
pm2-animation-lab pipeline --request /data/requests/activity.json --playback-directory /data/deliveries/activity-v1 --quarantine-root /data --source-root /sources/pm2
```

只使用成功JSON的`media`项。默认拒绝有差异的结果；明确研究差异时可加`--allow-differences`，返回内容仍标为诊断，阈值不变。请求变化、资源变化或解释器状态格式变化后，重新生成新目录，不能忽略字段来继续使用旧片。

## 高级诊断接口

包内保留原分析模块，使用`python -m pm2_animation_lab.<模块名> --help`查看接口。诊断只输出PNG/JSON或手动静帧页面，不提供固定时长动画。批量静帧研究的外部位置由`PM2_DATA_ROOT`、`PM2_SOURCE_ROOT`、`PM2_ARCHIVE`、`PM2_PALETTE`环境变量配置；设置后在新进程中运行。可选模拟器回放需要自己提供外部可执行文件、配置及输入回放，不纳入标准安装或CI。
