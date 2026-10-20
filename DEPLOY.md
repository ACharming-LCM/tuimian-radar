# 部署到魔搭创空间（得到 https 演示链接）

> **已部署（2026-10-20）**
> - 应用直链：https://lcmcharming-tuimian-radar.ms.show/
> - 创空间页：https://www.modelscope.cn/studios/LCMcharming/tuimian-radar
> - 配置：Docker 类型 + `platform/2v-cpu-8g-mem`，监听 `0.0.0.0:7860`，`TUIMIAN_PUBLIC=1`
> - 实测：`/api/health` 200（2646 条）；首页/推送/公告/导师/今日更新/订阅页 200；
>   演示身份写入 403、访客 `guest_xxx` 写入 200、论坛发帖 403。
>
> 重新部署：改完代码后上传 `Dockerfile`、`requirements.txt`、`run.py`、`server/`、
> `collector/`、`contracts/`、`web/`、`data/sample/`，在创空间页点「重新部署」。
> 注意：魔搭要求 **ENTRYPOINT**（不能用 CMD）且端口必须是 **7860**。

## 1. 本地生成脱敏数据

部署用的数据是 `data/out/` 的脱敏副本。原始抓取数据里有考生名单，不能上公网：

```powershell
python collector/tools/sanitize_for_deploy.py --src data/out --dst data/sample/out
# 最后一行 "left_after_check": 0 才算通过
```

`data/sample/out` 已随仓库提供一份（2026-10-19 快照），所以**新克隆不改数据也能直接跑**。
抓完新数据后重跑上面这条命令即可更新。

脚本会做这些处理：

- 名单类公告：正文整段替换成说明文字，并引导用户去看原文。
- 正文里含「姓名 / 考生」的表格行：删掉。
- 手机号、证件号格式的数字：打码。
- 导师电话：清空。
- 截止日、三态、类型、排序：都不改动。

`data/sample/out` 是公开的脱敏数据（不含姓名/电话）；原始 `data/out` 仍被 `.gitignore` 排除。

## 2. 创建创空间

1. 打开 modelscope.cn，进入「创空间」，点「创建」。
2. SDK 或部署方式选 **Docker**。
3. 可见性选「公开」，链接要能给评委打开。
4. 把下面这些文件和目录上传到创空间仓库（它也是一个 git 仓库）：
   `Dockerfile` `requirements.txt` `run.py` `server/` `collector/` `contracts/` `web/` `data/sample/`
5. 在创空间的「设置 → 环境变量」里加一项 `TUIMIAN_ADMIN_TOKEN`，值自己定一个长随机串。不设的话，每次重启会随机生成一个，只打印在日志里。

## 3. 端口与配置

Dockerfile 默认监听 `0.0.0.0:7860`。平台注入 `PORT` 环境变量时，以 `PORT` 为准。

创空间对 Docker 的具体要求（端口、目录、资源）请以魔搭官方文档为准，这里写的值没有在创空间上实测过。

镜像内已关闭定时抓取（`TUIMIAN_CRAWL_SCHEDULE=0`），原因有两个：云上主动去抓学校服务器不合适；部分学校官网在公网访问不到（见 `issues/001`）。数据更新的流程是：在本地跑完抓取，重新执行第 1 步，再重新上传 `deploy_data/`。

## 4. 部署后自检

```
https://<你的创空间地址>/api/health        # 200，且响应里不应出现管理口令
https://<你的创空间地址>/                  # 首页
https://<你的创空间地址>/pushes.html       # 推送页
```
