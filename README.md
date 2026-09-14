# 雙市場資金羅盤

台股以 FinLab 三大法人淨買賣估計產業資金流；美股以 Google Drive 上的 LSEG S&P 500 價量資料計算方向成交額代理。網站支援日、週、月、今年及自訂交易日區間。

## 本機預覽

```powershell
C:\Users\teraw_rp58jwl\anaconda3\python.exe -m http.server 4174 --directory dist
```

開啟 <http://127.0.0.1:4174/>。

## 更新資料

```powershell
.\daily_update.ps1
```

更新程式會先重建美股，再重建台股，驗證輸出後只提交 `dist/index.html` 與 `dist/data.js`。成功推送至 GitHub 後，GitHub Pages 工作流程會自動發布新版。

## 安裝每日排程

網站首次推送成功後執行：

```powershell
.\daily_update.ps1 -InstallTask
```

排程使用台北時間，每天 11:30 與 16:00 執行；若電腦當時未開機，會在下一次可執行時補跑。這台電腦必須能讀取 FinLab 快取／API 與 `G:` Google Drive 的 LSEG 資料庫。

## 資料口徑

- 台股：外陸資（不含外資自營商）＋投信＋自營商自行買賣，估計金額為淨買賣股數乘以當日收盤價。
- 美股：每日報酬乘以收盤價與成交量的方向成交額代理，不代表基金或法人真實淨流量。
- 台美股口徑不同，數值只適合各自市場內比較。
