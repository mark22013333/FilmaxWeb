# 下一輪的交接：換掉 ffmpeg 4.2.3

把下面整段貼進新的 session 就可以開始。這一份自己也留著，因為它記了
「為什麼要做」與「上一輪踩過哪些坑」。

---

## 貼這一段

```
FilmaxWeb（自架網頁串流媒體庫）在這台電腦的 C:\Users\Administrator\Projects\filmax-web。
規格書在 docs\規格需求書.md（索引）＋ docs\規格\ 底下一章一個檔案，
章別代號（A-4、G-6、J 的第 −1 層…）全書通用。

這一輪要做的是 J 章（影片畫質）剩下的第一件事：
**換掉目前在用的 ffmpeg 4.2.3，然後用新版重新量一次畫質，做出兩個決定。**

背景（詳細推導在 docs\規格\J-影片畫質.md，請先讀那一章）：
- 現在用到的 ffmpeg 是 ImageMagick 附帶的 4.2.3，而 4.x 拿不到 NVENC 的
  p1~p7 preset、-tune hq、-multipass，也沒有 libvmaf 與 libsvtav1。
- 上一輪的量測只有 SSIM，而 SSIM 在高位元率會飽和 —— 所以「12 Mbps 夠不夠」
  這個問題其實還沒有答案。VMAF 才答得出來。
- app\vidprobe.py 已經會自動偵測 libvmaf，有就用 VMAF、沒有就退回 SSIM。
  所以換好 ffmpeg 之後，直接重跑既有的 測試畫質-全套.bat 就會拿到 VMAF 分數。

要做的事：
1. 先確認這個 session 有沒有可以在這台電腦上執行指令的工具（device_bash）。
   有就直接做；沒有的話，所有要在主機上跑的東西都交付成 .bat 讓我自己雙擊。
2. 決定要裝哪一版、哪一個 build。要 libsvtav1 的話是 full build；
   libvmaf 與 nvenc 一般的 build 就有。標準來源是 gyan.dev 與 BtbN 兩個，
   **請自己上網確認目前的版本與檔名，不要用你記得的版本號。**
3. 裝好之後在 .env 用**完整路徑**指定 FFMPEG_PATH 與 FFPROBE_PATH
   （不要靠 PATH —— 這台機器上有 ImageMagick 附帶的那一份，靠 PATH 會不知道
   自己拿到哪一個）。.env 我自己貼，你把要貼的那幾行給我。
4. 重跑 測試畫質-全套.bat（清單在 quality-probe-list.txt，上次的結果在
   quality-probe-result.txt 可以對照），然後用 VMAF 的結果回答兩個問題：
   (a) x264 與 NVENC 各該用在什麼情況；
   (b) REMOTE_BITRATE_KBPS 開到多少就夠了（現在是 12000，是估的不是量的）。
5. 把結論寫進 docs\規格\J-影片畫質.md 並更新 docs\規格需求書.md 的索引。

上一輪在量測上踩過的坑，不要再踩一次（J 章有寫）：
- 合成訊源（testsrc / mandelbrot）的結果是無效的，一定要用真實片源。
- 每一個變體與比較用的參考片段都要**直接從原始檔以 -ss/-t 取樣**。
  先用 -c copy 切一段當參考會壞掉：4K 那個 Dolby Vision mkv 的 DTS 不單調，
  切出來的片段解不開，症狀是 SSIM=1.0000、位元率 24 kbps。
- 報告裡 kbps < 100 一律當「數字無效」，不要拿它下結論。
- ffmpeg 4.x 的 -encoders 常數列沒有數值那一欄，5.x 才有。解析那一欄的 regex
  已經修過（G-6），但換版之後要再確認一次抓得到。
- .bat 的內容必須是純 ASCII。cmd.exe 用主控台 codepage 解析 .bat，
  中文註解會被誤解成指令（上一輪就是這樣壞的），中文全部放在 Python 那邊。
- 不要印任何 .env 裡的秘密（FTP／TMDB／Google／AUTH_PASSWORD）。

工作方式：規劃先跟我討論、我說 OK 才動工。文件用繁體中文，精簡、有證據、
有「為什麼」。改完程式要跑一次 tests\ 底下那幾支（都是 python tests\xxx_test.py
的獨立腳本，不是 pytest）。目前全套是 516 通過、0 失敗，不要讓它變紅。
```

---

## 為什麼這件事排在最前面

| | |
|---|---|
| 現在的畫質天花板 | 被 4.2.3 壓著。NVENC 只能用舊的 preset 名稱，品質／速度曲線比 p1~p7 差 |
| 現在的量測天花板 | 只有 SSIM。SSIM 在高位元率會飽和，所以「12 Mbps 夠不夠」還沒有答案 |
| 成本 | 下載解壓一個 zip、改兩行 `.env`、重跑一次既有的 `.bat` |

## 這一輪之後還剩什麼（優先序）

1. **J 的 Direct Stream（remux 不重編）** —— 97/117 部回到零損失畫質，
   是畫質影響最大的一項。需要「remux 失敗自動退回重編」與後台的失敗清單。
2. **J 的本機直讀 `LIBRARY_LOCAL_ROOTS`** —— 片庫本來就在 `D:\1.FTP`，
   現在每段轉碼要經過「HTTP → FastAPI → FTP → 磁碟」四層。
3. **A-2／A-3／A-4 資料層整理** —— 批次寫入、時間欄位、單一刪除進入點。
4. **I 的 PDF 第一頁封面** —— 要等 A-4 的 `purge()`。
5. **C／D／E／F 剩下的體驗修正。**
