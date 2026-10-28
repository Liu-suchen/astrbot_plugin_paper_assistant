# AstrBot Paper Assistant · 论文阅读助手

通过消息指令添加、查找 Zotero 文献，获取论文总结与已有中文译文。面向独立安装，
配置不依赖开发者的目录、账号、群号或部署地址。

当前版本：开发预览。基础逻辑有离线行为测试，真实平台收发、Zotero Storage 上传和翻译网站接入仍需部署者验收。
主要面向 QQ / OneBot v11；其他平台必须支持 AstrBot 的主动消息和文件消息，兼容性另行验证。
每个插件实例使用一个由管理员配置的共享 Zotero 文献库；允许的用户均可操作该库，不提供每用户独立 Zotero 账号。

## 安装与首次配置

运行环境：Python 3.12+，AstrBot 4.24.2+。插件以当前 AstrBot SDK 为开发基准，
不保证更早版本；后续发布前仍需真实容器联调。

将仓库内容放在 AstrBot 插件目录 astrbot_plugin_paper_assistant 下，安装 requirements.txt 中的依赖，
然后通过插件管理加载或重载。公开仓库地址尚未确定，暂不提供虚构的安装链接。
加载方式与普通 AstrBot 插件一致。

在插件设置中填写：

| 设置 | 用途 |
| --- | --- |
| allowed_users | 当前消息平台的用户 ID 列表；QQ 使用 QQ 号。留空拒绝文献操作 |
| private_only | 默认开启，仅在私聊使用私人文献库 |
| zotero_library_type | users 个人库，groups 群组库 |
| zotero_library_id | 数字 User ID 或 Group ID，不是昵称 |
| zotero_api_key | 专用 API Key，授予目标库读写和文件权限 |
| zotero_collection_key | 可选的集合 Key；留空保存到库根目录 |
| summary_provider_id | 已配置的 AstrBot 对话模型提供商 ID；只在显式模型总结时使用 |
| file_transfer_mode | auto / local / callback |
| file_callback_base | 可选的文件回调地址，默认空；留空时可继承 AstrBot 全局设置 |

