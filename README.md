# Ubuntu 轻量盘中行情采集器 v1.1

这是一套与 `quant-invest` 分离运行的轻量采集节点。它只负责保存原始数字行情，不生成投资建议、板块分组、因子或文字报告。

## 最重要的维护规则

日常只需要编辑一个文件：

```text
config/instruments.csv
```

格式只有一列：

```csv
code
688041
300308
020692
AU999
```

规则：

- 六位数字：程序自动推断上海、深圳或北京市场，并在首次联网采集时判断是否存在实时行情；
- 有实时行情：作为股票、ETF、LOF等市场证券采集；
- 无实时行情：作为参考基金保存到元数据，不伪造盘中净值；
- `AU999`、`AU9999`、`Au99.99` 都会统一识别为上海黄金交易所 `Au99.99`；
- 从 CSV 删除代码后，后续停止采集，但数据库中的历史数据不删除；
- 保存 CSV 后无需重启服务，下一个节点自动生效。

内置指数不需要日常维护：上证指数、深证成指、创业板指、沪深300、科创50。

## 默认采集频率（北京时间）

A股：

- 09:30—10:00：每10分钟；
- 10:00—11:30：每30分钟；
- 13:00—14:00：每30分钟；
- 14:00—15:00：每10分钟。

Au99.99：

- 00:00—02:30：每30分钟；
- 09:00—15:30：每30分钟；
- 20:00—23:30：每30分钟。

程序内部统一使用 `Asia/Shanghai`，Ubuntu 主机可继续使用美国当地时区。

## 保存字段

SQLite 数据库：

```text
data/intraday.db
```

`quote_snapshots` 保存：

- 采集节点时间、数据源行情时间；
- 代码、名称、资产类型、交易所；
- 最新价、昨收、开盘、最高、最低；
- 涨跌额、涨跌幅；
- 日内累计成交量、累计成交额；
- 相邻采集节点之间的成交量增量、成交额增量；
- 有效状态和错误信息。

说明：上海黄金交易所公开分时端点不一定提供成交量、成交额和昨收。程序只保存端点实际返回的字段；缺失字段保持 `NULL`，不会猜测或伪造。

## 安装

Ubuntu 安装 Python：

```bash
sudo apt update
sudo apt install -y python3 unzip
```

将压缩包上传并解压，例如：

```bash
mkdir -p ~/apps
cd ~/apps
unzip ubuntu_intraday_collector_v1_1.zip
cd ubuntu_intraday_collector_v1_1
```

安装服务：

```bash
chmod +x install.sh
./install.sh
sudo loginctl enable-linger "$USER"
```

## 首次联网测试

A股和指数：

```bash
python3 collector.py once --scope equity --force
```

Au99.99：

```bash
python3 collector.py once --scope gold --force
```

查看识别结果和数据库状态：

```bash
python3 collector.py status
```

## 服务管理

```bash
systemctl --user status intraday-collector
systemctl --user restart intraday-collector
systemctl --user stop intraday-collector
systemctl --user start intraday-collector
journalctl --user -u intraday-collector -f
```

## 修改池

```bash
nano config/instruments.csv
```

增加一行六位代码或 `AU999`，保存即可，不需要重启。

## 导出和备份

按日期导出 CSV：

```bash
python3 collector.py export 2026-09-15
```

立即备份：

```bash
python3 collector.py backup
```

每日自动备份保存在：

```text
data/backups/intraday_YYYYMMDD.db
data/backups/intraday_latest.db
```

建议同步 `data/backups/intraday_latest.db`，不要实时同步正在写入的 `data/intraday.db`。

## 卸载服务

```bash
./uninstall.sh
```

卸载不会删除项目目录和数据库。

## 资源占用

按当前约81个六位代码、5个内置指数和Au99.99估算：

- 空闲内存通常约20—40MB；
- systemd内存上限128MB；
- 单次采集通常数秒；
- 三个月数据通常几十MB；
- 一年通常在数百MB以内。

实际大小取决于实时证券数量、采样频率和失败日志。
