# ZenTao Chat Extractor

从禅道客户端（Electron/Chromium）数据目录中提取聊天记录，导出为 JSON / CSV。  
核心数据来自 `IndexedDB`，并自动关联成员信息、会话信息与图片文件路径。

## 快速开始

### 克隆仓库

```bash
git clone --recursive https://github.com/gaoweifan/ZenTaoChatExtractor.git
```

如果已克隆但未拉取子模块：

```bash
git submodule update --init --recursive
```

### 运行导出

在仓库根目录运行（建议显式指定 `--root`）：

```bash
python export_zentao_chat.py --root "C:\Users\gaoweifan\AppData\Roaming\zentaoclient" --out output --format both
```

如果只导出某个账号对应的数据：

```bash
python export_zentao_chat.py --root "C:\Users\gaoweifan\AppData\Roaming\zentaoclient" --db-name gaoweifan@192.168.131.211__11443 --out output --format both
```

提示：`db-name` 是 `zentaoclient\users` 目录下的文件夹名称。

导出结果示例：

```
output/
  gaoweifan@192.168.131.211__11443/
    messages.json
    messages.csv
```

## 使用方法

```
python export_zentao_chat.py [options]
```

### 主要参数

| 参数 | 说明 |
|---|---|
| `--root` | 禅道客户端数据根目录，默认 `zentaoclient`（若不在当前工作目录，请显式指定，例如 Windows 默认路径 `C:\Users\gaoweifan\AppData\Roaming\zentaoclient`） |
| `--db-name` | 仅导出指定数据库名称（可多次指定） |
| `--out` | 输出目录，默认 `output` |
| `--include-deleted` | 导出已删除消息 |
| `--include-duplicates` | 导出 LevelDB 历史版本（不去重） |
| `--format` | `json` / `csv` / `both`，默认 `both` |
| `--timezone` | `local` / `utc`，默认 `local` |

### 常见用法

导出某个账号的数据为 CSV：

```bash
python export_zentao_chat.py --root "C:\Users\gaoweifan\AppData\Roaming\zentaoclient" --db-name gaoweifan@192.168.131.31__11443 --out output --format csv
```

包含已删除消息：

```bash
python export_zentao_chat.py --root "C:\Users\gaoweifan\AppData\Roaming\zentaoclient" --db-name gaoweifan@192.168.125.73__11443 --include-deleted
```

## 输出内容说明

### messages.json

- `messages`：完整消息列表  
- `members`：成员信息（按 user id 索引）  
- `chats`：会话信息（按 chat gid 索引）  
- `message_count`：导出条数  

### messages.csv

每行对应一条消息，包含以下关键字段：

- `chat_id` / `chat_name` / `chat_type`  
- `sender_id` / `sender_account` / `sender_realname`  
- `timestamp_ms` / `timestamp_iso`  
- `content_type` / `content` / `content_json`  
- `image_path` / `image_thumb_path`  

为保证 CSV 行结构稳定，所有字段中的换行符会被替换成字面量 `\n`。

## 工作原理

1) **读取 IndexedDB**  
   从 `<root>/IndexedDB/file__0.indexeddb.leveldb` 解析数据库内容。

2) **提取三类核心数据**
   - `Member`：成员信息  
   - `Chat`：会话信息  
   - `ChatMessage`：消息记录  

3) **去重策略**  
   LevelDB 可能包含多版本记录：
   - 成员/会话：按“更新时间”选择较新版本  
   - 消息：默认去重并优先保留“未删除、较新”的版本  
   - 若指定 `--include-duplicates` 则保留全部历史记录

4) **内容解析**
   - 文本消息直接输出 `content`  
   - 图片/文件/链接/表情等，`content` 里是 JSON 字符串，会解析到 `content_json`  
   - 图片根据 `gid` 在 `<root>/users/<db-name>/images` 下匹配文件路径

5) **写出 JSON/CSV**

## 关键模块/函数说明

脚本中关键逻辑集中在以下函数：

- `snappy_decompress`  
  用于解压 LevelDB 中的 Snappy 压缩块，避免额外依赖。

- `install_shims` / `add_ccl_reader_path`  
  注入必要的模块 shim，并将本地 `ccl_chromium_reader` 加入搜索路径。

- `normalize_json`  
  统一清洗输出结构，保证 JSON 可序列化（如 set 转 list）。

- `parse_content`  
  解析 `content` 字段中的 JSON 字符串（图片/文件/链接/表情消息等）。

- `pick_member` / `pick_chat`  
  去重策略：选择“未删除/较新”的成员和会话记录。

- `find_image_paths`  
  根据图片 `gid` 在 `users/<db-name>/images` 下匹配原图与缩略图。

- `csv_safe_value`  
  处理多行文本，把换行替换为 `\\n`，避免 CSV 行错乱。

- `export_db`  
  单个数据库的核心导出流程，具体包括：  
  1) 读取 `Member` 与 `Chat` store 并做去重（优先非删除与较新记录）  
  2) 读取 `ChatMessage`，按 `unionId / id / gid` 去重（可选保留历史版本）  
  3) 解析 `content` JSON，提取图片/文件/链接/表情等结构化字段  
  4) 关联成员与会话信息，补齐 `sender_account / chat_name / chat_members`  
  5) 匹配图片路径 `users/<db-name>/images/<gid>.*` 和缩略图  
  6) 按时间与索引排序消息  
  7) 写出 JSON / CSV，并进行 CSV 字段换行安全处理  

## 目录结构要求

默认假定数据结构如下：

```
<root>/
  IndexedDB/file__0.indexeddb.leveldb/
  users/<db-name>/images/
```

说明：`<root>` 是禅道客户端数据目录，Windows 默认路径类似：

```
C:\Users\gaoweifan\AppData\Roaming\zentaoclient
```

其中 `<db-name>` 类似：

```
gaoweifan@192.168.131.211__11443
```

## 依赖与环境

- Python 3.10+
