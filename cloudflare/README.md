# Cloudflare Tunnel 設定

把 `video.longhopick.com` 接到本機的 FilmaxWeb，不必開任何連接埠。

## 為什麼用 Tunnel 而不是開 port

- **不用在路由器開連接埠轉發**，家用 IP 也不必是固定的
- **對外沒有可掃到的服務**。連線是 cloudflared 主動往外建立的，
  你的機器在公網上沒有開放的入口
- HTTPS 憑證由 Cloudflare 處理，不用自己申請與續期
- 附帶 DDoS 防護與 WAF
- 會送 `CF-Connecting-IP` 與訪客位置標頭 —— 登入紀錄的來源與位置直接就有了

## 兩條路，選一條

### A. 從後台建立（建議）— `設定通道-後台權杖.bat`

通道在 Cloudflare 後台建立，拿一組權杖回來安裝連線器。
**不需要 `cloudflared tunnel login` 那個瀏覽器授權流程**，
網域與路由都在後台的圖形介面上選，看得到自己在做什麼。

先在後台做：

1. 打開 <https://one.dash.cloudflare.com>
2. 左邊 **Networking → Tunnels**
3. **Create a tunnel** → 選 **Cloudflared**
4. 名稱打 `filmax` → **Create tunnel**
5. 作業系統選 **Windows**，畫面會給一行安裝指令，
   裡面 `service install` 後面那一長串就是權杖，複製起來

然後對 `設定通道-後台權杖.bat` 按**右鍵 → 以系統管理員身分執行**，把權杖貼進去。

裝完回後台：**Routes** 分頁 → **Add route** → **Published application**

| 欄位 | 填 |
|---|---|
| Subdomain | `video` |
| Domain | `longhopick.com` |
| Service Type | `HTTP` |
| URL | `127.0.0.1:8080` |

### B. 從指令列建立 — `設定通道.bat`

傳統做法，通道設定寫在本機的 `config.yml`。
會跑 `cloudflared tunnel login` 開瀏覽器授權。

**授權頁面看不到網域可選的話**，代表你登入的那個 Cloudflare 帳號底下
沒有可用的網域。可能是登入到別的帳號，或網域還沒完成啟用。
這種情況直接改用 A 比較快。

兩條路都會把 cloudflared 裝成 Windows 服務（開機自動啟動、掛掉自動重開）。

### 兩者差在哪

| | A 後台建立 | B 指令列建立 |
|---|---|---|
| 授權方式 | 一組權杖 | 瀏覽器 OAuth，產生 `cert.pem` |
| 路由設定在哪 | Cloudflare 後台 | 本機 `config.yml` |
| 改設定要重啟服務嗎 | 不用 | 要 |
| 適合 | 大部分情況 | 想把設定納入版控時 |

## 跑完之後一定要做的兩件事

> **順序很重要：先改好 `.env` 並重啟，再啟動通道。**
> 反過來的話，中間那段時間你的媒體庫是對全世界公開的。
>
> 服務本身有一道防線：`AUTH_ENABLED=false` 時，來自外部網路的連線一律回 403，
> 不會把媒體庫送出去。但那是最後的保險，不要當成流程的一部分。
> 已經先開了通道才想到的話，先 `sc stop cloudflared` 下線，改完再 `sc start cloudflared`。

### 1. 改 `.env` 並重啟服務

```ini
HOST=127.0.0.1        # 關鍵：只讓同機的 cloudflared 連得到
PORT=8080
TRUST_PROXY=true      # 這時候才可以開，代表前面真的有代理
COOKIE_SECURE=true    # session cookie 只走 HTTPS
AUTH_ENABLED=true
AUTH_PASSWORD=夠長的隨機字串
VIEWER_PASSWORD=分享給別人看片用的
```

`HOST` 留在 `0.0.0.0` 的話，區網裡任何人仍然可以繞過 Cloudflare 直連你的
服務——那條路徑沒有 WAF、沒有 HTTPS，而且 `TRUST_PROXY=true` 之下
來源判斷會採信他送的標頭。**一定要改成 `127.0.0.1`。**

### 2. 開啟訪客位置標頭

Cloudflare 後台 → **Rules → Managed Transforms → Add visitor location headers**

不開的話只拿得到國家（`CF-IPCountry`），城市、時區、經緯度都會是空的。
登入紀錄還是會寫，只是位置那欄顯示「位置未知」——那不是壞掉。

## 驗證

```
sc query cloudflared              查看服務狀態
cloudflared tunnel list           確認通道存在
cloudflared tunnel info filmax    看目前有幾條連線
```

打開 `https://video.longhopick.com` 應該會看到登入頁。
登入後用管理員身分打 `/api/audit/logins`，確認來源 IP 是你的真實外網位址
（不是 127.0.0.1），位置欄有值。

## 授權頁面看不到網域可以選

`cloudflared tunnel login` 的頁面列的是**你登入的那個 Cloudflare 帳號底下
已經啟用的網域**。清單空白代表：

- 登入到別的 Cloudflare 帳號（同一個 email 也可能有多個帳號／組織）
- `longhopick.com` 還在 Pending，nameserver 尚未完成切換
- 你在那個帳號裡的權限不足以建立通道

最快的解法是**改用後台建立**（上面的 A），完全不碰這個授權流程。

## 授權那一步卡住了

`cloudflared tunnel login` 會印出一個授權網址。**如果瀏覽器沒有自動打開**
（伺服器版 Windows、或沒有設定預設瀏覽器時很常見），要自己把那段網址
複製到瀏覽器貼上。網址長這樣：

```
https://dash.cloudflare.com/argotunnel?aud=&callback=https%3A%2F%2Flogin.cloudflareaccess.org%2F...
```

網頁上要**選到 longhopick.com** 再按 Authorize。只是登入而沒有選網域的話，
`cert.pem` 不會產生，腳本就會停在這一步。

授權網址有時效，過期就重跑腳本產生新的。想單獨重試這一步：

```
cloudflared tunnel login
```

成功的話 `%USERPROFILE%\.cloudflared\cert.pem` 會出現。

## 排錯

**開網址出現 502／1033** —— cloudflared 連得到 Cloudflare 但連不到你的服務。
確認 FilmaxWeb 有在跑，且 `PORT` 跟 `config.yml` 裡的一致。

**影片播到一半斷掉** —— 分段轉碼太久被切斷。`config.yml` 已經把
`connectTimeout` 調到 30 秒；還是會斷的話，把 `.env` 的 `HLS_SEGMENT_SECONDS`
調小（例如 4），單段轉碼時間就會下降。

**登入紀錄的 IP 是 127.0.0.1** —— `TRUST_PROXY` 沒開，或連線沒有真的走 tunnel。

**服務跑不起來** —— 先 `sc stop cloudflared`，再用
`cloudflared tunnel run filmax` 在前景跑，錯誤訊息會直接顯示出來。

## 注意

Cloudflare 免費方案的**代理流量**有使用規範，長時間大量的影音串流
不在它原本設想的用途裡。自己看片的量通常沒問題，但不要當成公開的
影音服務在跑——那既違反 Cloudflare 的服務條款，也違反本專案的使用聲明。
