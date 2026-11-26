# 脱敏占位符表（开源仓库沿用同一套）

> 本文件会随仓库发布，作为「我们如何脱敏」的公开说明。真实值一律不出现。

## 占位符对照

| 占位符 | 替换的对象 |
|---|---|
| `<PLATFORM_HOST>` | 文档平台租户域名（如 `<tenant>.example.com`） |
| `<FOLDER_ID>` | 云盘文件夹 ID |
| `<FILE_ID>` | 单个文件 ID |
| `<USER_HOME>` | 本机用户主目录的绝对路径 |
| `<SERIAL>` | 机器序列号 |
| `<HARDWARE_UUID>` | 机器硬件 UUID |
| `<COURSE_TITLE>` | 被录制内容的标题 |
| `<INSTRUCTOR>` | 内容作者 / 讲师 |
| `<REDACTED>` | 任何 key、token、cookie |

## 发布前必须全绿的检查

```bash
# 1) 真实值清零（占位符之外不该再有命中）
#    本文件自身含占位词与检查命令，扫描时排除：-g '!SECURITY.md'
#    机器路径前缀拆成变量写，避免本文件自己命中扫描正则
HOME_PREFIX='/Users'
rg -ni --hidden -g '!.git' -g '!SECURITY.md' \
  "$HOME_PREFIX/[A-Za-z]|Hardware[ _]UUID|Provisioning[ _]UDID|Serial[ _]Number" . || echo "clean"

# 1b) 私有词表（平台名、业务中文词、内部模块前缀）不进仓库，
#     放在仓库外一份，用 -f 引用，避免把它们写进公开文件
rg -ni --hidden -g '!.git' -g '!SECURITY.md' -f ../private-words.txt . || echo "clean"
#     词表至少包含：平台品牌词、业务目录的中文名、注入变量的内部前缀

# 2) 凭据扫描
(gitleaks detect --no-git -v 2>/dev/null) \
  || rg -n --hidden -g '!.git' 'sk-[A-Za-z0-9]{16,}|hf_[A-Za-z0-9]{20,}|Bearer [A-Za-z0-9\-_.]{20,}'

# 3) 媒体与状态文件不得进暂存区
git status --porcelain | grep -E '\.(mov|mp4|mkv|wav|log)$' && echo "!! 媒体或日志待提交"

# 4) 全树看一眼
git ls-files | head -80
```

## 硬规则

1. **内容不进仓库**：任何实际录制/下载的视频、真实队列、真实状态文件、课程清单。
2. **新建仓库，不重写历史**：私有项目转公开时 `git init` 全新仓库、单次初始提交；不要指望 `git rm` 能清掉历史。
3. **目录用通用英文**：不要用业务方向的中文名。
4. **示例一律假数据**：`example.com`、`example-course-01.mp4`、`2026-01-01T00:00:00Z`。
5. **推送即泄露**：宁可多扫一遍再 push。已推送就按泄露处理（换钥、联系镜像方）。
6. **扫描要不区分大小写，并扫中文**：品牌词常以首字母大写出现在 docstring/注释里，只扫小写形式会漏；业务目录名往往是中文，正则里要显式带上。
