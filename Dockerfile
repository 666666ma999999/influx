FROM mcr.microsoft.com/playwright/python:v1.57.0-jammy

WORKDIR /app

# 日本語フォント・ロケール設定
RUN apt-get update && apt-get install -y \
    fonts-noto-cjk \
    locales \
    && rm -rf /var/lib/apt/lists/* \
    && locale-gen ja_JP.UTF-8

ENV LANG=ja_JP.UTF-8
ENV LANGUAGE=ja_JP:ja
ENV LC_ALL=ja_JP.UTF-8
ENV TZ=Asia/Tokyo

# 依存パッケージをインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコードをコピー
COPY . .

# 出力ディレクトリを作成
RUN mkdir -p /app/output /app/data /app/x_profile

# 非rootユーザーで実行（セキュリティ対策）
# ただしブラウザ操作のため一部権限が必要
RUN chown -R pwuser:pwuser /app
USER pwuser

CMD ["python", "scripts/collect_tweets.py"]
