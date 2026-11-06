# 股票异动扫描器网页版

Flask 包装的统一股票异动扫描器（A股 / 港股 / 美股），部署在 InstaBox 免费沙箱上。

## 功能
- 自选股扫描：输入代码，返回异动评分、涨跌幅、RSI、量比、MA、涨停/炸板/突破信号
- A股涨停池：当日涨停板名单
- 数据源：新浪行情、腾讯K线、Yahoo、CBOE（仅供个人研究）

## 本地运行
```bash
pip install -r requirements.txt
python app.py
# 打开 http://localhost:5000
```

## 部署（InstaBox）
```bash
pip install -r requirements.txt
python app.py   # 监听 0.0.0.0:5000，在 InstaBox 里把 5000 端口设为 Public
```

## 注意
- 免费沙箱空闲 30 分钟或连续运行 8 小时后自动暂停
- 异动评分 0–100，仅供参考，不构成投资建议
- Vol/OI>1 仅为成交活跃信号，不认定为机构买入
