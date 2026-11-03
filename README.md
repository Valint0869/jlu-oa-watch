# 吉大 OA 通知监控

一个**免登录、免浏览器**的吉林大学 OA 通知监控脚本：定时读取 `https://oa.jlu.edu.cn/` 的公开“校内通知”，提取正文、生成摘要、按学生情况打分，并把值得看的通知推到手机。

> 只访问 `oa.jlu.edu.cn` 的公开页面，不登录、不提交账号密码、不调用任何业务接口。它读取的是 OA 首页本来就公开的“校内通知”列表和详情页。

## 为什么不用浏览器插件 / RPA

浏览器插件需要一直开着 Edge、保持登录，OA 页面改版或登录过期就容易失效，还会反复“自动点击”。这个方案直接请求 OA 页面中通知链接指向的同一个地址，效果等价，但更轻、更稳，也不需要保存统一身份认证密码。

## 功能

- 抓取“校内通知”列表和详情页，提取标题、部门、日期、正文、附件
- 结构化摘要：优先抽取“时间、地点、对象、报名方式、截止时间”
- 按主题打分：入党/团、奖学金/资助、竞赛/创新创业、停水停电供暖、教学考试、安全健康等
- 按个人画像加分：校区、公寓、本科生/新生、关键词
- 降权噪音：研究生/博士、教职工、招标/任免/拟聘、纯会议培训等
- 微信推送：只发一条汇总，避免刷屏；支持静默时段、失败重试、唤醒补跑
- 本地留档：`digest_latest.md` 保存完整摘要，`logs/oa_watch.log` 记录运行日志
- 无第三方 Python 依赖，只使用标准库（Python 3.10+）

## 环境要求

- Windows 10/11
- Python 3.10+（需要 `pythonw.exe` 用于后台运行）
- 能访问 `oa.jlu.edu.cn`：校园网或学校 VPN
- 可选：DeepSeek Harness 桌面版 + 微信机器人插件（当前默认推送通道）

## 快速开始

### 1. 准备配置

把 `config.example.json` 复制为 `config.json`，至少填写：

```json
"profile": {
  "grade": "大一",
  "campus": "你的校区（如：南岭校区）",
  "dorm": "你的公寓（如：12公寓）",
  "extra_keywords": [],
  "mute_keywords": ["研究生", "博士", "教职工", "博士后"]
}
```

`campus` 填你所在校区（如“南岭校区”），`dorm` 填公寓名（如“12公寓”）。不填就不会有校区/公寓加分。

### 2. 检查抓取

```powershell
python .\oa_watch.py --check
```

能打印出通知条数和前几条标题，就说明网络和解析正常。

### 3. 建立基线

第一次运行不建议把历史通知全部推给自己：

```powershell
# 只把当前列表记为已读，不发送
python .\oa_watch.py --baseline --init-only
```

如果你想立刻收到一条“当前值得关注”的启动消息，可以改用：

```powershell
python .\oa_watch.py --baseline
```

### 4. 安装计划任务

```powershell
powershell -ExecutionPolicy Bypass -File .\install_task.ps1
```

默认每 30 分钟运行一次，后台隐藏，不弹窗。可选：安装“睡眠唤醒后补跑一次”：

```powershell
powershell -ExecutionPolicy Bypass -File .\install_wake_task.ps1
```

### 5. 测试推送

```powershell
python .\oa_watch.py --test-send
```

## 常用命令

```powershell
# 只检查抓取，不改状态
python .\oa_watch.py --check

# 抓取并打印摘要，不发送、不记已读
python .\oa_watch.py --dry-run --max-new 5

# 正常巡检一轮（计划任务调用的就是这个）
python .\oa_watch.py

# 测试微信通道
python .\oa_watch.py --test-send

# 重新建立基线，不发送
python .\oa_watch.py --baseline --init-only
```

计划任务：

```powershell
schtasks /Query /TN "JLU-OA-Watch" /V /FO LIST
schtasks /Run   /TN "JLU-OA-Watch"
schtasks /End   /TN "JLU-OA-Watch"

schtasks /Query /TN "JLU-OA-Watch-OnWake"
```

`--if-stale N`：距上次巡检不足 N 分钟就直接退出，专给唤醒补跑用，避免一次唤醒重复推送。

## 配置说明

```json
{
  "channels": ["179577"],
  "profile": {
    "grade": "大一",
    "campus": "你的校区（如：南岭校区）",
    "dorm": "你的公寓（如：12公寓）",
    "extra_keywords": ["辅导员", "班级群"],
    "mute_keywords": ["研究生", "博士", "教职工", "博士后"]
  },
  "notify": {
    "enabled": true,
    "min_score": 40,
    "important_score": 65,
    "max_items": 6,
    "max_chars": 1500,
    "send_empty": false,
    "quiet_start": "23:30",
    "quiet_end": "07:00",
    "urgent_score": 80
  },
  "fetch": {
    "max_pages": 4,
    "timeout": 20,
    "delay": 0.6,
    "retries": 2
  },
  "run": {
    "retry_delays": [60, 120]
  }
}
```