API Key 创建入口：[Zotero API Keys](https://www.zotero.org/settings/keys/new)。
User ID 可在该页查看。集合 Key 可从 Zotero Web Library 对应集合的地址中取得。
推荐先使用单独的测试集合。配置示例见 [config.example.json](config.example.json)，其中没有真实凭据。

本插件实现 Zotero Storage 文件上传；WebDAV 上传未实现。设置变更后重载插件。
变更目标库时，任务、关联和文件按文献库隔离，原库任务不会迁移到新库。

## 不同部署的文件发送

- **同机或共享文件目录**：选择 local；消息适配器需要能访问同一路径。
- **不同主机或独立容器**：选择 callback，填写消息平台能访问的 AstrBot 地址。
- **auto（默认）**：优先插件回调地址，其次 AstrBot 全局 callback_api_base；均未配置时使用本地文件。

回调地址可以是 HTTPS 反向代理地址，也可以是受信网络内可达的 HTTP 地址。
地址必须指向 AstrBot，不应填写 QQ 账号、API Key、数据库路径或翻译网站地址。
请按自己的网络设置主机名和端口；插件不会假定某个操作系统、容器 DNS 名称或端口。
文件链接有有效期，每次重新投递会创建新链接。是否可达需要从实际消息平台侧验证。

## 指令

| 指令 | 作用 |
| --- | --- |
| /paper_add 编号或标题 | 查询 arXiv，确认候选后去重加入 Zotero，上传并发送原文 PDF |
| /paper_find 编号或标题 | 只搜索已有 Zotero 文献，返回信息及附件标题 |
| /paper_sum 编号或标题 | 读取 Papers.cool 已有解读 |
| /paper_sum_model 编号或标题 | 明确调用配置的模型生成总结 |
| /paper_zh 编号或标题 | 读取已有、版本匹配的译文 PDF，或提供翻译网站入口 |
| /paper_save_zh 编号或标题 | 将可用译文保存到 Zotero，必要时创建文献 |
| /paper_pick 请求编号 序号 | 选择当前用户、当前会话的候选 |
| /paper_status 任务编号 | 查询状态并取回已保存结果 |
| /paper_help | 显示帮助 |

示例：/paper_add 1706.03762，或 /paper_add Attention Is All You Need。
支持 arXiv 编号、abs/pdf 链接、旧式编号及版本号。标题外部搜索首版限 arXiv。
回复带有 [paper:任务编号] 的插件消息时，可省略阅读指令的论文参数。
候选有效期、数量、分段长度等可在设置中调整。
所有指令只在当前会话回复；插件没有固定收件人或默认推送群。
获取 PDF 或总结不会添加 Read 标签，阅读记录与自动收集不在当前实现中。

## 高级配置与服务端点

所有默认值以 [_conf_schema.json](_conf_schema.json) 为唯一来源，运行时与配置 UI 共用；
缺少旧版本新增的参数时自动采用默认值，不需要改 Python 文件。

可调参数包括：

- 网络超时、读取重试次数和最大等待；
- 任务超时、检查间隔、每用户队列上限和投递尝试次数；
- 候选数量、选择有效期、消息分段和文件链接有效期；
- PDF 大小、提取文本长度和总结输出长度；
- 总结语言、结构要求、系统提示词、译文附件识别词；
- arXiv API/页面、Zotero API/页面、Papers.cool、翻译网站入口。

公共服务使用官方地址作为可编辑默认值。服务端点替换仅适用于兼容相同协议的可信服务，
不会把任意网页自动变成相应 API。Zotero API Key 会发送到配置的 Zotero API 地址。
arXiv 标准编号格式、Atom 命名空间、Zotero 字段和协议版本属于实现协议，不作为部署参数。

## 结果来源与当前限制

**添加与查找**

按 arXiv 基础编号和 DOI 去重；同名但身份未匹配的条目会提示核对，不直接合并。
重试使用稳定条目 Key。原文、译文和不同 arXiv 版本使用独立附件。
文献已创建但 PDF 上传失败时，分别反馈，重试不会盲目重复创建父条目。

**总结**

Papers.cool 适配器只 GET 读取已有解读，不请求生成，也不修改计数。
这是网站公开脚本使用的读取方式，并非承诺稳定的公开 API；失败时提供论文页和模型总结入口。
网站未标明解读对应版本，因此结果会提示这一点。
模型总结明确标注来自 PDF 提取文本还是仅摘要；只有 /paper_sum_model 调用模型，
更改语言、提示词或提供商会使用不同缓存。

**译文**

翻译网站的自动创建、进度查询和下载尚未接通。
/paper_zh 可复用缓存、Zotero 中已同步且版本匹配的译文，或返回配置的网站入口。
附件标题/文件名需要包含可配置的译文识别词和准确 arXiv 版本，例如 1706.03762v7_zh.pdf。
不自动付费、不伪造完成状态。改变入口地址不会自动开通新网站的翻译能力。

**任务与缓存**

任务、候选和缓存位于 AstrBot 提供的插件数据目录，按目标文献库分区，不使用绝对部署路径。
已受理任务持久化，重载后恢复；网络读取有限重试，写入不盲目重试。
QQ 等消息服务发送与本地记账无法保证严格恰好一次，发送成功瞬间崩溃仍可能重复。
文件缓存暂不自动清理；确认没有待投递任务后再清理，并留意磁盘空间。
若部署机器关机或休眠，实时处理暂停，服务不保证收到离线期间的消息。

## 开发与发布

在本仓库根目录运行离线测试：

    python -B -m unittest discover -s tests -v

如已安装 Ruff，在插件仓库根目录运行：

    ruff format .
    ruff check .

测试使用模拟网络响应和临时数据库，不需要私人凭据，不会调用模型或发送消息。
公开仓库只包含源码、元数据、配置 Schema、空凭据示例、测试和文档；
不要提交实际配置、数据库、缓存 PDF、Cookie、日志或个人路径。

提交社区前仍需完成：

- 真实 AstrBot 加载和指定消息平台文本/文件测试；
- 测试 Zotero 集合入库、附件上传、手机打开和失败恢复；
- 验证外部网站的实际可用性并保留限制说明；
- 确定公开仓库 URL，填入 metadata.yaml 的 repo 后再提交插件市场。

作者与版本统一由 metadata.yaml 提供，不在执行代码中重复注册。
许可见 [LICENSE](LICENSE)。
