# 外部授權與方法來源

本目錄程式由既有第一方轉錄引擎移植。未複製第三方 Python／Node 套件、瀏覽器、FFmpeg 或模型權重。

`douyin_download.py` 的公開分享頁解析與正常瀏覽器回應觀察方式，原始碼既有註記參考 [shared-video-transcriber/server.py](https://github.com/ZY-ZhichaoYu/shared-video-transcriber/blob/main/server.py)。不引入其伺服器、雲端功能或其他檔案。保留其 [MIT 授權](https://github.com/ZY-ZhichaoYu/shared-video-transcriber/blob/main/LICENSE)：

```text
MIT License

Copyright (c) 2026 ZY-ZhichaoYu

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

執行時另行安裝的套件適用各自授權：faster-whisper MIT、CTranslate2 MIT、PyAV BSD-3-Clause、yt-dlp Unlicense、yt-dlp-ejs Unlicense AND MIT AND ISC、Playwright Apache-2.0。其套件內附授權與轉遞依賴告知仍須保留。FFmpeg 依實際 build 為 LGPL／GPL 條件；本專案不發送其二進位。模型權重由使用者另備，依所選模型的來源與授權處理，程式碼授權不涵蓋模型或影片。
