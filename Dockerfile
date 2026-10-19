# 推免雷达：魔搭创空间（Docker 方式）/ 任意容器平台部署
#
# 数据：镜像里用的是 deploy_data/out（collector/tools/sanitize_for_deploy.py 生成的脱敏版），
#       不是原始 data/out。生成方法见 DEPLOY.md。
# 端口：默认 7860（创空间常用端口）；平台若注入 PORT 环境变量会以它为准。
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=7860 \
    # 云上不定时抓取：一是对学校服务器不礼貌，二是部分学校官网公网访问不到（见 issues/001）
    TUIMIAN_CRAWL_SCHEDULE=0 \
    # 公网演示：演示身份只读，访客拿到自己的 guest_xxx，论坛只读
    TUIMIAN_PUBLIC=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com -r requirements.txt

COPY run.py ./
COPY server ./server
COPY collector ./collector
COPY contracts ./contracts
COPY web ./web
COPY data/sample/out ./data/out

# 运行时状态目录（演示用户 / 订阅等，首次启动自动生成）
RUN mkdir -p server/var && useradd -m -u 1000 app && chown -R app /app
USER app

EXPOSE 7860
CMD ["python", "run.py"]