- `channels`：默认只抓 `179577`（校内通知）。不建议加“快讯/动态”频道，噪音大。
- `profile.campus` / `profile.dorm`：命中后分别 +15 / +20 分。
- `profile.extra_keywords`：额外关注的词，每个命中 +12 分。
- `profile.mute_keywords`：标题里出现就 -6 分。
- `notify.min_score`：达到多少分才推送；40 是“关注”，65 是“重要”。
- `notify.max_items`：一条微信最多列几条。
- `notify.quiet_start` / `quiet_end`：静默时段，普通通知留到早上；`urgent_score` 以上的仍会推送。
- `fetch.max_pages`：每次最多翻几页，防止一次抓太多。
- `run.retry_delays`：整轮抓取失败后的重试间隔（秒）；`[]` 表示不重试。

### 可选：接入模型做更强的语义摘要

`config.json` 里的 `ai` 默认关闭。如果你有 OpenAI 兼容接口的 key，可以打开：

```json
"ai": {
  "enabled": true,
  "base_url": "https://api.deepseek.com/v1",
  "api_key": "sk-...",
  "model": "deepseek-chat",
  "timeout": 60
}
```

打开后，每轮会把新通知的标题和正文发给该接口，让模型给出更准确的重要性评分和摘要；接口失败会自动回退到本地规则。**注意**：这会把 OA 正文发送到外部服务，默认不开启。

## 打分逻辑

标题优先，正文只补充“谁/哪里/什么时候”等细节：

- 入党/党团、奖学金/资助、竞赛/创新创业：+30 ~ +35
- 停水/停电/供暖/维修/公寓/食堂/校车：+34
- 选课/考试/学籍/转专业/推免：+28
- 安全/健康：+26
- 你的校区：+15；你的公寓：+20；各校区/全校：+8
- 本科生/新生：+10；学生：+5
- 截止/报名/申请/提交：+8；紧急/临时/今天/明天：+8
- 研究生/博士、教师/教职工、招标/任免/拟聘等降权

摘要从正文中挑“时间、地点、对象、报名方式、截止时间”等最有信息量的句子，按原文顺序拼接。

## 推送通道

默认通过 DeepSeek Harness 的微信机器人发送：

- 脚本会查找 `~/.dsh/profiles/*/node_modules/dsh-wechat-plugin/cli.js`
- 以及 `~/.dsh/dsh-runtimes/*/dependencies/node/bin/node.exe`
- 也可以通过 `notify.wechat_node` / `notify.wechat_cli` 指定绝对路径

如果你不用 DSH，可以改 `send_wechat()`，接入邮件、PushPlus、Server 酱、Bark、Telegram 等。脚本的其余部分（抓取、摘要、打分）与推送通道解耦。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `oa_watch.py` | 主程序：抓取、解析、摘要、打分、推送 |
| `config.example.json` | 配置模板，复制为 `config.json` 后修改 |
| `install_task.ps1` | 安装每 30 分钟运行的计划任务 |
| `install_wake_task.ps1` | 安装睡眠唤醒后补跑一次的任务 |
| `uninstall_task.ps1` | 卸载相关计划任务 |
| `state.json` | 已读记录和待重试队列，自动维护（不提交） |
| `digest_latest.md` | 最近一轮完整摘要（不提交） |
| `logs/oa_watch.log` | 运行日志（不提交） |
| `run.lock` | 单实例锁（不提交） |

## 限制

1. **只覆盖公开的“校内通知”**。看不到登录后的个人待办、公文、站内信、教务系统选课结果等。
2. **需要校园网或学校 VPN**。`oa.jlu.edu.cn` 解析到校内地址，校外访问不了时会抓取失败并写日志。
3. **电脑要开机并登录**。计划任务是“用户登录时运行”；睡眠/关机期间不跑，唤醒后会补跑一次。
4. **微信回复窗口有限**。微信机器人一个窗口最多连发 10 条；脚本每轮只发 1 条汇总，发不出去会自动排队。
5. **页面改版需要维护**。如果 OA 改了列表/正文的 HTML 结构，`parse_items` / `parse_detail` 可能要跟着改。
6. **不要移动脚本文件夹**。计划任务里写的是绝对路径；移动后重新运行安装脚本。
7. **TLS 校验被关闭**。OA 的证书链缺少中间证书，脚本用 `CERT_NONE` 读取公开页面；不传输任何账号信息。

## 故障排查

1. 先看日志：`logs/oa_watch.log`。
2. 抓取失败：确认浏览器能打开 `https://oa.jlu.edu.cn/`，以及当前是否在校园网/VPN。
3. 微信没收到：
   - 运行 `--test-send` 看退出码；
   - 确认 DSH 桌面版和微信机器人仍绑定；
   - 看 `~/.dsh/wechat/state.json` 里的 `quota.used` 是否已到 10。
4. 任务没跑：`schtasks /Query /TN "JLU-OA-Watch" /V /FO LIST`，看 `Last Result` 和 `Next Run Time`。
5. 改了 `config.json` 不需要重装任务，下一轮自动生效。

## 卸载

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall_task.ps1
```

然后删除整个文件夹即可。

## 隐私

- 不保存学号、密码、cookie 或统一身份认证信息。
- `config.json`、`state.json`、`digest_latest.md`、`logs/`、`run.lock` 已加入 `.gitignore`。
- 如果开启 `ai`，通知正文会发送到你配置的模型接口；请自行评估隐私。

## License

MIT
