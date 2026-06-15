#!/bin/bash
set -e

# 数据目录
DATA_DIR="/data"

echo "Checking data persistence..."

# 1. 确保持久化数据目录存在
mkdir -p "$DATA_DIR"

# 2. 处理 database.db (文件)
# 逻辑：如果数据卷里没有数据库，但容器代码里有（比如预置的），则复制过去；否则新建空文件让应用初始化
if [ ! -f "$DATA_DIR/database.db" ]; then
    if [ -f "/app/database.db" ]; then
        echo "Moving existing database to data volume..."
        mv /app/database.db "$DATA_DIR/database.db"
    else
        echo "Creating placeholder for database..."
        # 注意：不要 touch，sqlite 可能不喜欢空文件。
        # 让应用自己去创建，我们只需要确保软链接的目标路径是合法的？
        # 其实软链接可以指向不存在的文件。但 SQLite open 时会创建它。
        # 只要目录存在即可。
    fi
fi

# 无论如何，删除 /app 下的原文件（或残留链接），建立新链接
rm -f /app/database.db
ln -sf "$DATA_DIR/database.db" /app/database.db

# 3. 处理 repo_cache (目录)
mkdir -p "$DATA_DIR/repo_cache"
# 删除 /app 下可能存在的目录或旧链接
rm -rf /app/repo_cache
# 建立软链接
ln -sfn "$DATA_DIR/repo_cache" /app/repo_cache

# 4. 处理 epg_cache (目录)
mkdir -p "$DATA_DIR/epg_cache"
rm -rf /app/epg_cache
ln -sfn "$DATA_DIR/epg_cache" /app/epg_cache

# 5. 聚合静态产物（M3U / 预览 gzip）
mkdir -p "$DATA_DIR/artifacts/exports" "$DATA_DIR/artifacts/previews"

echo "Data persistence setup complete. Starting application..."

# 执行传入的命令 (CMD)
exec "$@"
